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

from desktop import logic
from desktop.screens.base import ScreenBase
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

_BADGE_COLORS = {
    "secondary": ("#6c757d", "white"),
    "success": ("#198754", "white"),
    "danger": ("#dc3545", "white"),
    "warning": ("#c98a00", "black"),
    "info": ("#0dcaf0", "black"),
}

_FEEDBACK_COLORS = {
    "success": "#198754",
    "danger": "#dc3545",
    "warning": "#c98a00",
    "info": "#0dcaf0",
    "light": "",
}


class PidTuningScreen(ScreenBase):
    title = "PID Tuning"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._override_active = False
        self._local_setpoints = {axis: None for axis in logic.ATTITUDE_AXES}
        self._latest_imu = {}
        self._latest_telemetry = None
        self._latest_control_state = {}

        root = QVBoxLayout(self)
        root.addWidget(self.notice_widget)
        root.addLayout(self._build_action_bar())
        root.addLayout(self._build_readouts())

        columns = QHBoxLayout()
        columns.addWidget(self._build_left_stack(), 1)
        columns.addWidget(self._build_right_stack(), 1)
        root.addLayout(columns, 1)

        # A plain QTimer, not a hub poller: this is a local write loop (matches debug.py).
        self._send_timer = QTimer(self)
        self._send_timer.setInterval(SEND_INTERVAL_MS)
        self._send_timer.timeout.connect(self._send_override)

        self.watch(
            "pid.control_state", self._read_control_state, CONTROL_STATE_INTERVAL_MS, self._on_control_state_update
        )
        self.watch("pid.imu_telemetry", self._read_imu_telemetry, IMU_TELEMETRY_INTERVAL_MS, self._on_imu_telemetry)
        self.watch("pid.rov_status", self._read_rov_status, ROV_STATUS_INTERVAL_MS, self._on_rov_status)

        self._refresh_enabled()

    # --- UI construction ------------------------------------------------------

    def _build_action_bar(self):
        layout = QHBoxLayout()

        status_row = QHBoxLayout()
        self._control_path_label = QLabel("PS4 Controller")
        status_row.addWidget(self._make_status_card("Active control", self._control_path_label))

        self._pid_mode_badge = self._make_badge("OFF", "secondary")
        status_row.addWidget(self._make_status_card("PID", self._pid_mode_badge))

        self._branch_label = QLabel("--")
        status_row.addWidget(self._make_status_card("Branch", self._branch_label))

        self._imu_age_label = QLabel("--")
        status_row.addWidget(self._make_status_card("IMU age (ms)", self._imu_age_label))
        layout.addLayout(status_row, 1)

        controls = QHBoxLayout()
        self._btn_toggle_pid = QPushButton("Start PID")
        self._btn_toggle_pid.clicked.connect(self._toggle_pid)
        self._btn_force_start = QPushButton("Force Start")
        self._btn_force_start.setVisible(False)
        self._btn_force_start.clicked.connect(self._force_start_pid)
        self._btn_rearm = QPushButton("Re-arm")
        self._btn_rearm.clicked.connect(self._rearm_controls)
        self._btn_kill = QPushButton("KILLSWITCH")
        self._btn_kill.clicked.connect(self._kill_controls)
        controls.addWidget(self._btn_toggle_pid)
        controls.addWidget(self._btn_force_start)
        controls.addWidget(self._btn_rearm)
        controls.addWidget(self._btn_kill)
        layout.addLayout(controls)
        return layout

    @staticmethod
    def _make_status_card(title, value_widget):
        box = QGroupBox()
        v = QVBoxLayout(box)
        v.addWidget(QLabel(f"<small>{title}</small>"))
        v.addWidget(value_widget)
        return box

    def _make_badge(self, text, variant):
        label = QLabel()
        self._badge(label, text, variant)
        return label

    def _badge(self, label, text, variant="secondary"):
        bg, fg = _BADGE_COLORS.get(variant, _BADGE_COLORS["secondary"])
        label.setText(text)
        label.setStyleSheet(f"background-color:{bg}; color:{fg}; padding:2px 10px; border-radius:4px; font-weight:600;")

    def _build_readouts(self):
        layout = QHBoxLayout()
        self._readout_labels = {}
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
            self._readout_labels[axis] = {"position": pos_label, "setpoint": set_label, "error": err_label}
            layout.addWidget(box)
        return layout

    def _build_left_stack(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._build_override_panel())
        layout.addWidget(self._build_setpoints_panel())
        layout.addStretch(1)
        return widget

    def _build_right_stack(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._build_gains_panel())
        layout.addWidget(self._build_telemetry_panel(), 1)
        return widget

    def _build_override_panel(self):
        box = QGroupBox("Override Controls")
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self._debug_status_badge = self._make_badge("INACTIVE", "secondary")
        header.addWidget(self._debug_status_badge)
        layout.addLayout(header)

        buttons = QHBoxLayout()
        self._btn_enable = QPushButton("Enable Override")
        self._btn_enable.clicked.connect(self._enable_override)
        self._btn_disable = QPushButton("Disable Override")
        self._btn_disable.setEnabled(False)
        self._btn_disable.clicked.connect(self._disable_override)
        self._btn_reset_all = QPushButton("Reset Sliders")
        self._btn_reset_all.clicked.connect(self._reset_all_sliders)
        buttons.addWidget(self._btn_enable)
        buttons.addWidget(self._btn_disable)
        buttons.addWidget(self._btn_reset_all)
        layout.addLayout(buttons)

        grid = QGridLayout()
        self._sliders = {}
        for index, (axis, label) in enumerate(AXIS_LABELS):
            widget = AxisSlider(axis, label)
            self._sliders[axis] = widget
            grid.addWidget(widget, index // 3, index % 3)
        layout.addLayout(grid)
        return box

    def _build_setpoints_panel(self):
        box = QGroupBox("Angle Setpoints")
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self._setpoint_status_badge = self._make_badge("IDLE", "secondary")
        header.addWidget(self._setpoint_status_badge)
        layout.addLayout(header)

        note = QLabel("VN-100 YPR degrees: roll and yaw use -180..+180. Pitch uses -90..+90.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._setpoint_inputs = {}
        self._use_current_buttons = {}
        self._clear_axis_buttons = {}
        form = QGridLayout()
        for row, (axis, label) in enumerate(ROT_AXIS_LABELS):
            limit = logic.ATTITUDE_LIMITS_DEG[axis]
            edit = QLineEdit()
            edit.setPlaceholderText("0.0")
            edit.setValidator(QDoubleValidator(-limit, limit, 2, edit))
            self._setpoint_inputs[axis] = edit

            use_btn = QPushButton("Use Current")
            use_btn.clicked.connect(partial(self._use_current, axis))
            self._use_current_buttons[axis] = use_btn

            clear_btn = QPushButton("Clear")
            clear_btn.clicked.connect(partial(self._clear_axis, axis))
            self._clear_axis_buttons[axis] = clear_btn

            form.addWidget(QLabel(label), row, 0)
            form.addWidget(edit, row, 1)
            form.addWidget(use_btn, row, 2)
            form.addWidget(clear_btn, row, 3)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self._btn_send_setpoints = QPushButton("Save Setpoints")
        self._btn_send_setpoints.clicked.connect(self._send_setpoints)
        self._btn_clear_setpoints = QPushButton("Stop and Clear")
        self._btn_clear_setpoints.clicked.connect(self._clear_setpoints)
        buttons.addWidget(self._btn_send_setpoints)
        buttons.addWidget(self._btn_clear_setpoints)
        layout.addLayout(buttons)

        self._setpoint_feedback = QLabel("Waiting for input.")
        self._setpoint_feedback.setWordWrap(True)
        layout.addWidget(self._setpoint_feedback)
        return box

    def _build_gains_panel(self):
        box = QGroupBox("PID Values")
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self._gain_sync_badge = self._make_badge("READY", "secondary")
        header.addWidget(self._gain_sync_badge)
        layout.addLayout(header)

        buttons = QHBoxLayout()
        self._btn_pid_request = QPushButton("Request Current")
        self._btn_pid_request.clicked.connect(self._request_pid_gains)
        self._btn_pid_send = QPushButton("Send to MCU")
        self._btn_pid_send.clicked.connect(self._send_pid_gains)
        buttons.addWidget(self._btn_pid_request)
        buttons.addWidget(self._btn_pid_send)
        layout.addLayout(buttons)

        grid = QGridLayout()
        grid.addWidget(QLabel("Axis"), 0, 0)
        grid.addWidget(QLabel("P"), 0, 1)
        grid.addWidget(QLabel("I"), 0, 2)
        grid.addWidget(QLabel("D"), 0, 3)
        self._gain_inputs = {}
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
            self._gain_inputs[axis] = axis_inputs
        layout.addLayout(grid)

        config_row = QHBoxLayout()
        self._pid_config_name = QLineEdit()
        self._pid_config_name.setPlaceholderText("Tune name")
        self._btn_pid_save = QPushButton("Save")
        self._btn_pid_save.clicked.connect(self._save_config)
        self._pid_config_select = QComboBox()
        self._pid_config_select.addItem("Load tune", "")
        self._btn_pid_load = QPushButton("Load")
        self._btn_pid_load.clicked.connect(self._load_config)
        self._btn_pid_delete = QPushButton("Delete")
        self._btn_pid_delete.clicked.connect(self._delete_config)
        self._delete_armed = False
        self._delete_arm_timer = QTimer(self)
        self._delete_arm_timer.setSingleShot(True)
        self._delete_arm_timer.timeout.connect(self._disarm_delete)
        self._config_status_badge = self._make_badge("-", "secondary")
        config_row.addWidget(self._pid_config_name)
        config_row.addWidget(self._btn_pid_save)
        config_row.addWidget(self._pid_config_select)
        config_row.addWidget(self._btn_pid_load)
        config_row.addWidget(self._btn_pid_delete)
        config_row.addWidget(self._config_status_badge)
        layout.addLayout(config_row)
        return box

    def _build_telemetry_panel(self):
        box = QGroupBox("Telemetry and Link")
        layout = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addStretch(1)
        self._telemetry_age_badge = self._make_badge("NO TELEMETRY", "secondary")
        header.addWidget(self._telemetry_age_badge)
        layout.addLayout(header)

        self._telemetry_table = QTableWidget(len(ROT_AXIS_LABELS), 7)
        self._telemetry_table.setHorizontalHeaderLabels(
            ["Axis", "Mode", "Setpoint", "MCU Measure", "Error", "Output", "Gains"]
        )
        self._telemetry_table.verticalHeader().setVisible(False)
        self._telemetry_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self._telemetry_table)

        self._rov_status_view = QPlainTextEdit()
        self._rov_status_view.setReadOnly(True)
        self._rov_status_view.setPlainText("Loading...")
        layout.addWidget(self._rov_status_view, 1)
        return box

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
        ctrl = self.hub.controller
        return ctrl.get_control_state() if ctrl else None

    def _on_control_state_update(self, state):
        if state is None:
            self._control_path_label.setText("Controller unavailable")
            self._refresh_enabled()
            return
        self._update_control_banner(state)

    # --- polling: IMU + control telemetry -----------------------------------

    def _read_imu_telemetry(self):
        hub = self.hub
        imu_stats = hub.imu.get_stats() if hub.imu else None
        telemetry = hub.control_telem.get_latest() if hub.control_telem else None
        return {"imu": imu_stats, "telemetry": telemetry}

    def _on_imu_telemetry(self, payload):
        imu_stats = payload.get("imu")
        if imu_stats:
            data = imu_stats.get("last_data") or {}
            self._latest_imu = {axis: self._safe_float(data.get(axis)) for axis in logic.ATTITUDE_AXES}
            age = imu_stats.get("age_ms")
            self._imu_age_label.setText(str(age) if age is not None else "--")
        else:
            self._imu_age_label.setText("--")
        self._latest_telemetry = payload.get("telemetry") or None
        self._update_telemetry_age(self._latest_telemetry)
        self._update_telemetry_table()

    # --- polling: rov status / debug view ------------------------------------

    def _read_rov_status(self):
        hub = self.hub
        udp_rx, udp_err = hub.resource.get_udp_counters() if hub.resource else (0, 0)
        ctrl = hub.controller
        return {
            "command": hub.bitmask.get_command() if hub.bitmask else {},
            "uplink": hub.bitmask.get_uplink_status() if hub.bitmask else {},
            "control_state": ctrl.get_control_state() if ctrl else {},
            "resource": {"udp_rx_count": udp_rx, "udp_rx_errors": udp_err},
        }

    def _on_rov_status(self, status):
        control_state = status.get("control_state")
        if control_state:
            self._update_control_banner(control_state)
        payload = self._build_debug_payload(status, control_state or self._latest_control_state)
        self._rov_status_view.setPlainText(json.dumps(payload, indent=2, default=str))

    def _build_debug_payload(self, status, control):
        control = control or {}
        uplink = status.get("uplink") or {}
        telemetry = self._latest_telemetry or {}
        return {
            "control_path": self._control_path_text(control.get("control_path"), control.get("killed") is True),
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

    # --- control banner / readouts ------------------------------------------

    @staticmethod
    def _control_path_text(path, killed):
        if killed:
            return "Controls locked"
        if path == "Override Controls":
            return "Override sliders"
        if path == "PS4":
            return "PS4 Controller"
        return path or "PS4 Controller"

    def _update_control_banner(self, state):
        self._latest_control_state = state or {}
        killed = self._latest_control_state.get("killed") is True
        pid_on = self._latest_control_state.get("pid_enabled") is True
        path = self._latest_control_state.get("control_path") or "PS4"
        setpoints = self._latest_control_state.get("pid_setpoints") or {}
        has_setpoints = any(self._safe_float(setpoints.get(axis)) is not None for axis in logic.ATTITUDE_AXES)
        self._sync_local_setpoints(setpoints, clear_missing=False)

        self._control_path_label.setText(self._control_path_text(path, killed))
        self._badge(self._pid_mode_badge, "ON" if pid_on else "OFF", "success" if pid_on else "secondary")
        if pid_on:
            self._badge(self._setpoint_status_badge, "ACTIVE", "danger")
        elif has_setpoints:
            self._badge(self._setpoint_status_badge, "SAVED", "info")
        else:
            self._badge(self._setpoint_status_badge, "IDLE", "secondary")
        self._btn_toggle_pid.setText("Stop PID" if pid_on else "Start PID")
        self._update_axis_readouts()
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

    # --- axis readouts / telemetry table -------------------------------------

    @staticmethod
    def _safe_float(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @classmethod
    def _fmt(cls, value, digits=2):
        value = cls._safe_float(value)
        return "--" if value is None else f"{value:.{digits}f}"

    def _telemetry_setpoint(self, axis):
        telemetry = self._latest_telemetry or {}
        from_telem = self._safe_float((telemetry.get("setpoint") or {}).get(axis))
        from_local = self._local_setpoints.get(axis)
        if self._latest_control_state.get("pid_enabled") is True and from_telem is not None:
            return from_telem
        if from_local is not None:
            return from_local
        return from_telem

    def _axis_measurement(self, axis):
        telemetry = self._latest_telemetry or {}
        from_telem = self._safe_float((telemetry.get("measurement") or {}).get(axis))
        if from_telem is not None:
            return from_telem
        return self._safe_float(self._latest_imu.get(axis))

    def _axis_error(self, axis, setpoint, position):
        telemetry = self._latest_telemetry or {}
        from_telem = self._safe_float((telemetry.get("error") or {}).get(axis))
        if from_telem is not None:
            return from_telem
        if setpoint is None or position is None:
            return None
        if axis == "pitch":
            return setpoint - position
        return logic.normalize_angle_deg(setpoint - position)

    def _axis_output(self, axis):
        telemetry = self._latest_telemetry or {}
        return self._safe_float((telemetry.get("output") or {}).get(axis))

    def _axis_gains_text(self, axis):
        telemetry = self._latest_telemetry or {}
        gains = (telemetry.get("gains") or {}).get(axis)
        if not gains:
            return "--"
        return f"P {self._fmt(gains.get('kp'))} I {self._fmt(gains.get('ki'))} D {self._fmt(gains.get('kd'))}"

    def _axis_mode(self, axis):
        telemetry = self._latest_telemetry
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

    def _update_axis_readouts(self):
        for axis in logic.ATTITUDE_AXES:
            setpoint = self._telemetry_setpoint(axis)
            position = self._axis_measurement(axis)
            error = self._axis_error(axis, setpoint, position)
            labels = self._readout_labels[axis]
            labels["position"].setText(self._fmt(position))
            labels["setpoint"].setText(self._fmt(setpoint))
            labels["error"].setText(self._fmt(error))
            if error is None:
                labels["error"].setStyleSheet("color: gray;")
            elif abs(error) > 25:
                labels["error"].setStyleSheet("color: #dc3545; font-weight: bold;")
            elif abs(error) > 10:
                labels["error"].setStyleSheet("color: #c98a00; font-weight: bold;")
            else:
                labels["error"].setStyleSheet("")

    def _update_telemetry_table(self):
        self._update_axis_readouts()
        table = self._telemetry_table
        timeout = bool(self._latest_telemetry and (self._latest_telemetry.get("flags") or {}).get("timeout"))
        for row, axis in enumerate(logic.ATTITUDE_AXES):
            setpoint = self._telemetry_setpoint(axis)
            position = self._axis_measurement(axis)
            error = self._axis_error(axis, setpoint, position)
            output = self._axis_output(axis)
            values = [
                axis.upper(),
                self._axis_mode(axis),
                self._fmt(setpoint),
                self._fmt(position),
                self._fmt(error),
                self._fmt(output),
                self._axis_gains_text(axis),
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

    def _update_telemetry_age(self, telemetry):
        if not telemetry or telemetry.get("timestamp") is None:
            self._badge(self._telemetry_age_badge, "NO TELEMETRY", "secondary")
            return
        age_ms = max(0.0, (time.time() - telemetry["timestamp"]) * 1000.0)
        flags = telemetry.get("flags") or {}
        if flags.get("timeout"):
            self._badge(self._telemetry_age_badge, "NUCLEO TIMEOUT", "danger")
        elif age_ms < 750:
            self._badge(self._telemetry_age_badge, f"{age_ms:.0f} ms", "success")
        elif age_ms < 2500:
            self._badge(self._telemetry_age_badge, f"{age_ms:.0f} ms", "warning")
        else:
            self._badge(self._telemetry_age_badge, "STALE", "danger")

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
            value = self._safe_float(setpoints.get(axis)) if axis in setpoints else None
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
        value = self._safe_float(self._latest_imu.get(axis))
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
