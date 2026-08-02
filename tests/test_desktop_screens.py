"""Screen-level behaviour that only exists once real Qt widgets are involved.

Everything here needs an (offscreen) `QApplication` because it asserts on widget state (badge
text, list contents, notice messages) rather than on a plain-method return value. Where the same
behaviour could be exercised without Qt, it lives in `test_desktop_services.py` instead.

Never imports `routes`, `app`, or `flask` -- only `desktop.*` and `lib.*`.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QGroupBox

from desktop import grid, registry
from desktop.grid import GridCanvas, Placement
from desktop.screens.base import PanelBase
from desktop.screens.config import ConfigScreen
from desktop.screens.connection import ConnectionScreen
from desktop.screens.debug import DebugScreen
from desktop.screens.logs import LogsScreen
from desktop.screens.pid_tuning import PidTuningScreen
from desktop.screens.pilot import PilotScreen
from lib.aruco_logger import ArucoPipelineLogger


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeController:
    def __init__(self):
        self.killed = False
        self.debug_axes = None

    def is_killed(self):
        return self.killed

    def kill(self):
        self.killed = True

    def rearm(self):
        self.killed = False

    def set_debug_override(self, axes):
        if self.killed:
            return False
        self.debug_axes = dict(axes)
        return True

    def clear_debug_override(self):
        self.debug_axes = None

    def is_pid_enabled(self):
        return False


class FakeSetpointOverride:
    """Mirrors lib.setpoint_override.SetpointOverrideClient's shape.

    `fail_message`, when set, makes every send/clear raise -- reproducing the resource-health
    guard in `_check_resource_health()` that refuses to transmit when UDP RX errors are rising.
    """

    def __init__(self, fail_message=None):
        self.clear_count = 0
        self.send_count = 0
        self.fail_message = fail_message

    def _maybe_fail(self):
        if self.fail_message:
            raise RuntimeError(self.fail_message)

    def clear_override(self):
        self._maybe_fail()
        self.clear_count += 1
        return {"active": False, "axes": {}}

    def send_override(self, axes, **kwargs):
        self._maybe_fail()
        self.send_count += 1
        return {"active": True, "axes": dict(axes)}

    def get_state(self):
        return {"active": False, "axes": {}}

    def set_error(self, message):
        self.last_error = message


class _StubSignal:
    """Per-instance stand-in for a Qt signal. Must support disconnect -- PanelBase.teardown()
    disconnects its own slot so it does not sever other panels sharing the same poller."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, payload):
        for slot in list(self.slots):
            slot(payload)


class _StubPoller:
    """Duck-types desktop.services.Poller without ever starting a QTimer or hitting hardware.

    `_subscribers` mirrors the real reference count, which is the observable that proves a
    panel behind a dock tab costs nothing.
    """

    def __init__(self):
        self.updated = _StubSignal()
        self.failed = _StubSignal()
        self._subscribers = 0

    def subscribe(self):
        self._subscribers += 1

    def unsubscribe(self):
        self._subscribers = max(0, self._subscribers - 1)

    def set_interval(self, interval_ms):
        pass


class FakeHub:
    """Everything a screen's __init__ might touch -- never a real ServiceHub."""

    def __init__(self, controller=None, setpoint_override=None, aruco_logger=None, resource=None, imu=None):
        self.controller = controller
        self.setpoint_override = setpoint_override
        self.aruco_logger = aruco_logger
        self.ip_camera = None
        self.bitmask = None
        self.resource = resource
        self.log_stream = None
        self.control_telem = None
        self.imu = imu
        self.neutralize_calls = 0
        self.pollers = {}
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1

    def poller(self, key, fn, interval_ms):
        """Memoized by key, like the real ServiceHub -- two panels watching one key share a timer."""
        return self.pollers.setdefault(key, _StubPoller())

    def call_async(self, fn, on_done=None, on_error=None):
        try:
            result = fn()
        except Exception as exc:  # pragma: no cover - defensive only
            if on_error:
                on_error(str(exc))
            return
        if on_done:
            on_done(result)

    def neutralize_thruster_command(self):
        self.neutralize_calls += 1

    def manipulator_payload(self):
        return {}

    def connection_proof(self):
        """Mirrors ServiceHub.connection_proof()'s shape enough for ConnectionScreen tests."""
        return {
            "connected": False,
            "proofs": [],
            "uplink": {},
            "resource": self.resource.get_stats() if self.resource else {},
            "imu": {},
        }


# --- killswitch blocks debug override until rearm ------------------------------------------------
# (test_killswitch_zeroes_pid_gains_and_blocks_debug_override_until_rearm -- the "blocks override"
#  half; the "zeroes MCU gains" half is test_kill_work_zeroes_mcu_pid_gains in
#  test_desktop_services.py, which needs no Qt at all.)


def test_debug_screen_blocks_override_when_killed_then_allows_after_rearm(qapp):
    ctrl = FakeController()
    override = FakeSetpointOverride()
    hub = FakeHub(controller=ctrl, setpoint_override=override)
    screen = DebugScreen(hub)

    ctrl.kill()
    screen._enable_override()

    assert screen._override_active is False
    assert ctrl.debug_axes is None
    assert hub.neutralize_calls == 1
    assert "killed" in screen._notice.text()

    ctrl.rearm()
    screen._enable_override()

    assert screen._override_active is True
    assert ctrl.debug_axes is not None  # _send_override fired once synchronously


# --- setpoint override refusal (lib/setpoint_override.py's resource-health guard) reaches the
# operator instead of silently doing nothing ------------------------------------------------

_REFUSAL_MESSAGE = "Resource monitor reports increasing UDP RX errors; refusing to send override"


def test_debug_screen_surfaces_override_refusal_reason(qapp):
    ctrl = FakeController()
    override = FakeSetpointOverride(fail_message=_REFUSAL_MESSAGE)
    hub = FakeHub(controller=ctrl, setpoint_override=override)
    screen = DebugScreen(hub)

    screen._enable_override()
    screen._stop_all()

    assert screen._override_active is False
    assert _REFUSAL_MESSAGE in screen._notice.text()
    assert screen._notice.text().startswith("Debug override:")


def test_pid_tuning_screen_surfaces_override_refusal_reason(qapp):
    ctrl = FakeController()
    override = FakeSetpointOverride(fail_message=_REFUSAL_MESSAGE)
    hub = FakeHub(controller=ctrl, setpoint_override=override)
    screen = PidTuningScreen(hub)

    screen._enable_override()
    screen._disable_override()

    assert screen._override_active is False
    assert _REFUSAL_MESSAGE in screen._notice.text()
    assert screen._notice.text().startswith("PID tuning:")


# --- ARUCO log start/stop/record/clear wiring (test_aruco_log_routes_control_logger) -------------


def test_pilot_screen_aruco_toggle_records_and_clears_log(qapp):
    logger = ArucoPipelineLogger()
    hub = FakeHub(aruco_logger=logger)
    screen = PilotScreen(hub)

    # Start
    screen._on_aruco_toggle()
    assert screen._aruco_badge.text() == "ON"
    assert screen._btn_aruco_toggle.text() == "Stop"

    # Record a sighting and render the resulting snapshot (mirrors GET /api/aruco-log).
    log = logger.record_visible([{"id": 5, "center": (10, 10)}])
    screen._render_aruco(log)
    assert screen._aruco_log.count() == 1
    assert screen._aruco_log.item(0).text() == "ID 5"

    # Clear
    screen._on_aruco_clear()
    assert screen._aruco_log.count() == 0

    # Stop
    screen._on_aruco_toggle()
    assert screen._aruco_badge.text() == "OFF"
    assert screen._btn_aruco_toggle.text() == "Start"


# --- logs screen (LogStreamReceiver frontend) -----------------------------------------------


class FakeLogStream:
    """Duck-types LogStreamReceiver.get_recent() -- a plain in-memory ring buffer."""

    def __init__(self):
        self._entries = []

    def push(self, level, message, ts=None):
        self._entries.append(
            {"ts": ts if ts is not None else float(len(self._entries)), "level": level, "message": message}
        )

    def get_recent(self, limit=100):
        return list(self._entries[-limit:])


def test_logs_screen_disables_controls_when_log_stream_unavailable(qapp):
    hub = FakeHub()
    hub.log_stream = None
    screen = LogsScreen(hub)

    assert screen._view.isEnabled() is False
    for widget in screen._toolbar_widgets:
        assert widget.isEnabled() is False
    assert "unavailable" in screen._notice.text().lower()


def test_logs_screen_incremental_append_does_not_duplicate_rows(qapp):
    stream = FakeLogStream()
    stream.push("I", "boot complete")
    stream.push("W", "low voltage")
    hub = FakeHub()
    hub.log_stream = stream
    screen = LogsScreen(hub)

    # First poll renders everything present so far.
    screen._on_recent(screen._read_recent())
    first_pass_text = screen._view.toPlainText()
    assert "boot complete" in first_pass_text
    assert "low voltage" in first_pass_text

    # Second poll with no new entries must not re-render or duplicate rows.
    screen._on_recent(screen._read_recent())
    assert screen._view.toPlainText() == first_pass_text

    # Third poll with one genuinely new entry appends only that row.
    stream.push("E", "thruster fault")
    screen._on_recent(screen._read_recent())
    final_text = screen._view.toPlainText()
    assert final_text.count("boot complete") == 1
    assert final_text.count("low voltage") == 1
    assert final_text.count("thruster fault") == 1


# --- connection screen resource strip (surfaces lib/resource_receiver.py's get_stats()) ------


class FakeResourceReceiver:
    """Duck-types ResourceReceiver.get_stats() with a canned, mutable payload."""

    def __init__(self, stats):
        self.stats = stats

    def get_stats(self):
        return self.stats


def test_connection_screen_resource_strip_disabled_without_resource_service(qapp):
    hub = FakeHub()  # resource=None -- degrades like a build with no MCU link.
    screen = ConnectionScreen(hub)

    assert screen._resource_box.isEnabled() is False
    assert screen._cpu_value.text() == "--"
    assert screen._heap_value.text() == "--"
    assert screen._rx_errors_value.text() == "--"
    assert screen._crc_errors_value.text() == "--"


def test_connection_screen_resource_strip_renders_stats_and_flags_rising_udp_errors(qapp):
    stats = {
        "packet_count": 10,
        "crc_errors": 0,
        "packets_lost": 0,
        "last_seq": 9,
        "last_data": {
            "cpu_percent": 42,
            "heap_used_percent": 30,
            "heap_free_kb": 70,
            "heap_total_kb": 100,
            "thread_count": 6,
            "udp_rx_count": 100,
            "udp_rx_errors": 0,
        },
        "last_age_ms": 50.0,
        "last_addr": ["10.77.0.2", 12346],
    }
    resource = FakeResourceReceiver(stats)
    hub = FakeHub(resource=resource)
    screen = ConnectionScreen(hub)

    # A genuinely clean link -- every counter zero -- is the only thing that may read "OK".
    screen._on_proof(hub.connection_proof())
    assert screen._resource_box.isEnabled() is True
    assert screen._cpu_value.text() == "42%"
    assert screen._heap_value.text() == "70/100 KB (30% used)"
    assert screen._rx_errors_value.text() == "0"
    assert screen._crc_errors_value.text() == "0"
    assert screen._resource_badge.text() == "OK"

    # Rising UDP RX errors is the early warning this strip exists for -- it must read as a
    # problem (badge + colored value), not just a bigger neutral number.
    stats["last_data"]["udp_rx_errors"] = 5
    screen._on_proof(hub.connection_proof())

    assert screen._rx_errors_value.text() == "5"
    assert screen._resource_badge.text() == "RISING"

    # A counter that climbed and then plateaued is still a degraded link. Since "rising" only
    # compares against the previous poll, this is the tick where the badge would otherwise fall
    # back to green "OK" while sitting next to a nonzero error count.
    screen._on_proof(hub.connection_proof())

    assert screen._rx_errors_value.text() == "5"
    assert screen._resource_badge.text() == "ERRORS"


# --- config screen axis-config post-send liveness probe (UDP 5004 has no ACK) -------------------
# The probe cannot confirm the axis remap was applied -- only a physical test of the vehicle can
# do that -- but it must say whether the 9DOF stream (UDP 5002) is still producing fresh samples.


class FakeImu:
    """Duck-types IMUReceiver.get_stats() -- only the packet_count field the probe reads."""

    def __init__(self, packet_count):
        self.packet_count = packet_count

    def get_stats(self):
        return {"packet_count": self.packet_count}


def test_config_screen_axis_probe_reports_no_imu_data(qapp):
    hub = FakeHub()
    screen = ConfigScreen(hub)
    imu = FakeImu(packet_count=5)

    # No packets arrived since the baseline was taken -- the probe must say so plainly, and
    # never claim the remap itself was verified.
    screen._finish_axis_probe(screen._axes_feedback, "Mapping saved", imu, baseline=5)

    feedback_text = screen._axes_feedback.text()
    assert "NO IMU DATA" in feedback_text
    assert "verified" not in feedback_text.lower()
    assert "NO IMU DATA" in screen._notice.text()


def test_config_screen_axis_probe_reports_imu_alive(qapp):
    hub = FakeHub()
    screen = ConfigScreen(hub)
    imu = FakeImu(packet_count=10)

    screen._finish_axis_probe(screen._axes_feedback, "Mapping saved", imu, baseline=5)

    assert "IMU stream alive" in screen._axes_feedback.text()
    assert "verified" not in screen._axes_feedback.text().lower()


def test_config_screen_axis_probe_degrades_without_imu_receiver(qapp):
    hub = FakeHub()  # imu=None -- degrades like a build with the IMU receiver unavailable.
    screen = ConfigScreen(hub)

    screen._start_axis_probe(screen._offset_feedback, "Offset saved")

    assert "no IMU receiver available" in screen._offset_feedback.text()


# --- panel lifecycle: a tile you cannot see must cost nothing ------------------------------------
# The trap this guards: a panel scrolled out of the canvas viewport is still isVisible() as far as
# Qt is concerned, so showEvent/hideEvent alone would leave it polling forever. PanelBase.bind_host()
# hands activation to the host's visibilityChanged instead, which GridCanvas drives from the
# viewport. (Under the old dock shell the same signal came from a dock tabbed behind another.)


class _CountingPanel(PanelBase):
    title = "Counting"

    def __init__(self, hub, key="panel.probe", parent=None):
        super().__init__(hub, parent)
        self.activations = 0
        self.deactivations = 0
        self.poller = self.watch(key, lambda: None, 100, self._on_sample)

    def _on_sample(self, payload):
        pass

    def on_activate(self):
        self.activations += 1

    def on_deactivate(self):
        self.deactivations += 1


def _canvas(qapp, height=400):
    """A shown canvas with a known viewport, so 'scrolled out of view' is deterministic."""
    canvas = GridCanvas()
    canvas.resize(800, height)
    canvas.show()
    qapp.processEvents()
    return canvas


def _tile(canvas, panel, title, placement=None):
    """add_tile binds the panel to the tile itself; doing it here would be too late."""
    tile = canvas.add_tile(title, panel, placement)
    tile.setObjectName(f"tile.{title}")
    return tile


def test_panel_scrolled_out_of_the_viewport_stops_polling(qapp):
    canvas = _canvas(qapp, height=300)
    hub = FakeHub()
    near = _CountingPanel(hub, key="panel.near")
    far = _CountingPanel(hub, key="panel.far")
    # ROW_HEIGHT is 44, so row 40 is ~1760px down: far below a 300px viewport.
    _tile(canvas, near, "near", Placement(0, 0, 6, 4))
    _tile(canvas, far, "far", Placement(0, 40, 6, 4))
    qapp.processEvents()

    # `far` is still isVisible() as far as Qt is concerned -- that is exactly why showEvent/
    # hideEvent is not enough and the canvas has to drive visibility from its viewport.
    assert near._active is True
    assert far._active is False
    assert near.poller._subscribers == 1
    assert far.poller._subscribers == 0

    canvas.verticalScrollBar().setValue(canvas.verticalScrollBar().maximum())
    qapp.processEvents()

    assert far._active is True
    assert far.poller._subscribers == 1
    assert near.poller._subscribers == 0
    canvas.close()


def test_panel_teardown_returns_poller_refcount_to_zero(qapp):
    canvas = _canvas(qapp)
    hub = FakeHub()
    panel = _CountingPanel(hub)
    _tile(canvas, panel, "solo", Placement(0, 0, 6, 4))
    qapp.processEvents()
    assert panel.poller._subscribers == 1

    panel.teardown()

    # Without teardown the refcount would stay at 1 and the shared timer would run forever.
    assert panel.poller._subscribers == 0
    assert panel.poller.updated.slots == []
    canvas.close()


def test_teardown_leaves_a_sibling_sharing_the_same_poller_connected(qapp):
    """Both panels watch one key, so hub.poller() hands them the same object."""
    hub = FakeHub()
    keeper = _CountingPanel(hub, key="panel.shared")
    goer = _CountingPanel(hub, key="panel.shared")
    assert keeper.poller is goer.poller
    assert len(keeper.poller.updated.slots) == 2

    goer.teardown()

    # A bare updated.disconnect() would have severed the keeper's slot too.
    assert keeper.poller.updated.slots == [keeper._on_sample]


def test_set_active_cascades_to_nested_child_panels(qapp):
    hub = FakeHub()
    parent = _CountingPanel(hub, key="panel.parent")
    box = QGroupBox(parent)  # nesting makes the child a grandchild, not a direct child
    child = _CountingPanel(hub, key="panel.child", parent=box)

    parent.set_active(True)

    assert child._active is True
    assert child.poller._subscribers == 1

    parent.set_active(False)

    assert child._active is False
    assert child.poller._subscribers == 0


def test_child_of_a_hosted_panel_does_not_self_activate_on_show(qapp):
    """A child built after bind_host() must still defer to the tile, not to its own showEvent."""
    canvas = _canvas(qapp, height=200)
    hub = FakeHub()
    parent = _CountingPanel(hub, key="panel.host")
    # Placed off the bottom of the viewport, so the host reports it as not visible.
    _tile(canvas, parent, "host", Placement(0, 40, 6, 4))
    child = _CountingPanel(hub, key="panel.hosted-child", parent=parent)
    child.show()
    qapp.processEvents()

    assert child._host_managed() is True
    assert child._active is False
    assert child.poller._subscribers == 0

    canvas.verticalScrollBar().setValue(canvas.verticalScrollBar().maximum())
    qapp.processEvents()

    assert child._active is True
    canvas.close()


def test_unhosted_panel_still_activates_on_show(qapp):
    """Screens constructed directly -- in a plain layout or a test -- keep the old behaviour."""
    hub = FakeHub()
    panel = _CountingPanel(hub, key="panel.unhosted")
    panel.show()
    qapp.processEvents()

    assert panel._active is True
    assert panel.poller._subscribers == 1

    panel.hide()
    qapp.processEvents()

    assert panel._active is False
    assert panel.poller._subscribers == 0


# --- workspace shell ----------------------------------------------------------------------------
# Shutdown ownership and single-instance enforcement are the two things here that turn a UI bug
# into a hardware bug, so they are asserted directly rather than through a screen.


class _ShellHub(FakeHub):
    """FakeHub plus the surface Shell itself touches."""


def _shell(qapp):
    from desktop.shell import Shell

    shell = Shell(_ShellHub(), app=qapp)
    return shell


def test_a_new_workspace_starts_empty(qapp):
    """No default arrangement: the operator builds one out of the Components menu."""
    shell = _shell(qapp)

    window = shell.load_empty()

    assert len(shell.windows) == 1
    assert window.tiles == []
    window.close()


def test_opening_components_tiles_them_without_overlapping(qapp):
    """The dock shell put every component full-width in one column; the grid must not."""
    shell = _shell(qapp)
    window = shell.new_window()
    window.resize(1280, 860)
    qapp.processEvents()

    for component_id in ("panel.pilot.depth", "panel.pilot.lights", "panel.home.branch"):
        shell.open_component(registry.BY_ID[component_id], window)
    qapp.processEvents()

    placements = [tile.placement for tile in window.tiles]
    assert len(placements) == 3
    for i, first in enumerate(placements):
        for second in placements[i + 1 :]:
            assert not grid.intersects(first, second)
    # At least two of them fit side by side -- the whole point of the change.
    assert len({p.row for p in placements}) < len(placements)
    window.close()


def test_closing_a_tile_removes_the_component_and_releases_its_pollers(qapp):
    shell = _shell(qapp)
    window = shell.new_window()
    record = shell.open_component(registry.BY_ID["screen.logs"], window)
    qapp.processEvents()

    record["tile"].request_close()
    qapp.processEvents()

    assert shell.instances["screen.logs"] == []
    assert window.tiles == []
    # Reopening must work cleanly, which is what the X button removing it for real buys.
    assert shell.open_component(registry.BY_ID["screen.logs"], window) is not None
    window.close()


def test_single_instance_component_is_refused_and_brought_to_front(qapp):
    shell = _shell(qapp)
    window = shell.new_window()
    pilot = registry.BY_ID["screen.pilot"]

    first = shell.open_component(pilot, window)
    second_window = shell.new_window()
    refused = shell.open_component(pilot, second_window)

    assert first is not None
    assert refused is None
    assert len(shell.instances["screen.pilot"]) == 1
    assert "already open elsewhere" in second_window.statusBar().currentMessage()
    assert "scrolled into view" in second_window.statusBar().currentMessage()
    second_window.close()
    window.close()


def test_duplicable_component_can_be_opened_twice(qapp):
    shell = _shell(qapp)
    window = shell.new_window()
    graphs = registry.BY_ID["screen.graphs"]

    first = shell.open_component(graphs, window)
    second = shell.open_component(graphs, shell.new_window())

    assert first is not None and second is not None
    assert len(shell.instances["screen.graphs"]) == 2
    # Distinct object names are what lets a preset restore both.
    assert first["tile"].objectName() != second["tile"].objectName()
    for w in list(shell.windows):
        w.close()


def test_hub_shuts_down_only_when_the_last_window_closes(qapp):
    shell = _shell(qapp)
    first = shell.new_window()
    second = shell.new_window()

    first.close()

    # The old MainWindow.closeEvent called hub.shutdown() unconditionally -- with two windows
    # that would have killed every service while the second window was still driving them.
    assert shell.hub.shutdown_calls == 0

    second.close()

    assert shell.hub.shutdown_calls == 1


def test_reveal_brings_an_open_component_to_front_instead_of_duplicating(qapp):
    shell = _shell(qapp)
    window = shell.new_window()
    logs = registry.BY_ID["screen.logs"]
    opened = shell.open_component(logs, window)

    revealed = shell.reveal("Logs", window)

    assert revealed is opened
    assert len(shell.instances["screen.logs"]) == 1
    window.close()


def test_reveal_opens_a_component_that_is_not_yet_placed(qapp):
    shell = _shell(qapp)
    window = shell.new_window()

    revealed = shell.reveal("Config", window)

    assert revealed is not None
    assert len(shell.instances["screen.config"]) == 1
    window.close()


# --- workspace presets --------------------------------------------------------------------------


@pytest.fixture
def workspace_store(tmp_path, monkeypatch):
    """Point the preset file at a throwaway dir so tests never touch the real data directory."""
    from desktop import logic
    from lib.json_data_handler import JSONDataHandler

    monkeypatch.setattr(logic, "workspace_handler", JSONDataHandler(file_path=tmp_path / "workspaces.json"))
    return logic


def test_capture_and_reload_preset_round_trips_a_two_window_layout(qapp, workspace_store):
    shell = _shell(qapp)
    first = shell.new_window()
    shell.open_component(registry.BY_ID["screen.pilot"], first)
    shell.open_component(registry.BY_ID["screen.graphs"], first)
    shell.open_component(registry.BY_ID["screen.graphs"], first)  # duplicable -> two instances
    second = shell.new_window()
    shell.open_component(registry.BY_ID["screen.logs"], second)
    qapp.processEvents()
    captured = {r["tile"].objectName(): _cells(r["tile"]) for rs in shell.instances.values() for r in rs}

    ok, _message = workspace_store.save_workspace_preset("Dive rig", shell.capture_preset())
    assert ok
    assert shell.load_preset("Classic")
    qapp.processEvents()
    assert len(shell.windows) == 1

    assert shell.load_preset("Dive rig")
    qapp.processEvents()

    assert len(shell.windows) == 2
    assert len(shell.instances["screen.graphs"]) == 2
    assert len(shell.instances["screen.pilot"]) == 1
    # Distinct object names are what let a preset tell the two copies apart.
    names = {r["tile"].objectName() for r in shell.instances["screen.graphs"]}
    assert len(names) == 2
    # Cells, not just membership: a layout that reloads in different places is not restored.
    restored = {r["tile"].objectName(): _cells(r["tile"]) for rs in shell.instances.values() for r in rs}
    assert restored == captured
    for window in list(shell.windows):
        window.close()


def _cells(tile):
    p = tile.placement
    return (p.col, p.row, p.cols, p.rows)


def test_preset_without_cells_still_opens_its_components(qapp, workspace_store):
    """A version-1 preset (the dock shell's) carries no cells. It must auto-place, not fail."""
    shell = _shell(qapp)
    v1 = {"version": 1, "windows": [{"components": [{"id": "screen.logs"}, {"id": "screen.home"}]}]}
    workspace_store.save_workspace_preset("Old", v1)

    assert shell.load_preset("Old")
    qapp.processEvents()

    assert len(shell.windows[0].tiles) == 2
    placements = [t.placement for t in shell.windows[0].tiles]
    assert not grid.intersects(placements[0], placements[1])
    for window in list(shell.windows):
        window.close()


def test_preset_naming_a_removed_component_still_loads(qapp, workspace_store):
    """A layout saved before a screen was decomposed must degrade, not crash."""
    shell = _shell(qapp)
    stale = {"windows": [{"components": [{"id": "screen.pilot"}, {"id": "screen.gone"}]}]}
    workspace_store.save_workspace_preset("Stale", stale)

    assert shell.load_preset("Stale")
    qapp.processEvents()

    assert len(shell.instances["screen.pilot"]) == 1
    assert "screen.gone" not in shell.instances
    for window in list(shell.windows):
        window.close()


def test_unknown_preset_falls_back_to_an_empty_workspace_rather_than_no_windows(qapp, workspace_store):
    """The saved 'last layout' can name a preset the operator has since deleted."""
    shell = _shell(qapp)

    assert shell.load_preset("never saved") is False

    assert len(shell.windows) == 1
    assert shell.windows[0].tiles == []
    for window in list(shell.windows):
        window.close()


def test_no_saved_layout_opens_an_empty_workspace(qapp, workspace_store):
    """A fresh install: load_last_workspace() returns "" and must still leave one window up."""
    shell = _shell(qapp)

    assert workspace_store.load_last_workspace() == ""
    assert shell.load_preset(workspace_store.load_last_workspace()) is False

    assert len(shell.windows) == 1
    assert shell.windows[0].tiles == []
    for window in list(shell.windows):
        window.close()


def test_classic_preset_stacks_the_ten_screens_full_width(qapp, workspace_store):
    """Classic is no longer the default, but it must still load — and not overlap."""
    shell = _shell(qapp)

    assert shell.load_preset("Classic")
    qapp.processEvents()

    tiles = shell.windows[0].tiles
    assert len(tiles) == len(registry.SCREEN_COMPONENTS)
    assert all(tile.placement.cols == grid.COLUMNS for tile in tiles)
    rows = sorted(tile.placement.row for tile in tiles)
    assert rows == sorted(set(rows))  # stacked, one per band
    for window in list(shell.windows):
        window.close()


def test_classic_preset_is_reserved(qapp, workspace_store):
    ok, message = workspace_store.save_workspace_preset("Classic", {"windows": []})
    assert ok is False
    assert "built in" in message

    ok, message = workspace_store.delete_workspace_preset("Classic")
    assert ok is False
    assert "Classic" in workspace_store.load_workspace_presets()


# --- decomposed panels: Graphs is the worked reference -------------------------------------------


def test_graph_charts_share_one_poller_and_release_it_together(qapp):
    """Six charts on one poller key is what makes duplicating a chart into another dock free."""
    from desktop.screens.graphs import GraphsScreen

    hub = FakeHub()
    screen = GraphsScreen(hub)
    poller = hub.pollers["graphs.sample"]

    assert len(poller.updated.slots) == 6

    screen.set_active(True)
    assert poller._subscribers == 6

    screen.set_active(False)
    assert poller._subscribers == 0


def test_chart_panel_narrows_a_shared_sample_to_its_own_channel(qapp):
    from desktop.screens.graphs import ChartPanel

    hub = FakeHub()
    yaw = ChartPanel(hub, "yaw", "Yaw", "deg", "#0dcaf0", True)
    roll_rate = ChartPanel(hub, "rr", "Roll Rate", "deg/s", "#198754", False)
    yaw.set_active(True)
    roll_rate.set_active(True)

    hub.pollers["graphs.sample"].updated.emit({"imu": {"yaw": 12.5, "rr": -2.0}, "setpoint": {"yaw": 10.0}})

    _xs, ys, setpoints = yaw.samples()
    assert ys == [12.5]
    assert setpoints == [10.0]
    _xs, rate_ys, rate_sp = roll_rate.samples()
    assert rate_ys == [-2.0]
    assert rate_sp == []  # rate charts carry no PID setpoint at all


def test_read_only_screen_keeps_its_composite_component(qapp):
    """Graphs has no hardware writer, so the all-six grid stays openable next to single charts."""
    assert "screen.graphs" in registry.BY_ID
    chart_ids = [c.id for c in registry.COMPONENTS if c.category == "Graphs"]
    assert len(chart_ids) == 6
    assert all(registry.BY_ID[cid].duplicable for cid in chart_ids)


# --- composite/panel single-instance guard --------------------------------------------------------
# The hazard: a screen and one of its own panels have DIFFERENT component ids, so an id-only guard
# would happily give you two live widgets driving the same actuator. Component.owns closes that.


def test_opening_a_screen_reserves_its_writer_panels(qapp):
    shell = _shell(qapp)
    window = shell.new_window()

    shell.open_component(registry.BY_ID["screen.pilot"], window)
    refused = shell.open_component(registry.BY_ID["panel.pilot.manipulator"], window)

    assert refused is None
    assert "panel.pilot.manipulator" not in shell.instances or not shell.instances["panel.pilot.manipulator"]
    assert "already open in" in window.statusBar().currentMessage()
    window.close()


def test_opening_a_writer_panel_reserves_its_parent_screen(qapp):
    """The guard has to work in both directions, not just screen-first."""
    shell = _shell(qapp)
    window = shell.new_window()

    shell.open_component(registry.BY_ID["panel.pilot.manipulator"], window)
    refused = shell.open_component(registry.BY_ID["screen.pilot"], window)

    assert refused is None
    assert not shell.instances.get("screen.pilot")
    window.close()


def test_read_only_screen_and_its_panels_coexist(qapp):
    """Nothing in Graphs writes, so the grid and a single chart may be open at once."""
    shell = _shell(qapp)
    window = shell.new_window()

    grid = shell.open_component(registry.BY_ID["screen.graphs"], window)
    chart = shell.open_component(registry.BY_ID["panel.graphs.yaw"], window)

    assert grid is not None
    assert chart is not None
    window.close()


def test_component_ids_are_unique(qapp):
    ids = [c.id for c in registry.COMPONENTS]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_every_non_duplicable_panel_is_owned_by_a_screen(qapp):
    """A writer panel nobody claims would be openable alongside its screen, unguarded."""
    owned = {claim for c in registry.SCREEN_COMPONENTS for claim in c.owns}
    orphans = [
        c.id
        for c in registry.COMPONENTS
        if c.id.startswith("panel.") and not c.duplicable and c.id not in owned and c.category != "Workspace"
    ]
    assert orphans == [], orphans


def test_owns_only_references_real_components(qapp):
    unknown = [claim for c in registry.COMPONENTS for claim in c.owns if claim not in registry.BY_ID]
    assert unknown == [], unknown
