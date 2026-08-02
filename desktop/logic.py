"""UI-independent logic lifted from routes.py.

Everything here is pure or touches only the filesystem — no Qt, no ServiceHub, no hardware.
Lifted verbatim from routes.py so validation and clamping semantics cannot drift between the
Flask app and the desktop port. Hub-dependent helpers live in services.py instead.
"""

import ipaddress
import json
import math
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from lib.json_data_handler import JSONDataHandler
from lib.pid_config_client import AXES as PID_AXES
from lib.runtime_paths import data_path

PID_CONFIGS_FILE = data_path("pid_configs.json")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

data_handler = JSONDataHandler()
config_handler = JSONDataHandler(file_path=data_path("config.json"))

_DEFAULT_IMU_AXES = {"yaw": "+yaw", "pitch": "+pitch", "roll": "+roll"}
_DEFAULT_ACCEL_AXES = {"x": "+x", "y": "+y", "z": "+z"}
_DEFAULT_OFFSET = {"x": 0.0, "y": 0.0, "z": 0.0}
ATTITUDE_LIMITS_DEG = {"roll": 180.0, "pitch": 90.0, "yaw": 180.0}
CONTROL_AXES = ("surge", "sway", "heave", "roll", "pitch", "yaw")
TRANSLATIONAL_AXES = ("surge", "sway", "heave")
ATTITUDE_AXES = ("roll", "pitch", "yaw")
DEFAULT_PID_SETPOINT_RATES = {axis: 90.0 for axis in ATTITUDE_AXES}
DEFAULT_CONTROLLER_GAINS = {"master": 1.0, "axes": {axis: 1.0 for axis in CONTROL_AXES}}
DEFAULT_IP_CAMERA_IP = "10.77.0.4"

_VALID_THEMES = {"dark", "light"}
VALID_IMU_AXES = {"+yaw", "-yaw", "+pitch", "-pitch", "+roll", "-roll"}
VALID_ACCEL_AXES = {"+x", "-x", "+y", "-y", "+z", "-z"}
NAME_PATTERN = re.compile(r"^[\w\s\-\.]+$")

# Returned by the resources view when no telemetry has arrived yet.
DEFAULT_RESOURCES = {
    "sequence": 0,
    "uptime_ms": 0,
    "cpu_percent": 0,
    "heap_used_percent": 0,
    "heap_free_kb": 0,
    "heap_total_kb": 0,
    "thread_count": 0,
    "udp_rx_count": 0,
    "udp_rx_errors": 0,
}


def valid_name(name):
    """Preset and PID-config names share one validation rule."""
    return bool(name) and bool(NAME_PATTERN.match(name))


def clamp(value, lower, upper):
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def normalize_angle_deg(value):
    wrapped = ((float(value) + 180.0) % 360.0) - 180.0
    if wrapped == -180.0 and float(value) > 0:
        return 180.0
    return wrapped


def neutral_axis_values():
    return {axis: 0.0 for axis in CONTROL_AXES}


def zero_pid_gains():
    return {axis: {"kp": 0.0, "ki": 0.0, "kd": 0.0} for axis in PID_AXES}


def attitude_pid_gains(gains):
    return {axis: gains.get(axis, {"kp": 0.0, "ki": 0.0, "kd": 0.0}) for axis in ATTITUDE_AXES}


def mcu_pid_gains(gains):
    """Coerce arbitrary input into the full zero-filled gain packet the MCU expects."""
    packet = zero_pid_gains()
    for axis in ATTITUDE_AXES:
        axis_gains = gains.get(axis, {}) if isinstance(gains, dict) else {}
        cleaned = {}
        for key in ("kp", "ki", "kd"):
            try:
                cleaned[key] = float(axis_gains.get(key, 0.0))
            except (AttributeError, TypeError, ValueError):
                cleaned[key] = 0.0
        packet[axis] = {"kp": cleaned["kp"], "ki": cleaned["ki"], "kd": cleaned["kd"]}
    return packet


def clean_pid_rates(data):
    rates = {}
    for axis in ATTITUDE_AXES:
        try:
            value = float(data.get(axis, DEFAULT_PID_SETPOINT_RATES[axis]))
        except (AttributeError, TypeError, ValueError):
            value = DEFAULT_PID_SETPOINT_RATES[axis]
        if not math.isfinite(value):
            value = DEFAULT_PID_SETPOINT_RATES[axis]
        rates[axis] = clamp(value, 0.0, 90.0)
    return rates


def clean_controller_gains(data):
    gains = {"master": 1.0, "axes": {axis: 1.0 for axis in CONTROL_AXES}}
    if not isinstance(data, dict):
        return gains

    try:
        value = float(data.get("master", DEFAULT_CONTROLLER_GAINS["master"]))
    except (TypeError, ValueError):
        value = DEFAULT_CONTROLLER_GAINS["master"]
    if not math.isfinite(value):
        value = DEFAULT_CONTROLLER_GAINS["master"]
    gains["master"] = clamp(value, 0.0, 1.0)

    axes = data.get("axes", {})
    if not isinstance(axes, dict):
        axes = data
    for axis in CONTROL_AXES:
        try:
            value = float(axes.get(axis, DEFAULT_CONTROLLER_GAINS["axes"][axis]))
        except (AttributeError, TypeError, ValueError):
            value = DEFAULT_CONTROLLER_GAINS["axes"][axis]
        if not math.isfinite(value):
            value = DEFAULT_CONTROLLER_GAINS["axes"][axis]
        gains["axes"][axis] = clamp(value, 0.0, 1.0)
    return gains


def coerce_attitude_setpoints(data):
    """Keep only finite roll/pitch/yaw values, wrapped and clamped to their limits."""
    axes = {}
    for axis in ATTITUDE_AXES:
        if axis not in data:
            continue
        try:
            value = float(data[axis])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        limit = ATTITUDE_LIMITS_DEG[axis]
        if axis in ("roll", "yaw"):
            value = normalize_angle_deg(value)
        axes[axis] = clamp(value, -limit, limit)
    return axes


def debug_override_axes(values):
    """Clamp slider values to +/-1 and apply the debug yaw sign flip.

    The negation is load-bearing: the slider UI and the controller disagree on yaw sign, and
    POST /api/debug/override corrected for it. Any screen that drives a debug override must go
    through here — the debug and PID-tuning screens both do.
    """
    axes = {}
    for key in CONTROL_AXES:
        if key not in values:
            continue
        value = clamp(float(values[key]), -1.0, 1.0)
        axes[key] = -value if key == "yaw" else value
    return axes


def imu_attitude_sanity(stats):
    """Classify whether current IMU attitude can seed a PID hold.

    Three outcomes the UI must distinguish:
      not usable          -> hard failure, cannot start
      usable but not ok   -> recoverable, offer the operator a Force action
      ok                  -> start normally
    """
    age_ms = stats.get("age_ms")
    raw = stats.get("last_data") or {}
    reasons = []
    numeric = {}

    if age_ms is None:
        reasons.append("IMU data is missing")
    elif age_ms > 2000:
        reasons.append("IMU data is stale")

    for axis in ATTITUDE_AXES:
        value = raw.get(axis)
        try:
            value = float(value)
        except (TypeError, ValueError):
            reasons.append(f"{axis} is missing or not numeric")
            continue
        if not math.isfinite(value):
            reasons.append(f"{axis} is NaN or infinite")
            continue
        limit = ATTITUDE_LIMITS_DEG[axis]
        if value < -limit or value > limit:
            reasons.append(f"{axis} is outside -{limit:.0f}..{limit:.0f}")
        numeric[axis] = value

    setpoints = coerce_attitude_setpoints(numeric)
    usable = len(setpoints) == len(ATTITUDE_AXES)
    return {
        "ok": usable and not reasons,
        "usable": usable,
        "reason": "; ".join(reasons),
        "raw": raw,
        "age_ms": age_ms,
        "setpoints": setpoints,
    }


def git_info():
    """Blocking — 1 s subprocess timeout. Call via ServiceHub.call_async."""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
    except Exception:
        branch = "unknown"
    return {"branch": branch}


def live_from_age(age_ms, max_age_ms):
    return age_ms is not None and age_ms <= max_age_ms


# --- persisted settings -------------------------------------------------------


def load_pid_rates():
    return clean_pid_rates(config_handler.get_section("pid_setpoint_rates") or {})


def load_controller_gains():
    return clean_controller_gains(config_handler.get_section("controller_gains") or {})


def load_imu_axes():
    return config_handler.get_section("imu_axes") or dict(_DEFAULT_IMU_AXES)


def load_accel_axes():
    return config_handler.get_section("accel_axes") or dict(_DEFAULT_ACCEL_AXES)


def load_imu_offset():
    return config_handler.get_section("imu_offset") or dict(_DEFAULT_OFFSET)


def load_theme():
    return (config_handler.get_section("theme") or {}).get("name", "dark")


def save_theme(name):
    if name not in _VALID_THEMES:
        raise ValueError(f"Unknown theme: {name!r}")
    config_handler.update_data({"theme": {"name": name}})
    return name


# --- PID config presets -------------------------------------------------------
# Bare open() rather than JSONDataHandler, matching routes.py. Pre-existing; not fixed here.


def load_pid_configs():
    if PID_CONFIGS_FILE.exists():
        with open(PID_CONFIGS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_pid_configs(configs):
    PID_CONFIGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PID_CONFIGS_FILE, "w") as f:
        json.dump(configs, f, indent=2)


# --- IP camera ----------------------------------------------------------------


def camera_url_for_ip(ip):
    return f"rtsp://{ip}:554/stream1"


def coerce_ipv4(value):
    try:
        addr = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return str(addr)


def ip_from_url(url):
    try:
        return coerce_ipv4(urlparse(url).hostname)
    except Exception:
        return None


def get_ip_camera_config():
    """Normalize the stored ip_camera section; tolerates the legacy dict-of-presets shape."""
    section = config_handler.get_section("ip_camera") or {}
    raw_presets = section.get("presets", [])
    presets = []
    if isinstance(raw_presets, dict):
        raw_presets = [{"name": name, "ip": ip} for name, ip in raw_presets.items()]
    if isinstance(raw_presets, list):
        for item in raw_presets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            ip = coerce_ipv4(item.get("ip"))
            if name and ip:
                presets.append({"name": name, "ip": ip})
    active_ip = coerce_ipv4(section.get("active_ip")) or DEFAULT_IP_CAMERA_IP
    return {"active_ip": active_ip, "presets": presets}


def save_ip_camera_config(section):
    config_handler.update_data({"ip_camera": section})


def upsert_ip_camera_preset(name, ip):
    """Replace-by-name then sort case-insensitively, matching POST /api/ip_camera/configs."""
    section = get_ip_camera_config()
    presets = [preset for preset in section["presets"] if preset["name"] != name]
    presets.append({"name": name, "ip": ip})
    presets.sort(key=lambda preset: preset["name"].lower())
    section["presets"] = presets
    save_ip_camera_config(section)
    return presets


def delete_ip_camera_preset(name):
    """Returns None when the preset did not exist (the route's 404 case)."""
    section = get_ip_camera_config()
    presets = [preset for preset in section["presets"] if preset["name"] != name]
    if len(presets) == len(section["presets"]):
        return None
    section["presets"] = presets
    save_ip_camera_config(section)
    return presets


# --- workspace layout presets -------------------------------------------------
# Through JSONDataHandler, unlike pid_configs.json above -- that bare open() is a documented
# wart, not a pattern to copy.

WORKSPACE_FILE = data_path("workspaces.json")
workspace_handler = JSONDataHandler(file_path=WORKSPACE_FILE)

#: Rows a whole screen gets in the Classic layout. Their content is tall; anything less and every
#: one of them opens already scrolling.
CLASSIC_SCREEN_ROWS = 13

#: The built-in layout: the ten whole screens, each full width, stacked down one workspace.
#: It is NOT the startup default any more — a workspace starts empty and the operator builds one.
#: It survives as a named preset for an operator who wants every screen reachable by scrolling,
#: which is the closest the grid gets to the old tab bar; the grid has no tabs to reproduce it
#: exactly. Procedural rather than a captured blob, so it stays readable and cannot go stale.
CLASSIC_PRESET_NAME = "Classic"
CLASSIC_PRESET = {
    "version": 2,
    "windows": [
        {
            "components": [
                {
                    "id": f"screen.{name}",
                    "col": 0,
                    "row": index * CLASSIC_SCREEN_ROWS,
                    "cols": 12,
                    "rows": CLASSIC_SCREEN_ROWS,
                }
                for index, name in enumerate(
                    (
                        "home",
                        "pilot",
                        "tooling",
                        "debug",
                        "pid_tuning",
                        "graphs",
                        "config",
                        "connection",
                        "logs",
                        "ip_camera",
                    )
                )
            ],
        }
    ],
}


def _workspace_section(name):
    """Read a section without logging on a fresh install.

    JSONDataHandler reports a missing file to stdout; workspaces.json legitimately does not exist
    until the operator saves a layout, and an error line on every first launch is just noise.
    """
    # Ask the handler for its path rather than WORKSPACE_FILE, so a test that swaps the handler
    # for a tmp_path one is still checking the file it actually reads.
    if not Path(workspace_handler.file_path).exists():
        return {}
    return workspace_handler.get_section(name) or {}


def load_workspace_presets():
    """Saved presets plus the built-in Classic, which a user preset may never shadow."""
    presets = dict(_workspace_section("presets"))
    presets[CLASSIC_PRESET_NAME] = CLASSIC_PRESET
    return presets


def save_workspace_preset(name, preset):
    """Returns (ok, message). Refuses invalid names and the reserved Classic name."""
    name = (name or "").strip()
    if not name:
        return False, "Name a layout before saving."
    if not valid_name(name):
        return False, "Use letters, numbers, spaces, dots or dashes."
    if name == CLASSIC_PRESET_NAME:
        return False, f"{CLASSIC_PRESET_NAME} is built in and cannot be overwritten."
    presets = dict(_workspace_section("presets"))
    presets[name] = preset
    # update_data is a shallow .update(), so the whole section goes back every time.
    workspace_handler.update_data({"presets": presets})
    return True, f"Saved {name}."


def delete_workspace_preset(name):
    """Returns (ok, message). None-equivalent for a name that was never saved."""
    if name == CLASSIC_PRESET_NAME:
        return False, f"{CLASSIC_PRESET_NAME} is built in and cannot be deleted."
    presets = dict(_workspace_section("presets"))
    if name not in presets:
        return False, f"No saved layout called {name}."
    del presets[name]
    workspace_handler.update_data({"presets": presets})
    return True, f"Deleted {name}."


def load_last_workspace():
    """Name of the layout to restore on launch, or "" for an empty workspace.

    Empty is the default on a fresh install: the operator picks the components they want out of
    the Components menu and saves the arrangement, rather than being handed ten screens they then
    have to close.
    """
    return _workspace_section("last").get("name", "")


def save_last_workspace(name):
    workspace_handler.update_data({"last": {"name": name}})
    return name
