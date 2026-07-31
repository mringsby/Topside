"""Screen-level behaviour that only exists once real Qt widgets are involved.

Everything here needs an (offscreen) `QApplication` because it asserts on widget state (badge
text, list contents, notice messages) rather than on a plain-method return value. Where the same
behaviour could be exercised without Qt, it lives in `test_desktop_services.py` instead.

Never imports `routes`, `app`, or `flask` -- only `desktop.*` and `lib.*`.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

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


class _StubPoller:
    """Duck-types desktop.services.Poller without ever starting a QTimer or hitting hardware."""

    class _Signal:
        def connect(self, _slot):
            pass

    updated = _Signal()

    def subscribe(self):
        pass

    def unsubscribe(self):
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
        self.imu = imu
        self.neutralize_calls = 0

    def poller(self, key, fn, interval_ms):
        return _StubPoller()

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
