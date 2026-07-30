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

from desktop.screens.debug import DebugScreen
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
    def __init__(self):
        self.clear_count = 0

    def clear_override(self):
        self.clear_count += 1
        return {"active": False, "axes": {}}


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

    def __init__(self, controller=None, setpoint_override=None, aruco_logger=None):
        self.controller = controller
        self.setpoint_override = setpoint_override
        self.aruco_logger = aruco_logger
        self.ip_camera = None
        self.bitmask = None
        self.resource = None
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
