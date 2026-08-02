"""PID tuning screen — the largest single unit in the port.

Ports `/pid-tuning` (`pid_tuning.html` + `pid_tuning.js`, 816 lines). This page is the field
console for attitude-hold tuning: debug override sliders, angle setpoints, MCU PID gain
request/send, saved tune presets, and a live telemetry table, all layered over the same
kill/rearm and control-state machinery as the debug screen.

Poll cadence mirrors the source JS exactly (load-bearing per PARITY.md §7):
  * control state         -> 500 ms  (pollControlState)
  * IMU + control telemetry -> 200 ms  (pollImuAndTelemetry)
  * rov status / debug view -> 500 ms  (pollRovStatus)
  * override send loop     ->  50 ms  (SEND_INTERVAL_MS, local timer, not a poller)

Two behaviours are deliberately NOT literal ports, both because the shard contract and
`ScreenBase` forbid `QMessageBox`-style blocking dialogs:

  * `POST /api/pid/start` returning 409 + `force_supported` normally prompts a
    `window.confirm(...)` in the browser. Here it surfaces a "Force Start" button instead
    (shown/enabled only for the recoverable case) — same three-way sanity outcome
    (hard fail / recoverable+force / ok), no modal.
  * Deleting a saved PID config normally goes through `window.confirm(...)`. The guard is
    kept — losing a tune the operator spent a dive building is not a one-click mistake to
    allow — but as a two-step arm rather than a modal: the first click re-labels the button,
    a second click within DELETE_ARM_TIMEOUT_MS commits, and the arm lapses on its own.
    Same pattern as `screens/connection.py::_on_reset_clicked`.

Every blocking MCU/network call (`request_pid_gains`, `send_pid_gains`, `send_override`,
`clear_override`) goes through `hub.call_async`, never directly from a slot.

Decomposed into six dockable `PanelBase` panels (one per former `_build_*` method). The two
read-only panels (`ReadoutsPanel`, `TelemetryPanel`) are fully self-sufficient — they poll for
their own data via module-level getters, exactly like `graphs.py`'s `ChartPanel` — which is why
they are the only panels marked `duplicable=True`. The four panels that can write to the vehicle
(`ActionBarPanel`, `OverridePanel`, `SetpointsPanel`, `GainsPanel`) stay `duplicable=False` and
delegate every action back to `PidTuningScreen` when composed on this screen; opened standalone
(no screen to delegate to) their controls are disabled with an explanatory notice rather than
duplicating hardware-writing logic in a second place.
"""

import json
import math
import time
from functools import partial

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop import logic, theme
from desktop.component import Component
from desktop.screens.base import PanelBase, ScreenBase
from desktop.screens.debug import AXIS_LABELS, AxisSlider
from lib.pid_config_client import request_pid_gains, send_pid_gains

SEND_INTERVAL_MS = 50
CONTROL_STATE_INTERVAL_MS = 500
IMU_TELEMETRY_INTERVAL_MS = 200
ROV_STATUS_INTERVAL_MS = 500
#: How long the Delete button stays armed awaiting a second click.
DELETE_ARM_TIMEOUT_MS = 5000

ROT_AXIS_LABELS = [("roll", "Roll"), ("pitch", "Pitch"), ("yaw", "Yaw")]
GAIN_KEYS = ("kp", "ki", "kd")

#: Shown on a write panel's controls when it is opened standalone (no PidTuningScreen to
#: delegate the action to). Duplicating hardware-writing logic into a second code path is the
#: exact hazard `duplicable=False` exists to avoid, so a standalone write panel disables its
#: controls instead of reimplementing them.
_STANDALONE_NOTICE = "Open the PID Tuning screen to use this control."

#: variant -> (theme token key, contrasting text colour for a solid-fill pill background).
#: Proxied through `theme.token()` lazily (see desktop/theme.py) rather than a frozen dict, so a
#: theme switch after import is picked up.
_BADGE_TEXT = {
    "secondary": "white",
    "success": "white",
    "danger": "white",
    "warning": "black",
    "info": "black",
}


class _BadgeColorMap:
    def __getitem__(self, variant):
        return theme.token(variant), _BADGE_TEXT[variant]

    def get(self, variant, default=None):
        try:
            return self[variant]
        except KeyError:
            return default


_BADGE_COLORS = _BadgeColorMap()


class _FeedbackColorMap:
    _KEYS = {"success", "danger", "warning", "info"}

    def get(self, variant, default=""):
        if variant not in self._KEYS:
            return default
        return theme.token(variant)


_FEEDBACK_COLORS = _FeedbackColorMap()


def _set_badge(label, text, variant="secondary"):
    bg, fg = _BADGE_COLORS.get(variant, _BADGE_COLORS["secondary"])
    label.setText(text)
    label.setStyleSheet(f"background-color:{bg}; color:{fg}; padding:2px 10px; border-radius:4px; font-weight:600;")


def _make_badge(text, variant):
    label = QLabel()
    _set_badge(label, text, variant)
    return label


def _make_status_card(title, value_widget):
    box = QGroupBox()
    v = QVBoxLayout(box)
    v.addWidget(QLabel(f"<small>{title}</small>"))
    v.addWidget(value_widget)
    return box


def _disable_for_standalone(panel, widgets, message=_STANDALONE_NOTICE):
    """A write panel opened without a host screen has nothing safe to delegate its actions to.

    Disabling the controls (rather than reimplementing the hardware-writing logic a second
    time) is the deliberate choice here — see the module docstring.
    """
    for widget in widgets:
        widget.setEnabled(False)
    panel.notify(message)


# --- pure calculations shared by ReadoutsPanel and TelemetryPanel --------------------------
# Both panels show the same per-axis numbers (position/setpoint/error, plus mode/output/gains
# in the table); kept as plain functions so neither panel duplicates the other's math.


def _safe_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fmt(value, digits=2):
    value = _safe_float(value)
    return "--" if value is None else f"{value:.{digits}f}"


def _telemetry_setpoint(telemetry, axis, local_setpoints, pid_enabled):
    from_telem = _safe_float((telemetry.get("setpoint") or {}).get(axis))
    from_local = local_setpoints.get(axis)
    if pid_enabled and from_telem is not None:
        return from_telem
    if from_local is not None:
        return from_local
    return from_telem


def _axis_measurement(telemetry, imu, axis):
    from_telem = _safe_float((telemetry.get("measurement") or {}).get(axis))
    if from_telem is not None:
        return from_telem
    return _safe_float(imu.get(axis))


def _axis_error(telemetry, axis, setpoint, position):
    from_telem = _safe_float((telemetry.get("error") or {}).get(axis))
    if from_telem is not None:
        return from_telem
    if setpoint is None or position is None:
        return None
    if axis == "pitch":
        return setpoint - position
    return logic.normalize_angle_deg(setpoint - position)


def _axis_output(telemetry, axis):
    return _safe_float((telemetry.get("output") or {}).get(axis))


def _axis_gains_text(telemetry, axis):
    gains = (telemetry.get("gains") or {}).get(axis)
    if not gains:
        return "--"
    return f"P {_fmt(gains.get('kp'))} I {_fmt(gains.get('ki'))} D {_fmt(gains.get('kd'))}"


def _axis_mode(telemetry, axis):
    if not telemetry:
        return "--"
    flags = telemetry.get("flags") or {}
    if flags.get("timeout"):
        return "TIMEOUT"
    bit = 1 << logic.CONTROL_AXES.index(axis)
    if (telemetry.get("override_mask") or 0) & bit:
        return "OVR"
    if (telemetry.get("pid_active_mask") or 0) & bit:
        return "PID"
    return "LEGACY" if telemetry.get("protocol_version") == 1 else "PASS"


def _control_path_text(path, killed):
    if killed:
        return "Controls locked"
    if path == "Override Controls":
        return "Override sliders"
    if path == "PS4":
        return "PS4 Controller"
    return path or "PS4 Controller"


def _debug_payload(status, control, telemetry):
    control = control or {}
    uplink = status.get("uplink") or {}
    telemetry = telemetry or {}
    return {
        "control_path": _control_path_text(control.get("control_path"), control.get("killed") is True),
        "pid_enabled": control.get("pid_enabled"),
        "active_setpoints": control.get("pid_setpoints"),
        "manual_command_before_pid": control.get("manual_command_before_pid"),
        "mcu_flags": telemetry.get("flags", {}),
        "mcu_command_age_ms": telemetry.get("last_command_age_ms"),
        "mcu_measurement": telemetry.get("measurement", {}),
        "pid_output": telemetry.get("output", {}),
        "pid_gains_mcu": telemetry.get("gains", {}),
        "final_topside_command": status.get("command"),
        "raw_payload": uplink.get("last_packet_hex"),
        "timestamp": uplink.get("last_send_timestamp"),
        "sequence": uplink.get("sequence"),
        "link": {
            "ack_age_ms": uplink.get("last_ack_age_ms"),
            "watchdog_resends": uplink.get("watchdog_resends"),
        },
        "telemetry": {
            "sequence": telemetry.get("sequence"),
            "timestamp": telemetry.get("timestamp"),
        },
        "resource": status.get("resource"),
    }


# --- shared poller getters --------------------------------------------------------------
# Module-level so several panels (and the screen) can watch the same key and share one timer.


def read_control_state(hub):
    ctrl = hub.controller
    return ctrl.get_control_state() if ctrl else None


def read_imu_telemetry(hub):
    imu_stats = hub.imu.get_stats() if hub.imu else None
    telemetry = hub.control_telem.get_latest() if hub.control_telem else None
    return {"imu": imu_stats, "telemetry": telemetry}


def read_rov_status(hub):
    udp_rx, udp_err = hub.resource.get_udp_counters() if hub.resource else (0, 0)
    ctrl = hub.controller
    return {
        "command": hub.bitmask.get_command() if hub.bitmask else {},
        "uplink": hub.bitmask.get_uplink_status() if hub.bitmask else {},
        "control_state": ctrl.get_control_state() if ctrl else {},
        "resource": {"udp_rx_count": udp_rx, "udp_rx_errors": udp_err},
    }


# --- panels ------------------------------------------------------------------------------


class ActionBarPanel(PanelBase):
    """Control-path/PID-mode status cards plus the start/force-start/rearm/kill buttons.

    Writes to the vehicle (start/stop PID, kill, rearm), so it stays single-instance
    (`duplicable=False`). When composed on `PidTuningScreen` its buttons delegate to the
    screen's existing handlers; opened standalone they are disabled (see module docstring).
    """

    def __init__(self, hub, screen=None, parent=None):
        super().__init__(hub, parent)
        self.title = "PID Action Bar"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        status_row = QHBoxLayout()
        self.control_path_label = QLabel("PS4 Controller")
        status_row.addWidget(_make_status_card("Active control", self.control_path_label))

        self.pid_mode_badge = _make_badge("OFF", "secondary")
        status_row.addWidget(_make_status_card("PID", self.pid_mode_badge))

        self.branch_label = QLabel("--")
        status_row.addWidget(_make_status_card("Branch", self.branch_label))

        self.imu_age_label = QLabel("--")
        status_row.addWidget(_make_status_card("IMU age (ms)", self.imu_age_label))
        layout.addLayout(status_row, 1)

        controls = QHBoxLayout()
        self.btn_toggle_pid = QPushButton("Start PID")
        self.btn_force_start = QPushButton("Force Start")
        self.btn_force_start.setVisible(False)
        self.btn_rearm = QPushButton("Re-arm")
        self.btn_kill = QPushButton("KILLSWITCH")
        controls.addWidget(self.btn_toggle_pid)
        controls.addWidget(self.btn_force_start)
        controls.addWidget(self.btn_rearm)
        controls.addWidget(self.btn_kill)
        layout.addLayout(controls)

        if screen is not None:
            self.btn_toggle_pid.clicked.connect(screen._toggle_pid)
            self.btn_force_start.clicked.connect(screen._force_start_pid)
            self.btn_rearm.clicked.connect(screen._rearm_controls)
            self.btn_kill.clicked.connect(screen._kill_controls)
        else:
            _disable_for_standalone(self, [self.btn_toggle_pid, self.btn_rearm, self.btn_kill])


class ReadoutsPanel(PanelBase):
    """Position/Setpoint/Error readout for roll, pitch, yaw.

    Read-only, so it is safe to duplicate or pop out on its own — unlike every other panel on
    this screen it never writes to the vehicle. Self-sufficient: polls its own data so it works
    alone in a dock, mirroring `graphs.py`'s `ChartPanel`.

    `local_setpoints` is an optional read-only view into `PidTuningScreen._local_setpoints` (a
    plain dict, shared by reference) so a setpoint typed but not yet sent still previews here,
    same as before decomposition. Standalone, it defaults to empty — no pending edits to show.
    """

    def __init__(self, hub, local_setpoints=None, parent=None):
        super().__init__(hub, parent)
        self.title = "PID Axis Readouts"
        self._local_setpoints = local_setpoints if local_setpoints is not None else {}
        self._latest_imu = {}
        self._latest_telemetry = None
        self._pid_enabled = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.readout_labels = {}
        for axis, label_text in ROT_AXIS_LABELS:
            box = QGroupBox(label_text)
            grid = QGridLayout(box)
            pos_label = QLabel("--")
            set_label = QLabel("--")
            err_label = QLabel("--")
            grid.addWidget(QLabel("Position"), 0, 0)
            grid.addWidget(pos_label, 1, 0)
            grid.addWidget(QLabel("Setpoint"), 0, 1)
            grid.addWidget(set_label, 1, 1)
            grid.addWidget(QLabel("Error"), 0, 2)
            grid.addWidget(err_label, 1, 2)
            self.readout_labels[axis] = {"position": pos_label, "setpoint": set_label, "error": err_label}
            layout.addWidget(box)

        self.watch(
            "pid.control_state", lambda: read_control_state(self.hub), CONTROL_STATE_INTERVAL_MS, self._on_control_state
        )
        self.watch(
            "pid.imu_telemetry", lambda: read_imu_telemetry(self.hub), IMU_TELEMETRY_INTERVAL_MS, self._on_sample
        )

    def _on_control_state(self, state):
        self._pid_enabled = bool(state and state.get("pid_enabled") is True)
        self._refresh()

    def _on_sample(self, payload):
        imu_stats = payload.get("imu")
        if imu_stats:
            data = imu_stats.get("last_data") or {}
            self._latest_imu = {axis: _safe_float(data.get(axis)) for axis in logic.ATTITUDE_AXES}
        self._latest_telemetry = payload.get("telemetry") or None
        self._refresh()

    def _refresh(self):
        telemetry = self._latest_telemetry or {}
        for axis in logic.ATTITUDE_AXES:
            setpoint = _telemetry_setpoint(telemetry, axis, self._local_setpoints, self._pid_enabled)
            position = _axis_measurement(telemetry, self._latest_imu, axis)
            error = _axis_error(telemetry, axis, setpoint, position)
            labels = self.readout_labels[axis]
            labels["position"].setText(_fmt(position))
            labels["setpoint"].setText(_fmt(setpoint))
            labels["error"].setText(_fmt(error))
            if error is None:
                labels["error"].setStyleSheet("color: gray;")
            elif abs(error) > 25:
                labels["error"].setStyleSheet("color: #dc3545; font-weight: bold;")
            elif abs(error) > 10:
                labels["error"].setStyleSheet("color: #c98a00; font-weight: bold;")
            else:
                labels["error"].setStyleSheet("")


class OverridePanel(PanelBase):
    """Debug-override sliders (per-axis manual thruster command) and enable/disable buttons.

    Writes to the vehicle at 20 Hz while active, so it stays single-instance. Delegates to
    `PidTuningScreen._enable_override` / `_disable_override` / `_reset_all_sliders` when
    composed; standalone, its controls are disabled (see module docstring).
    """

    def __init__(self, hub, screen=None, parent=None):
        super().__init__(hub, parent)
        self.title = "PID Override Controls"

        box = QGroupBox("Override Controls")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self.debug_status_badge = _make_badge("INACTIVE", "secondary")
        header.addWidget(self.debug_status_badge)
        layout.addLayout(header)

        buttons = QHBoxLayout()
        self.btn_enable = QPushButton("Enable Override")
        self.btn_disable = QPushButton("Disable Override")
        self.btn_disable.setEnabled(False)
        self.btn_reset_all = QPushButton("Reset Sliders")
        buttons.addWidget(self.btn_enable)
        buttons.addWidget(self.btn_disable)
        buttons.addWidget(self.btn_reset_all)
        layout.addLayout(buttons)

        grid = QGridLayout()
        self.sliders = {}
        for index, (axis, label) in enumerate(AXIS_LABELS):
            widget = AxisSlider(axis, label)
            self.sliders[axis] = widget
            grid.addWidget(widget, index // 3, index % 3)
        layout.addLayout(grid)

        if screen is not None:
            self.btn_enable.clicked.connect(screen._enable_override)
            self.btn_disable.clicked.connect(screen._disable_override)
            self.btn_reset_all.clicked.connect(screen._reset_all_sliders)
        else:
            for widget in self.sliders.values():
                widget.setEnabled(False)
            _disable_for_standalone(self, [self.btn_enable, self.btn_disable, self.btn_reset_all])


class SetpointsPanel(PanelBase):
    """Angle setpoint entry for roll/pitch/yaw, plus save/stop-and-clear.

    Writes to the vehicle (sends or clears PID setpoints), so it stays single-instance.
    Delegates to `PidTuningScreen` when composed; standalone, its controls are disabled.
    """

    def __init__(self, hub, screen=None, parent=None):
        super().__init__(hub, parent)
        self.title = "PID Angle Setpoints"

        box = QGroupBox("Angle Setpoints")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self.setpoint_status_badge = _make_badge("IDLE", "secondary")
        header.addWidget(self.setpoint_status_badge)
        layout.addLayout(header)

        note = QLabel("VN-100 YPR degrees: roll and yaw use -180..+180. Pitch uses -90..+90.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.setpoint_inputs = {}
        self.use_current_buttons = {}
        self.clear_axis_buttons = {}
        form = QGridLayout()
        for row, (axis, label) in enumerate(ROT_AXIS_LABELS):
            limit = logic.ATTITUDE_LIMITS_DEG[axis]
            edit = QLineEdit()
            edit.setPlaceholderText("0.0")
            edit.setValidator(QDoubleValidator(-limit, limit, 2, edit))
            self.setpoint_inputs[axis] = edit

            use_btn = QPushButton("Use Current")
            self.use_current_buttons[axis] = use_btn

            clear_btn = QPushButton("Clear")
            self.clear_axis_buttons[axis] = clear_btn

            if screen is not None:
                use_btn.clicked.connect(partial(screen._use_current, axis))
                clear_btn.clicked.connect(partial(screen._clear_axis, axis))

            form.addWidget(QLabel(label), row, 0)
            form.addWidget(edit, row, 1)
            form.addWidget(use_btn, row, 2)
            form.addWidget(clear_btn, row, 3)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.btn_send_setpoints = QPushButton("Save Setpoints")
        self.btn_clear_setpoints = QPushButton("Stop and Clear")
        buttons.addWidget(self.btn_send_setpoints)
        buttons.addWidget(self.btn_clear_setpoints)
        layout.addLayout(buttons)

        self.setpoint_feedback = QLabel("Waiting for input.")
        self.setpoint_feedback.setWordWrap(True)
        layout.addWidget(self.setpoint_feedback)

        if screen is not None:
            self.btn_send_setpoints.clicked.connect(screen._send_setpoints)
            self.btn_clear_setpoints.clicked.connect(screen._clear_setpoints)
        else:
            widgets = (
                [self.btn_send_setpoints, self.btn_clear_setpoints]
                + list(self.use_current_buttons.values())
                + list(self.clear_axis_buttons.values())
            )
            _disable_for_standalone(self, widgets)


class GainsPanel(PanelBase):
    """MCU PID gain request/send, plus saved-tune presets (local JSON, not networked).

    Writes to the vehicle (sends PID gains) and to local config storage, so it stays
    single-instance. Delegates to `PidTuningScreen` when composed; standalone, its controls
    are disabled.
    """

    def __init__(self, hub, screen=None, parent=None):
        super().__init__(hub, parent)
        self.title = "PID Values"

        box = QGroupBox("PID Values")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self.gain_sync_badge = _make_badge("READY", "secondary")
        header.addWidget(self.gain_sync_badge)
        layout.addLayout(header)

        buttons = QHBoxLayout()
        self.btn_pid_request = QPushButton("Request Current")
        self.btn_pid_send = QPushButton("Send to MCU")
        buttons.addWidget(self.btn_pid_request)
        buttons.addWidget(self.btn_pid_send)
        layout.addLayout(buttons)

        grid = QGridLayout()
        grid.addWidget(QLabel("Axis"), 0, 0)
        grid.addWidget(QLabel("P"), 0, 1)
        grid.addWidget(QLabel("I"), 0, 2)
        grid.addWidget(QLabel("D"), 0, 3)
        self.gain_inputs = {}
        for row, (axis, label) in enumerate(ROT_AXIS_LABELS, start=1):
            grid.addWidget(QLabel(label), row, 0)
            axis_inputs = {}
            for col, gain in enumerate(GAIN_KEYS, start=1):
                spin = QDoubleSpinBox()
                spin.setRange(-100000.0, 100000.0)
                spin.setDecimals(4)
                spin.setSingleStep(0.01)
                grid.addWidget(spin, row, col)
                axis_inputs[gain] = spin
            self.gain_inputs[axis] = axis_inputs
        layout.addLayout(grid)

        config_row = QHBoxLayout()
        self.pid_config_name = QLineEdit()
        self.pid_config_name.setPlaceholderText("Tune name")
        self.btn_pid_save = QPushButton("Save")
        self.pid_config_select = QComboBox()
        self.pid_config_select.addItem("Load tune", "")
        self.btn_pid_load = QPushButton("Load")
        self.btn_pid_delete = QPushButton("Delete")
        self.config_status_badge = _make_badge("-", "secondary")
        config_row.addWidget(self.pid_config_name)
        config_row.addWidget(self.btn_pid_save)
        config_row.addWidget(self.pid_config_select)
        config_row.addWidget(self.btn_pid_load)
        config_row.addWidget(self.btn_pid_delete)
        config_row.addWidget(self.config_status_badge)
        layout.addLayout(config_row)

        if screen is not None:
            self.btn_pid_request.clicked.connect(screen._request_pid_gains)
            self.btn_pid_send.clicked.connect(screen._send_pid_gains)
            self.btn_pid_save.clicked.connect(screen._save_config)
            self.btn_pid_load.clicked.connect(screen._load_config)
            self.btn_pid_delete.clicked.connect(screen._delete_config)
        else:
            _disable_for_standalone(
                self,
                [self.btn_pid_request, self.btn_pid_send, self.btn_pid_save, self.btn_pid_load, self.btn_pid_delete],
            )


class TelemetryPanel(PanelBase):
    """Per-axis PID telemetry table plus the raw debug JSON view (rov status / uplink / link).

    Read-only, so it is safe to duplicate or pop out on its own. Self-sufficient: polls its own
    data (control state, IMU + control telemetry, rov status) so it works alone in a dock.

    `local_setpoints` is the same optional shared-dict view used by `ReadoutsPanel` — see there.
    """

    def __init__(self, hub, local_setpoints=None, parent=None):
        super().__init__(hub, parent)
        self.title = "PID Telemetry and Link"
        self._local_setpoints = local_setpoints if local_setpoints is not None else {}
        self._latest_imu = {}
        self._latest_telemetry = None
        self._latest_control_state = {}
        self._pid_enabled = False

        box = QGroupBox("Telemetry and Link")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self.telemetry_age_badge = _make_badge("NO TELEMETRY", "secondary")
        header.addWidget(self.telemetry_age_badge)
        layout.addLayout(header)

        self.telemetry_table = QTableWidget(len(ROT_AXIS_LABELS), 7)
        self.telemetry_table.setHorizontalHeaderLabels(
            ["Axis", "Mode", "Setpoint", "MCU Measure", "Error", "Output", "Gains"]
        )
        self.telemetry_table.verticalHeader().setVisible(False)
        self.telemetry_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.telemetry_table)

        self.rov_status_view = QPlainTextEdit()
        self.rov_status_view.setReadOnly(True)
        self.rov_status_view.setPlainText("Loading...")
        layout.addWidget(self.rov_status_view, 1)

        self.watch(
            "pid.control_state", lambda: read_control_state(self.hub), CONTROL_STATE_INTERVAL_MS, self._on_control_state
        )
        self.watch(
            "pid.imu_telemetry",
            lambda: read_imu_telemetry(self.hub),
            IMU_TELEMETRY_INTERVAL_MS,
            self._on_imu_telemetry,
        )
        self.watch("pid.rov_status", lambda: read_rov_status(self.hub), ROV_STATUS_INTERVAL_MS, self._on_rov_status)

    def _on_control_state(self, state):
        self._latest_control_state = state or {}
        self._pid_enabled = self._latest_control_state.get("pid_enabled") is True
        self._update_table()

    def _on_imu_telemetry(self, payload):
        imu_stats = payload.get("imu")
        if imu_stats:
            data = imu_stats.get("last_data") or {}
            self._latest_imu = {axis: _safe_float(data.get(axis)) for axis in logic.ATTITUDE_AXES}
        self._latest_telemetry = payload.get("telemetry") or None
        self._update_telemetry_age()
        self._update_table()

    def _on_rov_status(self, status):
        control_state = status.get("control_state") or self._latest_control_state
        payload = _debug_payload(status, control_state, self._latest_telemetry)
        self.rov_status_view.setPlainText(json.dumps(payload, indent=2, default=str))

    def _update_telemetry_age(self):
        telemetry = self._latest_telemetry
        if not telemetry or telemetry.get("timestamp") is None:
            _set_badge(self.telemetry_age_badge, "NO TELEMETRY", "secondary")
            return
        age_ms = max(0.0, (time.time() - telemetry["timestamp"]) * 1000.0)
        flags = telemetry.get("flags") or {}
        if flags.get("timeout"):
            _set_badge(self.telemetry_age_badge, "NUCLEO TIMEOUT", "danger")
        elif age_ms < 750:
            _set_badge(self.telemetry_age_badge, f"{age_ms:.0f} ms", "success")
        elif age_ms < 2500:
            _set_badge(self.telemetry_age_badge, f"{age_ms:.0f} ms", "warning")
        else:
            _set_badge(self.telemetry_age_badge, "STALE", "danger")

    def _update_table(self):
        table = self.telemetry_table
        telemetry = self._latest_telemetry or {}
        timeout = bool(telemetry and (telemetry.get("flags") or {}).get("timeout"))
        for row, axis in enumerate(logic.ATTITUDE_AXES):
            setpoint = _telemetry_setpoint(telemetry, axis, self._local_setpoints, self._pid_enabled)
            position = _axis_measurement(telemetry, self._latest_imu, axis)
            error = _axis_error(telemetry, axis, setpoint, position)
            output = _axis_output(telemetry, axis)
            values = [
                axis.upper(),
                _axis_mode(telemetry, axis),
                _fmt(setpoint),
                _fmt(position),
                _fmt(error),
                _fmt(output),
                _axis_gains_text(telemetry, axis),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if timeout:
                    item.setBackground(QColor("#3a1414"))
                elif col == 4 and error is not None and abs(error) > 25:
                    item.setForeground(QColor("#dc3545"))
                elif col == 4 and error is not None and abs(error) > 10:
                    item.setForeground(QColor("#c98a00"))
                table.setItem(row, col, item)


class PidTuningScreen(ScreenBase):
    title = "PID Tuning"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._override_active = False
        self._local_setpoints = {axis: None for axis in logic.ATTITUDE_AXES}
        self._latest_imu = {}
        self._latest_control_state = {}

        root = QVBoxLayout(self)
        root.addWidget(self.notice_widget)

        self._action_bar_panel = ActionBarPanel(hub, screen=self)
        self._control_path_label = self._action_bar_panel.control_path_label
        self._pid_mode_badge = self._action_bar_panel.pid_mode_badge
        self._branch_label = self._action_bar_panel.branch_label
        self._imu_age_label = self._action_bar_panel.imu_age_label
        self._btn_toggle_pid = self._action_bar_panel.btn_toggle_pid
        self._btn_force_start = self._action_bar_panel.btn_force_start
        self._btn_rearm = self._action_bar_panel.btn_rearm
        self._btn_kill = self._action_bar_panel.btn_kill
        root.addWidget(self._action_bar_panel)

        self._readouts_panel = ReadoutsPanel(hub, local_setpoints=self._local_setpoints)
        root.addWidget(self._readouts_panel)

        self._override_panel = OverridePanel(hub, screen=self)
        self._debug_status_badge = self._override_panel.debug_status_badge
        self._btn_enable = self._override_panel.btn_enable
        self._btn_disable = self._override_panel.btn_disable
        self._btn_reset_all = self._override_panel.btn_reset_all
        self._sliders = self._override_panel.sliders

        self._setpoints_panel = SetpointsPanel(hub, screen=self)
        self._setpoint_status_badge = self._setpoints_panel.setpoint_status_badge
        self._setpoint_inputs = self._setpoints_panel.setpoint_inputs
        self._use_current_buttons = self._setpoints_panel.use_current_buttons
        self._clear_axis_buttons = self._setpoints_panel.clear_axis_buttons
        self._btn_send_setpoints = self._setpoints_panel.btn_send_setpoints
        self._btn_clear_setpoints = self._setpoints_panel.btn_clear_setpoints
        self._setpoint_feedback = self._setpoints_panel.setpoint_feedback

        self._gains_panel = GainsPanel(hub, screen=self)
        self._gain_sync_badge = self._gains_panel.gain_sync_badge
        self._btn_pid_request = self._gains_panel.btn_pid_request
        self._btn_pid_send = self._gains_panel.btn_pid_send
        self._gain_inputs = self._gains_panel.gain_inputs
        self._pid_config_name = self._gains_panel.pid_config_name
        self._btn_pid_save = self._gains_panel.btn_pid_save
        self._pid_config_select = self._gains_panel.pid_config_select
        self._btn_pid_load = self._gains_panel.btn_pid_load
        self._btn_pid_delete = self._gains_panel.btn_pid_delete
        self._config_status_badge = self._gains_panel.config_status_badge

        self._telemetry_panel = TelemetryPanel(hub, local_setpoints=self._local_setpoints)

        columns = QHBoxLayout()
        columns.addWidget(self._build_left_stack(), 1)
        columns.addWidget(self._build_right_stack(), 1)
        root.addLayout(columns, 1)

        # A plain QTimer, not a hub poller: this is a local write loop (matches debug.py).
        self._send_timer = QTimer(self)
        self._send_timer.setInterval(SEND_INTERVAL_MS)
        self._send_timer.timeout.connect(self._send_override)

        # Two-step arm for "Delete" (see module docstring) -- lives on the screen, not
        # GainsPanel, since a standalone GainsPanel disables the button entirely.
        self._delete_armed = False
        self._delete_arm_timer = QTimer(self)
        self._delete_arm_timer.setSingleShot(True)
        self._delete_arm_timer.timeout.connect(self._disarm_delete)

        self.watch(
            "pid.control_state", self._read_control_state, CONTROL_STATE_INTERVAL_MS, self._on_control_state_update
        )
        self.watch("pid.imu_telemetry", self._read_imu_telemetry, IMU_TELEMETRY_INTERVAL_MS, self._on_imu_telemetry)

        self._refresh_enabled()

    # --- layout composition -----------------------------------------------------

    def _build_left_stack(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._override_panel)
        layout.addWidget(self._setpoints_panel)
        layout.addStretch(1)
        return widget

    def _build_right_stack(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._gains_panel)
        layout.addWidget(self._telemetry_panel, 1)
        return widget

    def _badge(self, label, text, variant="secondary"):
        _set_badge(label, text, variant)

    # --- lifecycle --------------------------------------------------------

    def on_activate(self):
        self._refresh_config_list()
        self.hub.call_async(logic.git_info, on_done=self._on_git_info)

    def on_deactivate(self):
        """Never leave an override running behind the operator's back (see debug.py)."""
        if self._override_active:
            self._disable_override()
        self._disarm_delete()

    def _on_git_info(self, info):
        self._branch_label.setText((info or {}).get("branch", "--"))

    # --- polling: control state --------------------------------------------

    def _read_control_state(self):
        return read_control_state(self.hub)

    def _on_control_state_update(self, state):
        if state is None:
            self._control_path_label.setText("Controller unavailable")
            self._refresh_enabled()
            return
        self._update_control_banner(state)

    # --- polling: IMU + control telemetry -----------------------------------

    def _read_imu_telemetry(self):
        return read_imu_telemetry(self.hub)

    def _on_imu_telemetry(self, payload):
        imu_stats = payload.get("imu")
        if imu_stats:
            data = imu_stats.get("last_data") or {}
            self._latest_imu = {axis: _safe_float(data.get(axis)) for axis in logic.ATTITUDE_AXES}
            age = imu_stats.get("age_ms")
            self._imu_age_label.setText(str(age) if age is not None else "--")
        else:
            self._imu_age_label.setText("--")

    # --- control banner / readouts ------------------------------------------

    def _update_control_banner(self, state):
        self._latest_control_state = state or {}
        killed = self._latest_control_state.get("killed") is True
        pid_on = self._latest_control_state.get("pid_enabled") is True
        path = self._latest_control_state.get("control_path") or "PS4"
        setpoints = self._latest_control_state.get("pid_setpoints") or {}
        has_setpoints = any(_safe_float(setpoints.get(axis)) is not None for axis in logic.ATTITUDE_AXES)
        self._sync_local_setpoints(setpoints, clear_missing=False)

        self._control_path_label.setText(_control_path_text(path, killed))
        self._badge(self._pid_mode_badge, "ON" if pid_on else "OFF", "success" if pid_on else "secondary")
        if pid_on:
            self._badge(self._setpoint_status_badge, "ACTIVE", "danger")
        elif has_setpoints:
            self._badge(self._setpoint_status_badge, "SAVED", "info")
        else:
            self._badge(self._setpoint_status_badge, "IDLE", "secondary")
        self._btn_toggle_pid.setText("Stop PID" if pid_on else "Start PID")
        override_active = self._latest_control_state.get("override_active") is True
        self._badge(
            self._debug_status_badge,
            "ACTIVE" if override_active else "INACTIVE",
            "danger" if override_active else "secondary",
        )
        self._refresh_enabled()

    def _refresh_enabled(self):
        available = self.hub.controller is not None
        killed = self._latest_control_state.get("killed") is True if available else True
        self.setEnabled(True)
        self._set_controls_disabled(killed)
        for widget in self._sliders.values():
            widget.setEnabled(available)
        self._btn_reset_all.setEnabled(available)
        self._btn_enable.setEnabled(available and not killed and not self._override_active)
        self._btn_disable.setEnabled(available and self._override_active)
        for edit in self._setpoint_inputs.values():
            edit.setEnabled(available)
        for btn in self._use_current_buttons.values():
            btn.setEnabled(available)
        self._btn_clear_setpoints.setEnabled(available)

    def _set_controls_disabled(self, killed):
        available = self.hub.controller is not None
        enabled = available and not killed
        self._btn_toggle_pid.setEnabled(enabled)
        self._btn_send_setpoints.setEnabled(enabled)
        for btn in self._clear_axis_buttons.values():
            btn.setEnabled(enabled)
        self._btn_kill.setEnabled(enabled)
        self._btn_rearm.setEnabled(available and killed)

    # --- feedback -------------------------------------------------------------

    def _set_feedback(self, text, variant="light"):
        color = _FEEDBACK_COLORS.get(variant, "")
        self._setpoint_feedback.setText(text)
        self._setpoint_feedback.setStyleSheet(f"color: {color};" if color else "")

    def _on_error(self, message):
        self.notify(f"PID tuning: {message}")

    # --- override sliders -------------------------------------------------------

    def _reset_all_sliders(self):
        for widget in self._sliders.values():
            widget.reset()

    def _enable_override(self):
        ctrl = self.hub.controller
        if ctrl is None:
            return
        if ctrl.is_killed():
            self.hub.neutralize_thruster_command()
            self.notify("Controls are killed — rearm before overriding.")
            return
        self._override_active = True
        self._badge(self._debug_status_badge, "ACTIVE", "danger")
        self._refresh_enabled()
        self._send_override()
        self._send_timer.start()

    def _send_override(self):
        """50 Hz. set_debug_override is an in-memory write, so the GUI thread is fine here."""
        ctrl = self.hub.controller
        if not self._override_active or ctrl is None:
            return
        if ctrl.is_killed():
            self._disable_override()
            return
        values = {axis: widget.value() for axis, widget in self._sliders.items()}
        ctrl.set_debug_override(logic.debug_override_axes(values))

    def _disable_override(self):
        self._reset_override_ui()
        self.hub.call_async(self._clear_debug_override_work, on_error=self._on_error)

    def _reset_override_ui(self):
        self._override_active = False
        self._send_timer.stop()
        self._badge(self._debug_status_badge, "INACTIVE", "secondary")
        self._refresh_enabled()

    def _clear_debug_override_work(self):
        """Clearing may replay PID setpoints over UDP, so it goes off the GUI thread."""
        hub = self.hub
        ctrl = hub.controller
        if ctrl is not None:
            ctrl.clear_debug_override()
        client = hub.setpoint_override
        if client is None:
            return None
        if ctrl is not None and ctrl.is_pid_enabled():
            return hub.send_active_pid_setpoints()
        return client.clear_override()

    # --- angle setpoints ---------------------------------------------------------

    def _sync_local_setpoints(self, setpoints, clear_missing=True):
        setpoints = setpoints or {}
        for axis in logic.ATTITUDE_AXES:
            value = _safe_float(setpoints.get(axis)) if axis in setpoints else None
            edit = self._setpoint_inputs[axis]
            if value is not None:
                self._local_setpoints[axis] = value
                if not edit.hasFocus():
                    edit.setText(f"{value:.1f}")
            else:
                self._local_setpoints[axis] = None
                if clear_missing and not edit.hasFocus():
                    edit.clear()

    def _use_current(self, axis):
        value = _safe_float(self._latest_imu.get(axis))
        if value is None:
            return
        clamped = logic.coerce_attitude_setpoints({axis: value}).get(axis)
        if clamped is not None:
            self._setpoint_inputs[axis].setText(f"{clamped:.1f}")

    def _read_setpoint_inputs(self):
        raw = {}
        for axis, edit in self._setpoint_inputs.items():
            text = edit.text().strip()
            if not text:
                continue
            try:
                raw[axis] = float(text)
            except ValueError:
                continue
        axes = logic.coerce_attitude_setpoints(raw)
        for axis, value in axes.items():
            self._setpoint_inputs[axis].setText(f"{value:.1f}")
        return axes

    def _send_setpoints(self):
        axes = self._read_setpoint_inputs()
        if not axes:
            self._set_feedback("Enter at least one angle setpoint.", "warning")
            return
        self._badge(self._setpoint_status_badge, "SENDING", "warning")
        self.hub.call_async(
            partial(self._send_setpoints_work, axes),
            on_done=self._on_send_setpoints_done,
            on_error=self._on_send_setpoints_error,
        )

    def _send_setpoints_work(self, axes):
        ctrl = self.hub.controller
        if ctrl is None:
            return {"ok": False, "error": "Controller not available"}
        if ctrl.is_killed():
            return {"ok": False, "error": "Controls are killed", "state": ctrl.get_control_state()}
        setpoints = ctrl.set_pid_setpoints(axes)
        if setpoints is None:
            return {"ok": False, "error": "Controls are killed", "state": ctrl.get_control_state()}
        pid_active = ctrl.is_pid_enabled()
        client = self.hub.setpoint_override
        if pid_active:
            if client is None:
                return {"ok": False, "error": "Setpoint override client unavailable"}
            try:
                state = client.send_override(setpoints, replay_attempts=5, replay_delay=0.1)
            except Exception as exc:  # noqa: BLE001 - mirrors routes.py's broad except
                client.set_error(str(exc))
                return {"ok": False, "error": str(exc)}
        else:
            state = client.get_state() if client else {}
        return {
            "ok": True,
            "sent": setpoints,
            "state": state,
            "control_state": ctrl.get_control_state(),
            "pid_active": pid_active,
        }

    def _on_send_setpoints_done(self, result):
        if result.get("ok"):
            self._sync_local_setpoints(result.get("sent") or {})
            if result.get("control_state"):
                self._update_control_banner(result["control_state"])
            pid_active = result.get("pid_active") is True
            text = ", ".join(f"{axis}={value:.1f}" for axis, value in (result.get("sent") or {}).items())
            self._badge(
                self._setpoint_status_badge, "ACTIVE" if pid_active else "SAVED", "danger" if pid_active else "info"
            )
            prefix = "Updated active setpoints: " if pid_active else "Saved setpoints: "
            self._set_feedback(prefix + text, "success")
        else:
            error = result.get("error") or "Setpoint send failed."
            self._badge(self._setpoint_status_badge, "ERROR", "danger")
            self._set_feedback(error, "danger")
            self.notify(error)
            if result.get("state"):
                self._update_control_banner(result["state"])

    def _on_send_setpoints_error(self, message):
        self._badge(self._setpoint_status_badge, "ERROR", "danger")
        self._set_feedback(f"Error: {message}", "danger")

    def _stop_pid(self, clear):
        ctrl = self.hub.controller
        if ctrl is None:
            self.notify("Controller not available.")
            return
        self.hub.call_async(
            partial(self._stop_pid_work, clear),
            on_done=partial(self._on_stop_pid_done, clear=clear),
            on_error=self._on_error,
        )

    def _stop_pid_work(self, clear):
        ctrl = self.hub.controller
        state = ctrl.stop_pid(clear=clear)
        client = self.hub.setpoint_override
        override_error = None
        if client is not None:
            try:
                client.clear_override()
            except Exception as exc:  # noqa: BLE001 - stopping PID must not fail; surfaced via notify below
                override_error = str(exc)
        return {"state": state, "override_error": override_error}

    def _on_stop_pid_done(self, result, clear):
        state = result.get("state") or {}
        if clear:
            self._sync_local_setpoints({})
        else:
            setpoints = state.get("pid_setpoints")
            if setpoints:
                self._sync_local_setpoints(setpoints, clear_missing=False)
        self._reset_override_ui()
        self._update_control_banner(state)
        self._badge(self._setpoint_status_badge, "IDLE" if clear else "SAVED", "secondary" if clear else "info")
        self._set_feedback("Setpoints cleared." if clear else "PID stopped. Setpoints kept.", "light")
        if result.get("override_error"):
            self.notify(f"PID stopped, but clearing the override was refused: {result['override_error']}")

    def _clear_setpoints(self):
        self._stop_pid(clear=True)

    def _clear_axis(self, axis):
        self.hub.call_async(
            partial(self._clear_axis_work, axis),
            on_done=partial(self._on_clear_axis_done, axis=axis),
            on_error=self._on_error,
        )

    def _clear_axis_work(self, axis):
        ctrl = self.hub.controller
        if ctrl is None:
            return {"ok": False, "error": "Controller not available"}
        remaining = ctrl.clear_pid_setpoint(axis)
        if remaining is None:
            return {"ok": False, "error": "Invalid PID axis"}
        pid_active = ctrl.is_pid_enabled()
        client = self.hub.setpoint_override
        if pid_active:
            if client is None:
                return {"ok": False, "error": "Setpoint override client unavailable"}
            try:
                state = self.hub.send_active_pid_setpoints()
            except Exception as exc:  # noqa: BLE001 - mirrors routes.py's broad except
                client.set_error(str(exc))
                return {"ok": False, "error": str(exc)}
        else:
            state = client.get_state() if client else {}
        return {
            "ok": True,
            "remaining": remaining,
            "state": state,
            "control_state": ctrl.get_control_state(),
        }

    def _on_clear_axis_done(self, result, axis):
        if result.get("ok"):
            self._sync_local_setpoints(result.get("remaining") or {})
            if result.get("control_state"):
                self._update_control_banner(result["control_state"])
            self._set_feedback(f"{axis} setpoint cleared.", "success")
        else:
            error = result.get("error") or "Clear failed."
            self._set_feedback(error, "danger")
            self.notify(error)

    # --- PID start/stop toggle + sanity gate --------------------------------------

    def _toggle_pid(self):
        if self._latest_control_state.get("pid_enabled") is True:
            self._stop_pid(clear=False)
        else:
            self._start_pid(force=False)

    def _force_start_pid(self):
        self._btn_force_start.setEnabled(False)
        self._start_pid(force=True)

    def _start_pid(self, force):
        ctrl = self.hub.controller
        if ctrl is None:
            self.notify("Controller not available.")
            return
        self._btn_toggle_pid.setEnabled(False)
        self._badge(self._setpoint_status_badge, "STARTING", "warning")
        self.hub.call_async(
            partial(self._start_pid_work, force), on_done=self._on_start_pid_done, on_error=self._on_start_pid_error
        )

    def _start_pid_work(self, force):
        """The PID start sanity gate (PARITY.md §3): usable=False is a hard fail, usable but
        not ok is recoverable and reports force_supported so the UI can offer Force Start."""
        hub = self.hub
        ctrl = hub.controller
        imu = hub.imu
        client = hub.setpoint_override
        if ctrl is None:
            return {"ok": False, "error": "Controller not available"}
        if imu is None:
            return {"ok": False, "error": "IMU receiver not running"}
        if client is None:
            return {"ok": False, "error": "Setpoint override client unavailable"}
        if ctrl.is_killed():
            return {"ok": False, "error": "Controls are killed", "state": ctrl.get_control_state()}

        stats = imu.get_stats()
        sanity = logic.imu_attitude_sanity(stats)
        if not sanity["usable"]:
            return {"ok": False, "error": sanity["reason"] or "Current attitude is incomplete", "sanity": sanity}
        if not sanity["ok"] and not force:
            return {"ok": False, "error": sanity["reason"], "sanity": sanity, "force_supported": True}

        pending = ctrl.get_pid_setpoints()
        setpoints = {**sanity["setpoints"], **pending}
        result = ctrl.start_pid(setpoints)
        if result is None:
            return {"ok": False, "error": "Controls are killed", "state": ctrl.get_control_state()}
        try:
            client.clear_override()
            client.send_override(setpoints, replay_attempts=5, replay_delay=0.1)
        except Exception as exc:  # noqa: BLE001 - mirrors routes.py's broad except
            client.set_error(str(exc))
            ctrl.stop_pid(clear=False)
            return {"ok": False, "error": str(exc), "sanity": sanity}
        return {"ok": True, "setpoints": setpoints, "state": ctrl.get_control_state(), "sanity": sanity}

    def _on_start_pid_done(self, result):
        self._btn_toggle_pid.setEnabled(True)
        if result.get("ok"):
            self._btn_force_start.setVisible(False)
            self._sync_local_setpoints(result.get("setpoints") or {})
            if result.get("state"):
                self._update_control_banner(result["state"])
            self._badge(self._setpoint_status_badge, "ACTIVE", "danger")
            self._set_feedback("Started from current IMU attitude.", "success")
        elif result.get("force_supported"):
            self._btn_force_start.setVisible(True)
            self._btn_force_start.setEnabled(True)
            reason = result.get("error") or "IMU sanity check failed."
            self._badge(self._setpoint_status_badge, "NEEDS FORCE", "warning")
            self._set_feedback(f"{reason} Use Force Start to proceed anyway.", "warning")
            self.notify(f"PID start blocked: {reason}")
        else:
            error = result.get("error") or "Start failed."
            self._btn_force_start.setVisible(False)
            self._badge(self._setpoint_status_badge, "BLOCKED", "danger")
            self._set_feedback(error, "danger")
            self.notify(error)
            if result.get("state"):
                self._update_control_banner(result["state"])
        self._refresh_enabled()

    def _on_start_pid_error(self, message):
        self._btn_toggle_pid.setEnabled(True)
        self._badge(self._setpoint_status_badge, "ERROR", "danger")
        self._set_feedback(f"Error: {message}", "danger")
        self._refresh_enabled()

    # --- kill / rearm -----------------------------------------------------------

    def _kill_controls(self):
        ctrl = self.hub.controller
        if ctrl is None:
            self.notify("Controller not available.")
            return
        self.hub.call_async(self._kill_work, on_done=self._on_kill_done, on_error=self._on_error)

    def _kill_work(self):
        """Mirrors POST /api/control/killswitch: kill, zero the MCU's PID gains, clear override."""
        ctrl = self.hub.controller
        state = ctrl.kill()
        zero_gains = logic.zero_pid_gains()
        send_pid_gains(zero_gains, timeout=0.5, max_retries=2)
        client = self.hub.setpoint_override
        override_error = None
        if client is not None:
            try:
                client.clear_override()
            except Exception as exc:  # noqa: BLE001 - the kill must not fail; surfaced via notify below
                override_error = str(exc)
        return {"state": state, "override_error": override_error}

    def _on_kill_done(self, result):
        for widget in self._sliders.values():
            widget.reset()
        self._reset_override_ui()
        self._sync_local_setpoints({})
        self._update_control_banner(result.get("state") or {})
        self._set_feedback("Controls killed.", "danger")
        if result.get("override_error"):
            self.notify(f"Controls killed, but clearing the override was refused: {result['override_error']}")

    def _rearm_controls(self):
        ctrl = self.hub.controller
        if ctrl is None:
            self.notify("Controller not available.")
            return
        self.hub.call_async(self._rearm_work, on_done=self._on_rearm_done, on_error=self._on_error)

    def _rearm_work(self):
        ctrl = self.hub.controller
        state = ctrl.rearm()
        client = self.hub.setpoint_override
        override_error = None
        if client is not None:
            try:
                client.clear_override()
            except Exception as exc:  # noqa: BLE001 - rearm must not fail; surfaced via notify below
                override_error = str(exc)
        return {"state": state, "override_error": override_error}

    def _on_rearm_done(self, result):
        for widget in self._sliders.values():
            widget.reset()
        self._reset_override_ui()
        self._sync_local_setpoints({})
        self._update_control_banner(result.get("state") or {})
        self._set_feedback("Controls re-armed.", "success")
        if result.get("override_error"):
            self.notify(f"Controls re-armed, but clearing the override was refused: {result['override_error']}")

    # --- MCU PID gains ------------------------------------------------------------

    def _read_gain_fields(self):
        return {
            axis: {gain: self._gain_inputs[axis][gain].value() for gain in GAIN_KEYS} for axis in logic.ATTITUDE_AXES
        }

    def _fill_gain_fields(self, gains):
        if not gains:
            return
        for axis in logic.ATTITUDE_AXES:
            axis_gains = gains.get(axis) or {}
            for gain in GAIN_KEYS:
                value = axis_gains.get(gain)
                if value is not None:
                    self._gain_inputs[axis][gain].setValue(float(value))

    def _request_pid_gains(self):
        self._badge(self._gain_sync_badge, "REQUESTING", "warning")
        self._btn_pid_request.setEnabled(False)
        self.hub.call_async(
            partial(request_pid_gains, timeout=2.0),
            on_done=self._on_request_gains_done,
            on_error=self._on_request_gains_error,
        )

    def _on_request_gains_done(self, gains):
        self._btn_pid_request.setEnabled(True)
        if gains is None:
            self._badge(self._gain_sync_badge, "NO RESPONSE", "danger")
            return
        self._fill_gain_fields(logic.attitude_pid_gains(gains))
        self._badge(self._gain_sync_badge, "LOADED", "success")

    def _on_request_gains_error(self, message):
        self._btn_pid_request.setEnabled(True)
        self._badge(self._gain_sync_badge, "ERROR", "danger")
        self.notify(f"PID gain request failed: {message}")

    def _send_pid_gains(self):
        gains = logic.mcu_pid_gains(self._read_gain_fields())
        self._badge(self._gain_sync_badge, "SENDING", "warning")
        self._btn_pid_send.setEnabled(False)
        self.hub.call_async(
            partial(send_pid_gains, gains, timeout=1.0, max_retries=3),
            on_done=self._on_send_gains_done,
            on_error=self._on_send_gains_error,
        )

    def _on_send_gains_done(self, result):
        self._btn_pid_send.setEnabled(True)
        confirmed, attempts = result
        if confirmed is None:
            self._badge(self._gain_sync_badge, "NO RESPONSE", "danger")
            return
        self._fill_gain_fields(logic.attitude_pid_gains(confirmed))
        self._badge(self._gain_sync_badge, f"CONFIRMED RETRY {attempts}" if attempts > 1 else "CONFIRMED", "success")

    def _on_send_gains_error(self, message):
        self._btn_pid_send.setEnabled(True)
        self._badge(self._gain_sync_badge, "ERROR", "danger")
        self.notify(f"PID gain send failed: {message}")

    # --- saved PID config presets (local JSON file, not networked) -----------------

    def _refresh_config_list(self):
        configs = logic.load_pid_configs()
        current = self._pid_config_select.currentData()
        self._pid_config_select.blockSignals(True)
        self._pid_config_select.clear()
        self._pid_config_select.addItem("Load tune", "")
        for name in configs.keys():
            self._pid_config_select.addItem(name, name)
        if current:
            index = self._pid_config_select.findData(current)
            if index >= 0:
                self._pid_config_select.setCurrentIndex(index)
        self._pid_config_select.blockSignals(False)

    def _save_config(self):
        name = self._pid_config_name.text().strip()
        if not name or not logic.valid_name(name):
            self._badge(self._config_status_badge, "Invalid name", "warning")
            return
        gains = logic.mcu_pid_gains(self._read_gain_fields())
        configs = logic.load_pid_configs()
        configs[name] = gains
        logic.save_pid_configs(configs)
        self._badge(self._config_status_badge, "Saved", "success")
        self._refresh_config_list()

    def _load_config(self):
        name = self._pid_config_select.currentData()
        if not name:
            self._badge(self._config_status_badge, "Select", "warning")
            return
        configs = logic.load_pid_configs()
        if name not in configs:
            self._badge(self._config_status_badge, "Missing", "danger")
            return
        self._fill_gain_fields(logic.attitude_pid_gains(configs[name]))
        self._pid_config_name.setText(name)
        self._badge(self._config_status_badge, "Loaded", "success")

    def _disarm_delete(self):
        self._delete_armed = False
        self._delete_arm_timer.stop()
        self._btn_pid_delete.setText("Delete")

    def _delete_config(self):
        """First click arms, second click deletes — the non-blocking form of window.confirm()."""
        name = self._pid_config_select.currentData()
        if not name:
            self._badge(self._config_status_badge, "Select", "warning")
            return

        if not self._delete_armed:
            self._delete_armed = True
            self._btn_pid_delete.setText("Confirm delete?")
            self._badge(self._config_status_badge, "Confirm", "warning")
            self._delete_arm_timer.start(DELETE_ARM_TIMEOUT_MS)
            return
        self._disarm_delete()

        configs = logic.load_pid_configs()
        if name not in configs:
            self._badge(self._config_status_badge, "Missing", "danger")
            return
        del configs[name]
        logic.save_pid_configs(configs)
        self._badge(self._config_status_badge, "Deleted", "success")
        self._refresh_config_list()


#: The two read-only panels are safe to duplicate (nothing here writes to the vehicle); the four
#: write panels stay single-instance -- see each panel's docstring and the module docstring for
#: why. Opened standalone (outside this screen) a write panel's controls are disabled rather than
#: re-running hardware-writing logic through a second code path.
COMPONENTS = [
    Component(
        id="panel.pid_tuning.action_bar",
        title="PID Action Bar",
        factory=lambda hub: ActionBarPanel(hub),
        category="PID",
        duplicable=False,
        order=0,
    ),
    Component(
        id="panel.pid_tuning.readouts",
        title="PID Axis Readouts",
        factory=lambda hub: ReadoutsPanel(hub),
        category="PID",
        duplicable=True,
        order=1,
    ),
    Component(
        id="panel.pid_tuning.override",
        title="PID Override Controls",
        factory=lambda hub: OverridePanel(hub),
        category="PID",
        duplicable=False,
        order=2,
    ),
    Component(
        id="panel.pid_tuning.setpoints",
        title="PID Angle Setpoints",
        factory=lambda hub: SetpointsPanel(hub),
        category="PID",
        duplicable=False,
        order=3,
    ),
    Component(
        id="panel.pid_tuning.gains",
        title="PID Values",
        factory=lambda hub: GainsPanel(hub),
        category="PID",
        duplicable=False,
        order=4,
    ),
    Component(
        id="panel.pid_tuning.telemetry",
        title="PID Telemetry and Link",
        factory=lambda hub: TelemetryPanel(hub),
        category="PID",
        duplicable=True,
        order=5,
    ),
]
