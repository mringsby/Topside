"""Pure-function coverage for desktop/logic.py.

These are the UI-independent helpers routes.py used to call directly and that the dying
route tests (`test_pid_runtime_routes.py`, `test_ip_camera_routes.py`) exercised indirectly
through Flask. No Qt, no ServiceHub, no hardware — just `desktop.logic` and `lib.json_data_handler`.
"""

from desktop import logic
from lib.json_data_handler import JSONDataHandler

# --- controller gain clamping (test_controller_gains_api_clamps_and_updates_controller) --------


def test_clean_controller_gains_clamps_master_and_axes():
    gains = logic.clean_controller_gains({"master": 1.4, "axes": {"surge": 0.25, "yaw": -1}})

    assert gains["master"] == 1.0
    assert gains["axes"]["surge"] == 0.25
    assert gains["axes"]["yaw"] == 0.0
    # Axes not present in the input fall back to the default multiplier.
    assert gains["axes"]["heave"] == 1.0


def test_clean_controller_gains_rejects_non_dict_input():
    assert logic.clean_controller_gains("not a dict") == logic.DEFAULT_CONTROLLER_GAINS


# --- PID gain translation-axis zeroing (test_pid_gains_force_translation_axes_to_zero) ---------


def test_mcu_pid_gains_zeroes_translation_axes_and_keeps_attitude():
    gains = logic.mcu_pid_gains(
        {
            "surge": {"kp": 9, "ki": 9, "kd": 9},
            "roll": {"kp": 1.0, "ki": 2.0, "kd": 3.0},
        }
    )

    assert gains["surge"] == {"kp": 0.0, "ki": 0.0, "kd": 0.0}
    assert gains["sway"] == {"kp": 0.0, "ki": 0.0, "kd": 0.0}
    assert gains["heave"] == {"kp": 0.0, "ki": 0.0, "kd": 0.0}
    assert gains["roll"] == {"kp": 1.0, "ki": 2.0, "kd": 3.0}


def test_attitude_pid_gains_filters_to_roll_pitch_yaw():
    gains = logic.mcu_pid_gains({"roll": {"kp": 1.0, "ki": 2.0, "kd": 3.0}})

    attitude_only = logic.attitude_pid_gains(gains)

    assert set(attitude_only.keys()) == {"roll", "pitch", "yaw"}
    assert attitude_only["roll"] == {"kp": 1.0, "ki": 2.0, "kd": 3.0}


# --- PID start sanity gate three-way classification (test_pid_start_returns_sanity_failure...) -


def test_imu_attitude_sanity_ok_when_fresh_and_in_range():
    sanity = logic.imu_attitude_sanity({"age_ms": 10, "last_data": {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}})

    assert sanity["ok"] is True
    assert sanity["usable"] is True
    assert sanity["setpoints"] == {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}


def test_imu_attitude_sanity_stale_but_usable_offers_force():
    """Stale IMU data (age > 2000ms) is recoverable: usable=True, ok=False -> UI offers Force."""
    sanity = logic.imu_attitude_sanity({"age_ms": 3000, "last_data": {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}})

    assert sanity["usable"] is True
    assert sanity["ok"] is False
    assert "stale" in sanity["reason"]


def test_imu_attitude_sanity_hard_fail_when_axis_missing():
    """A missing/non-numeric axis is not recoverable at all: usable=False, no Force offered."""
    sanity = logic.imu_attitude_sanity({"age_ms": 10, "last_data": {"roll": 1.0, "pitch": None, "yaw": 3.0}})

    assert sanity["usable"] is False
    assert sanity["ok"] is False


# --- debug override clamping + yaw sign flip (killswitch/debug override behaviour) --------------


def test_debug_override_axes_clamps_and_negates_yaw():
    axes = logic.debug_override_axes({"surge": 2.0, "yaw": 0.5, "pitch": -2.0})

    assert axes["surge"] == 1.0
    assert axes["pitch"] == -1.0
    assert axes["yaw"] == -0.5  # yaw sign flip is load-bearing, see PARITY.md


# --- attitude setpoint coercion (setpoint save/clear semantics) ---------------------------------


def test_coerce_attitude_setpoints_wraps_and_clamps():
    axes = logic.coerce_attitude_setpoints({"roll": 190.0, "pitch": 120.0, "yaw": "not a number"})

    # roll wraps into -180..180, pitch clamps to its 90 deg limit, invalid yaw is dropped.
    assert axes["roll"] == -170.0
    assert axes["pitch"] == 90.0
    assert "yaw" not in axes


# --- IP camera preset CRUD (test_ip_camera_preset_save_and_delete) ------------------------------


def test_upsert_and_delete_ip_camera_preset(monkeypatch, tmp_path):
    config_handler = JSONDataHandler(file_path=tmp_path / "config.json")
    monkeypatch.setattr(logic, "config_handler", config_handler)

    presets = logic.upsert_ip_camera_preset("Pool", "10.77.0.5")
    assert presets == [{"name": "Pool", "ip": "10.77.0.5"}]

    section = logic.get_ip_camera_config()
    assert section["presets"] == [{"name": "Pool", "ip": "10.77.0.5"}]

    presets = logic.delete_ip_camera_preset("Pool")
    assert presets == []

    # Deleting a name that no longer exists is the route's 404-equivalent case.
    assert logic.delete_ip_camera_preset("Pool") is None


def test_valid_name_and_coerce_ipv4():
    assert logic.valid_name("Pool Cam-1") is True
    assert logic.valid_name("bad/name") is False
    assert logic.valid_name("") is False

    assert logic.coerce_ipv4("10.77.0.5") == "10.77.0.5"
    assert logic.coerce_ipv4("not-an-ip") is None
    assert logic.coerce_ipv4("::1") is None  # IPv6 rejected, only IPv4 presets are supported
