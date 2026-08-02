"""ServiceHub-bound logic and non-Qt screen "work" methods.

Ports the hub-dependent behaviour from the dying route tests
(`test_pid_runtime_routes.py`, `test_manipulator_api.py`, `test_ip_camera_routes.py`).

No real `ServiceHub` is ever constructed (it binds UDP sockets, opens cameras, starts threads).
Instead we call `ServiceHub`/`PidTuningScreen` methods directly against small fake stand-ins,
exactly like the old route tests stuffed `Fake*` objects into `app.config`.

`PidTuningScreen._start_pid_work` / `_kill_work` / `_rearm_work` / `_send_setpoints_work` /
`_clear_axis_work` / `_stop_pid_work` only ever touch `self.hub` — never a Qt widget — so they can
be exercised on a screen created via `object.__new__` (skipping `__init__`, i.e. no widget tree is
ever built). That means this file needs no `QApplication` at all.
"""

from types import SimpleNamespace

import pytest

from desktop import logic, services
from desktop.screens.pid_tuning import PidTuningScreen
from lib.json_data_handler import JSONDataHandler

CONTROL_AXES = logic.CONTROL_AXES
PID_AXES = list(logic.ATTITUDE_AXES) + list(logic.TRANSLATIONAL_AXES)


# --- Fakes lifted from the dying route tests ----------------------------------------------------


class FakeController:
    def __init__(self):
        self.killed = False
        self.pid_enabled = False
        self.setpoints = {}
        self.rates = {"roll": 90.0, "pitch": 90.0, "yaw": 90.0}
        self.gains = {"master": 1.0, "axes": {axis: 1.0 for axis in CONTROL_AXES}}
        self._manip_state = {
            "setpoint_deg": 0.0,
            "setpoint_norm": 0.0,
            "source": "neutral",
            "updated_at": 100.0,
        }

    def get_control_state(self):
        return {
            "killed": self.killed,
            "pid_enabled": self.pid_enabled,
            "pid_setpoints": dict(self.setpoints),
            "active_setpoints": dict(self.setpoints) if self.pid_enabled else {},
            "control_path": "KILLED" if self.killed else "PS4",
            "override_active": False,
            "manual_command_before_pid": {},
            "topside_command": {},
            "controller_gains": self.gains,
        }

    def is_killed(self):
        return self.killed

    def is_pid_enabled(self):
        return self.pid_enabled

    def kill(self):
        self.killed = True
        self.pid_enabled = False
        self.setpoints = {}
        return self.get_control_state()

    def rearm(self):
        self.killed = False
        self.pid_enabled = False
        self.setpoints = {}
        return self.get_control_state()

    def set_debug_override(self, axes):
        return not self.killed

    def clear_debug_override(self):
        pass

    def start_pid(self, setpoints):
        if self.killed:
            return None
        self.pid_enabled = True
        self.setpoints = dict(setpoints)
        return dict(self.setpoints)

    def stop_pid(self, clear=True):
        self.pid_enabled = False
        if clear:
            self.setpoints = {}
        return self.get_control_state()

    def set_pid_setpoints(self, setpoints):
        if self.killed:
            return None
        self.setpoints.update(setpoints)
        return dict(self.setpoints)

    def clear_pid_setpoint(self, axis):
        self.setpoints.pop(axis, None)
        if self.pid_enabled and not self.setpoints:
            self.pid_enabled = False
        return dict(self.setpoints)

    def get_pid_setpoints(self):
        return dict(self.setpoints)

    def set_controller_gains(self, gains):
        self.gains = {"master": gains["master"], "axes": dict(gains["axes"])}
        return {"master": self.gains["master"], "axes": dict(self.gains["axes"])}

    def get_controller_gains(self):
        return {"master": self.gains["master"], "axes": dict(self.gains["axes"])}

    def set_manipulator(self, setpoint_deg, source="gui"):
        deg = max(-50.0, min(50.0, float(setpoint_deg)))
        self._manip_state = {
            "setpoint_deg": deg,
            "setpoint_norm": deg / 50.0,
            "source": source,
            "updated_at": 100.0,
        }
        return dict(self._manip_state)

    def get_manipulator(self):
        return dict(self._manip_state)


class FakeSetpointOverride:
    def __init__(self):
        self.sent = []
        self.clear_count = 0
        self.error = None

    def clear_override(self):
        self.clear_count += 1
        return {"active": False, "axes": {}}

    def send_override(self, axes, replay_attempts=3, replay_delay=0.05):
        self.sent.append(dict(axes))
        return {"active": True, "axes": dict(axes)}

    def get_state(self):
        return {"active": bool(self.sent), "axes": self.sent[-1] if self.sent else {}}

    def set_error(self, message):
        self.error = message


class FakeIMU:
    def __init__(self, age_ms=10, data=None):
        self.age_ms = age_ms
        self.data = data or {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}

    def get_stats(self):
        return {"age_ms": self.age_ms, "last_data": dict(self.data)}


class FakeTelemetry:
    def get_latest(self):
        return {"timestamp": 100.0, "manipulator": {"deg": 12.5, "pulse_us": 1625}}


class FakeIPCamera:
    def __init__(self, url):
        self.url = url
        self.stopped = False

    def stop(self):
        self.stopped = True

    def get_status(self):
        return {"connected": True, "url": self.url}


def make_pid_screen(controller=None, imu=None, override=None):
    """A PidTuningScreen with no Qt widgets ever built -- __init__ never runs."""
    screen = PidTuningScreen.__new__(PidTuningScreen)
    hub = SimpleNamespace(
        controller=controller if controller is not None else FakeController(),
        imu=imu if imu is not None else FakeIMU(),
        setpoint_override=override if override is not None else FakeSetpointOverride(),
    )
    # send_active_pid_setpoints is already-ported hub-bound logic (PARITY.md sec4) -- reuse the
    # real implementation bound to this fake hub instead of re-deriving its behaviour here.
    hub.send_active_pid_setpoints = lambda: services.ServiceHub.send_active_pid_setpoints(hub)
    screen.hub = hub
    return screen


# --- PID start sanity/force path (test_pid_start_returns_sanity_failure_then_allows_force) ------


def test_start_pid_work_sanity_failure_then_force():
    ctrl = FakeController()
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, imu=FakeIMU(age_ms=3000), override=override)

    result = screen._start_pid_work(force=False)
    assert result["ok"] is False
    assert result["force_supported"] is True
    assert ctrl.pid_enabled is False

    result = screen._start_pid_work(force=True)
    assert result["ok"] is True
    assert ctrl.pid_enabled is True
    assert result["setpoints"] == {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}
    assert override.sent[-1] == {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}


def test_start_pid_work_merges_saved_setpoints_with_current_imu():
    ctrl = FakeController()
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, override=override)

    screen._send_setpoints_work({"roll": 45.0})
    result = screen._start_pid_work(force=False)

    assert result["ok"] is True
    assert ctrl.pid_enabled is True
    assert result["setpoints"] == {"roll": 45.0, "pitch": 2.0, "yaw": 3.0}
    assert override.sent[-1] == {"roll": 45.0, "pitch": 2.0, "yaw": 3.0}


# --- setpoint save-vs-resend semantics -----------------------------------------------------------


def test_send_setpoints_work_saves_without_starting_pid_or_sending_override():
    ctrl = FakeController()
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, override=override)

    result = screen._send_setpoints_work({"roll": 45.0})

    assert result["ok"] is True
    assert result["pid_active"] is False
    assert ctrl.pid_enabled is False
    assert ctrl.setpoints == {"roll": 45.0}
    assert override.sent == []


def test_send_setpoints_work_updates_override_when_pid_active():
    ctrl = FakeController()
    ctrl.start_pid({"roll": 10.0})
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, override=override)

    result = screen._send_setpoints_work({"yaw": -20.0})

    assert result["ok"] is True
    assert result["pid_active"] is True
    assert ctrl.setpoints == {"roll": 10.0, "yaw": -20.0}
    assert override.sent[-1] == {"roll": 10.0, "yaw": -20.0}


def test_stop_pid_work_can_keep_saved_setpoints():
    ctrl = FakeController()
    ctrl.start_pid({"roll": 10.0, "pitch": 20.0})
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, override=override)

    result = screen._stop_pid_work(clear=False)

    assert ctrl.pid_enabled is False
    assert ctrl.setpoints == {"roll": 10.0, "pitch": 20.0}
    assert override.clear_count == 1
    assert result["state"]["pid_setpoints"] == {"roll": 10.0, "pitch": 20.0}


# --- per-axis setpoint clearing (test_clear_pid_axis_clears_all_then_resends_remaining) ---------


def test_clear_axis_work_resends_remaining_setpoints():
    ctrl = FakeController()
    ctrl.start_pid({"roll": 10.0, "pitch": 20.0, "yaw": 30.0})
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, override=override)

    result = screen._clear_axis_work("roll")

    assert result["ok"] is True
    assert result["remaining"] == {"pitch": 20.0, "yaw": 30.0}
    assert override.clear_count == 1
    assert override.sent[-1] == {"pitch": 20.0, "yaw": 30.0}


# --- killswitch zeroes MCU PID gains (test_killswitch_zeroes_pid_gains_and_blocks_debug_override) -


def test_kill_work_zeroes_mcu_pid_gains(monkeypatch):
    captured = {}

    def fake_send_pid_gains(gains, timeout=1.0, max_retries=3):
        captured["gains"] = gains
        captured["timeout"] = timeout
        captured["max_retries"] = max_retries
        return gains, 1

    monkeypatch.setattr("desktop.screens.pid_tuning.send_pid_gains", fake_send_pid_gains)

    ctrl = FakeController()
    override = FakeSetpointOverride()
    screen = make_pid_screen(controller=ctrl, override=override)

    result = screen._kill_work()

    assert result["state"]["killed"] is True
    assert captured["gains"] == {axis: {"kp": 0.0, "ki": 0.0, "kd": 0.0} for axis in PID_AXES}
    assert captured["timeout"] == 0.5
    assert captured["max_retries"] == 2
    assert override.clear_count == 1

    # Rearm brings the controller back out of the killed state.
    rearm_result = screen._rearm_work()
    assert rearm_result["state"]["killed"] is False


# --- controller gain clamping + controller update (test_controller_gains_api_clamps_and_updates) -


def test_save_controller_gains_clamps_and_updates_controller(monkeypatch, tmp_path):
    config_handler = JSONDataHandler(file_path=tmp_path / "config.json")
    monkeypatch.setattr(logic, "config_handler", config_handler)

    ctrl = FakeController()
    hub = SimpleNamespace(controller=ctrl)

    gains = services.ServiceHub.save_controller_gains(hub, {"master": 1.4, "axes": {"surge": 0.25, "yaw": -1}})

    assert gains["master"] == 1.0
    assert gains["axes"]["surge"] == 0.25
    assert gains["axes"]["yaw"] == 0.0
    assert ctrl.gains == gains


# --- manipulator target/applied readback (test_manipulator_api_returns_target_and_applied_values) -


def test_manipulator_payload_reports_target_and_applied():
    hub = SimpleNamespace(controller=FakeController(), control_telem=FakeTelemetry())

    payload = services.ServiceHub.manipulator_payload(hub)

    assert payload["target_deg"] == 0.0
    assert payload["applied_deg"] == 12.5
    assert payload["pulse_us"] == 1625


def test_manipulator_payload_reflects_clamped_target_after_set():
    """Mirrors POST /api/manipulator's ctrl.set_manipulator(80, source="gui") + readback."""
    ctrl = FakeController()
    hub = SimpleNamespace(controller=ctrl, control_telem=FakeTelemetry())

    ctrl.set_manipulator(80, source="gui")
    payload = services.ServiceHub.manipulator_payload(hub)

    assert payload["target_deg"] == 50.0
    assert payload["source"] == "gui"


# --- pilot screen ARUCO wiring degrades gracefully without a logger -----------------------------


def test_pilot_screen_aruco_fn_returns_none_without_logger():
    """Mirrors the route's 503 'ARUCO logger unavailable' case -- desktop degrades instead of
    erroring, matching the ServiceHub-degradation contract in PARITY.md."""
    from desktop.screens.pilot import PilotScreen

    screen = PilotScreen.__new__(PilotScreen)
    screen.hub = SimpleNamespace(aruco_logger=None)

    assert screen._aruco_fn() is None


# --- IP camera reassign teardown/rebuild (test_ip_camera_reassign_restarts_fake_receiver) -------


class _FakeIpCameraHub:
    """Only the attributes `ServiceHub.reassign_ip_camera`/`camera_status` touch."""

    camera_status = services.ServiceHub.camera_status

    def __init__(self, camera):
        self.ip_camera = camera
        self.ip_camera_settings = {
            "out_width": 320,
            "out_height": 240,
            "jpeg_quality": 70,
            "flip_180": False,
        }
        self.ip_camera_active_ip = "10.77.0.4"
        self.ip_camera_active_url = camera.url
        self.aruco_logger = None


def test_reassign_ip_camera_tears_down_and_rebuilds(monkeypatch, tmp_path):
    config_handler = JSONDataHandler(file_path=tmp_path / "config.json")
    monkeypatch.setattr(logic, "config_handler", config_handler)

    old_camera = FakeIPCamera("rtsp://10.77.0.4:554/stream1")
    created = []

    def fake_init_ip_camera(**kwargs):
        camera = FakeIPCamera(kwargs["url"])
        created.append((camera, kwargs))
        return camera

    monkeypatch.setattr(services, "init_ip_camera", fake_init_ip_camera)

    hub = _FakeIpCameraHub(old_camera)

    active_ip, active_url, status = services.ServiceHub.reassign_ip_camera(hub, "10.77.0.9")

    assert active_ip == "10.77.0.9"
    assert active_url == "rtsp://10.77.0.9:554/stream1"
    assert status == {"connected": True, "url": "rtsp://10.77.0.9:554/stream1"}
    assert old_camera.stopped is True
    assert len(created) == 1
    assert created[0][1]["url"] == "rtsp://10.77.0.9:554/stream1"
    assert created[0][1]["out_width"] == 320
    assert hub.ip_camera is created[0][0]

    # Persisted so a restart comes back up on the reassigned IP.
    assert logic.get_ip_camera_config()["active_ip"] == "10.77.0.9"


# --- theme persistence (test_theme_load_defaults_and_round_trips) -------------------------------


def test_load_theme_defaults_to_dark_when_section_absent(monkeypatch, tmp_path):
    config_handler = JSONDataHandler(file_path=tmp_path / "config.json")
    monkeypatch.setattr(logic, "config_handler", config_handler)

    assert logic.load_theme() == "dark"


def test_save_theme_and_load_theme_round_trip(monkeypatch, tmp_path):
    config_handler = JSONDataHandler(file_path=tmp_path / "config.json")
    monkeypatch.setattr(logic, "config_handler", config_handler)

    logic.save_theme("light")

    assert logic.load_theme() == "light"


def test_save_theme_rejects_unknown_name(monkeypatch, tmp_path):
    config_handler = JSONDataHandler(file_path=tmp_path / "config.json")
    monkeypatch.setattr(logic, "config_handler", config_handler)

    with pytest.raises(ValueError):
        logic.save_theme("solarized")


# --- workspace layout presets (Workspaces panel persistence) ------------------------------------


def test_save_workspace_preset_round_trips(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    preset = {"windows": [{"layout": "tabbed", "components": [{"id": "screen.home"}]}]}
    ok, message = logic.save_workspace_preset("My Layout", preset)

    assert ok is True
    assert "My Layout" in message
    assert logic.load_workspace_presets()["My Layout"] == preset


def test_load_workspace_presets_always_includes_classic(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    presets = logic.load_workspace_presets()

    assert logic.CLASSIC_PRESET_NAME in presets
    assert presets[logic.CLASSIC_PRESET_NAME] == logic.CLASSIC_PRESET


def test_save_workspace_preset_refuses_classic_name(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    ok, message = logic.save_workspace_preset(logic.CLASSIC_PRESET_NAME, {"windows": []})

    assert ok is False
    assert message
    assert logic.load_workspace_presets()[logic.CLASSIC_PRESET_NAME] == logic.CLASSIC_PRESET


def test_delete_workspace_preset_refuses_classic_name(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    ok, message = logic.delete_workspace_preset(logic.CLASSIC_PRESET_NAME)

    assert ok is False
    assert message
    assert logic.CLASSIC_PRESET_NAME in logic.load_workspace_presets()


def test_save_workspace_preset_rejects_invalid_name(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    ok, message = logic.save_workspace_preset("bad/name!", {"windows": []})

    assert ok is False
    assert message
    assert "bad/name!" not in logic.load_workspace_presets()


def test_delete_workspace_preset_unknown_name_returns_false(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    ok, message = logic.delete_workspace_preset("Nope")

    assert ok is False
    assert message


def test_load_last_workspace_defaults_to_empty(monkeypatch, tmp_path):
    """A fresh install opens an empty workspace, not the ten screens it then has to close."""
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    assert logic.load_last_workspace() == ""


def test_save_last_workspace_round_trips(monkeypatch, tmp_path):
    workspace_handler = JSONDataHandler(file_path=tmp_path / "workspaces.json")
    monkeypatch.setattr(logic, "workspace_handler", workspace_handler)

    logic.save_last_workspace("My Layout")

    assert logic.load_last_workspace() == "My Layout"
