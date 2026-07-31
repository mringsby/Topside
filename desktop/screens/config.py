"""Config screen — ports `/config` (`config.html` + `configuration.js`).

Covers the Input Source status card, controller gain sliders, PID setpoint rates, IMU and
accelerometer axis mapping, and the IMU offset from mass center.

Any IMU axis or offset change must re-send the FULL axis config packet via
`hub.send_full_axis_config()` — the MCU takes remap and offset together in one packet, so a
partial update is a wire-protocol bug (see CLAUDE.md Invariants and PARITY.md §3 "IMU config").
`send_axis_config` (used by that hub method) is a fire-and-forget UDP send with no reply wait,
so — like `configuration.js`'s synchronous-looking saves — it is called directly from the slot,
not through `hub.call_async`; it is not in the PARITY.md §1 blocking-call table.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from desktop import logic
from desktop.screens.base import ScreenBase

STATUS_INTERVAL_MS = 1000
GAIN_SAVE_DEBOUNCE_MS = 250
#: How long to wait after send_full_axis_config() before checking the 9DOF stream is still alive.
AXIS_PROBE_DELAY_MS = 750

IMU_AXIS_OPTIONS = {
    "yaw": ["+yaw", "-yaw", "+pitch", "-pitch", "+roll", "-roll"],
    "pitch": ["+pitch", "-pitch", "+yaw", "-yaw", "+roll", "-roll"],
    "roll": ["+roll", "-roll", "+yaw", "-yaw", "+pitch", "-pitch"],
}
ACCEL_AXIS_OPTIONS = ["+x", "-x", "+y", "-y", "+z", "-z"]

_STATUS_STYLES = {
    "neutral": "color: palette(text);",
    "good": "color: #3fb950; font-weight: 600;",
    "bad": "color: #f85149; font-weight: 600;",
    "warn": "color: #d29922; font-weight: 600;",
}


def _badge(text="", tone="neutral"):
    label = QLabel(text)
    label.setStyleSheet(_STATUS_STYLES[tone])
    return label


class _GainSlider(QWidget):
    """One 0..100% gain slider with a live percentage label. Value is 0.0..1.0."""

    def __init__(self, label_text, axis=None, parent=None):
        super().__init__(parent)
        self.axis = axis  # None for the master slider

        self._value_label = QLabel("100%")
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(100)

        header = QHBoxLayout()
        header.addWidget(QLabel(label_text))
        header.addStretch(1)
        header.addWidget(self._value_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.slider)

    def value(self):
        return self.slider.value() / 100.0

    def set_value(self, gain):
        self.slider.setValue(round(logic.clamp(float(gain), 0.0, 1.0) * 100))

    def refresh_label(self):
        self._value_label.setText(f"{round(self.value() * 100)}%")


class ConfigScreen(ScreenBase):
    title = "Config"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addWidget(self._build_input_source_box())
        layout.addWidget(self._build_gain_box())
        layout.addWidget(self._build_pid_rates_box())
        layout.addWidget(self._build_imu_axes_box())
        layout.addWidget(self._build_accel_axes_box())
        layout.addWidget(self._build_offset_box())
        layout.addStretch(1)

        self._gain_save_timer = QTimer(self)
        self._gain_save_timer.setSingleShot(True)
        self._gain_save_timer.timeout.connect(self._save_gains)

        self.watch("command.status", self._command_status, STATUS_INTERVAL_MS, self._on_command_status)

    # --- Input Source ---------------------------------------------------------

    def _build_input_source_box(self):
        box = QGroupBox("Input Source")
        self._input_badge = _badge("UNKNOWN")
        self._input_controller = QLabel("--")
        self._input_override = QLabel("--")
        self._input_last_ack = QLabel("--")

        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self._input_badge)

        grid = QGridLayout()
        grid.addWidget(QLabel("Controller"), 0, 0)
        grid.addWidget(QLabel("Override"), 0, 1)
        grid.addWidget(QLabel("Last Ack"), 0, 2)
        grid.addWidget(self._input_controller, 1, 0)
        grid.addWidget(self._input_override, 1, 1)
        grid.addWidget(self._input_last_ack, 1, 2)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addLayout(grid)
        return box

    def _command_status(self):
        """Replaces GET /api/command/status. Cheap in-memory reads only."""
        hub = self.hub
        uplink = hub.bitmask.get_uplink_status() if hub.bitmask else {}
        controller_state = hub.controller.get_input_status() if hub.controller else {}
        override_state = hub.setpoint_override.get_state() if hub.setpoint_override else {}
        return {"uplink": uplink, "controller": controller_state, "override": override_state}

    def _on_command_status(self, status):
        controller = status.get("controller", {})
        override = status.get("override", {})
        uplink = status.get("uplink", {})

        connected = controller.get("connected") is True
        active_override = override.get("active") is True
        ack_age = uplink.get("last_ack_age_ms")

        self._input_controller.setText("Connected" if connected else "Not active")
        self._input_override.setText("Active" if active_override else "Inactive")
        self._input_last_ack.setText("--" if ack_age is None else f"{round(ack_age)} ms")

        if active_override:
            self._input_badge.setText("OVERRIDE")
            self._input_badge.setStyleSheet(_STATUS_STYLES["bad"])
        elif connected:
            self._input_badge.setText("CONTROLLER")
            self._input_badge.setStyleSheet(_STATUS_STYLES["good"])
        else:
            self._input_badge.setText("IDLE")
            self._input_badge.setStyleSheet(_STATUS_STYLES["neutral"])

    # --- Controller gain --------------------------------------------------------

    def _build_gain_box(self):
        box = QGroupBox("Controller Gain")
        self._gain_badge = _badge("LOADING")
        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self._gain_badge)

        self._gain_master = _GainSlider("Master")
        self._gain_master.slider.valueChanged.connect(self._on_gain_changed)

        grid = QGridLayout()
        self._gain_axes = {}
        for index, axis in enumerate(logic.CONTROL_AXES):
            widget = _GainSlider(axis.capitalize(), axis=axis)
            widget.slider.valueChanged.connect(self._on_gain_changed)
            self._gain_axes[axis] = widget
            grid.addWidget(widget, index // 3, index % 3)

        self._gain_feedback = QLabel("")
        btn_reset = QPushButton("Reset 100%")
        btn_reset.clicked.connect(self._reset_gains)

        footer = QHBoxLayout()
        footer.addWidget(self._gain_feedback)
        footer.addStretch(1)
        footer.addWidget(btn_reset)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._gain_master)
        outer.addLayout(grid)
        outer.addLayout(footer)
        return box

    def _read_gains(self):
        return {
            "master": self._gain_master.value(),
            "axes": {axis: widget.value() for axis, widget in self._gain_axes.items()},
        }

    def _fill_gains(self, gains):
        gains = gains or {}
        axes = gains.get("axes") or {}
        self._gain_master.set_value(1.0 if gains.get("master") is None else gains["master"])
        for axis, widget in self._gain_axes.items():
            widget.set_value(1.0 if axes.get(axis) is None else axes[axis])
        self._gain_master.refresh_label()
        for widget in self._gain_axes.values():
            widget.refresh_label()

    def _on_gain_changed(self, _raw):
        self._gain_master.refresh_label()
        for widget in self._gain_axes.values():
            widget.refresh_label()
        self._gain_badge.setText("CHANGED")
        self._gain_badge.setStyleSheet(_STATUS_STYLES["warn"])
        self._gain_feedback.setText("Saving...")
        self._gain_save_timer.start(GAIN_SAVE_DEBOUNCE_MS)

    def _save_gains(self):
        try:
            cleaned = self.hub.save_controller_gains(self._read_gains())
        except Exception as exc:  # settings IO should not crash the screen
            self._gain_badge.setText("ERROR")
            self._gain_badge.setStyleSheet(_STATUS_STYLES["bad"])
            self._gain_feedback.setText(f"Error: {exc}")
            return
        self._fill_gains(cleaned)
        self._gain_badge.setText("SAVED")
        self._gain_badge.setStyleSheet(_STATUS_STYLES["good"])
        self._gain_feedback.setText("Controller gain saved")

    def _reset_gains(self):
        self._fill_gains({"master": 1.0, "axes": {axis: 1.0 for axis in logic.CONTROL_AXES}})
        self._save_gains()

    # --- PID setpoint rates -------------------------------------------------------

    def _build_pid_rates_box(self):
        box = QGroupBox("PID Setpoint Rates")
        grid = QGridLayout()
        self._pid_rate_fields = {}
        for col, axis in enumerate(logic.ATTITUDE_AXES):
            grid.addWidget(QLabel(f"{axis.capitalize()} (deg/s)"), 0, col)
            field = QDoubleSpinBox()
            field.setRange(0.0, 90.0)
            field.setDecimals(0)
            field.setSingleStep(1.0)
            field.setValue(90.0)
            self._pid_rate_fields[axis] = field
            grid.addWidget(field, 1, col)

        btn_save = QPushButton("Save Rates")
        btn_save.clicked.connect(self._save_pid_rates)
        grid.addWidget(btn_save, 1, len(logic.ATTITUDE_AXES))

        self._pid_rates_feedback = QLabel("")

        outer = QVBoxLayout(box)
        outer.addLayout(grid)
        outer.addWidget(self._pid_rates_feedback)
        return box

    def _load_pid_rates(self):
        rates = logic.load_pid_rates()
        if self.hub.controller:
            self.hub.controller.set_pid_rates(rates)
        for axis, field in self._pid_rate_fields.items():
            field.setValue(rates.get(axis, 90.0))

    def _save_pid_rates(self):
        data = {axis: field.value() for axis, field in self._pid_rate_fields.items()}
        try:
            rates = self.hub.save_pid_rates(data)
        except Exception as exc:
            self._pid_rates_feedback.setText(f"Error: {exc}")
            return
        for axis, field in self._pid_rate_fields.items():
            field.setValue(rates.get(axis, 90.0))
        self._pid_rates_feedback.setText("Rates saved")

    # --- IMU axis mapping -----------------------------------------------------

    def _build_imu_axes_box(self):
        box = QGroupBox("IMU Axis Mapping")
        grid = QGridLayout()
        self._imu_axis_combos = {}
        for col, axis in enumerate(logic.ATTITUDE_AXES):
            grid.addWidget(QLabel(f"ROV {axis.capitalize()} reads from"), 0, col)
            combo = QComboBox()
            combo.addItems(IMU_AXIS_OPTIONS[axis])
            self._imu_axis_combos[axis] = combo
            grid.addWidget(combo, 1, col)

        btn_save = QPushButton("Save Mapping")
        btn_save.clicked.connect(self._save_imu_axes)
        grid.addWidget(btn_save, 1, len(logic.ATTITUDE_AXES))

        self._axes_feedback = QLabel("")

        outer = QVBoxLayout(box)
        outer.addLayout(grid)
        outer.addWidget(self._axes_feedback)
        return box

    def _load_imu_axes(self):
        axes = logic.load_imu_axes()
        for axis, combo in self._imu_axis_combos.items():
            value = axes.get(axis)
            if value is not None:
                index = combo.findText(value)
                if index >= 0:
                    combo.setCurrentIndex(index)

    def _save_imu_axes(self):
        data = {axis: combo.currentText() for axis, combo in self._imu_axis_combos.items()}
        axes = logic.load_imu_axes()
        for key in logic.ATTITUDE_AXES:
            if data.get(key) in logic.VALID_IMU_AXES:
                axes[key] = data[key]
        try:
            logic.config_handler.update_data({"imu_axes": axes})
            if self.hub.imu:
                self.hub.imu.set_axis_mapping(axes)
            self.hub.send_full_axis_config()
        except Exception as exc:
            self._axes_feedback.setText(f"Error: {exc}")
            return
        self._start_axis_probe(self._axes_feedback, "Mapping saved")

    # --- Accelerometer axis mapping -----------------------------------------------

    def _build_accel_axes_box(self):
        box = QGroupBox("Accelerometer Axis Mapping")
        grid = QGridLayout()
        self._accel_axis_combos = {}
        for col, axis in enumerate(("x", "y", "z")):
            grid.addWidget(QLabel(f"ROV {axis.upper()} reads from"), 0, col)
            combo = QComboBox()
            combo.addItems(ACCEL_AXIS_OPTIONS)
            self._accel_axis_combos[axis] = combo
            grid.addWidget(combo, 1, col)

        btn_save = QPushButton("Save Mapping")
        btn_save.clicked.connect(self._save_accel_axes)
        grid.addWidget(btn_save, 1, 3)

        self._accel_feedback = QLabel("")

        outer = QVBoxLayout(box)
        outer.addLayout(grid)
        outer.addWidget(self._accel_feedback)
        return box

    def _load_accel_axes(self):
        axes = logic.load_accel_axes()
        for axis, combo in self._accel_axis_combos.items():
            value = axes.get(axis)
            if value is not None:
                index = combo.findText(value)
                if index >= 0:
                    combo.setCurrentIndex(index)

    def _save_accel_axes(self):
        data = {axis: combo.currentText() for axis, combo in self._accel_axis_combos.items()}
        axes = logic.load_accel_axes()
        for key in ("x", "y", "z"):
            if data.get(key) in logic.VALID_ACCEL_AXES:
                axes[key] = data[key]
        try:
            logic.config_handler.update_data({"accel_axes": axes})
            if self.hub.imu:
                self.hub.imu.set_accel_mapping(axes)
            self.hub.send_full_axis_config()
        except Exception as exc:
            self._accel_feedback.setText(f"Error: {exc}")
            return
        self._start_axis_probe(self._accel_feedback, "Accelerometer mapping saved")

    # --- IMU offset from mass center -----------------------------------------------

    def _build_offset_box(self):
        box = QGroupBox("IMU Offset from Mass Center")
        grid = QGridLayout()
        labels = {"x": "X forward +", "y": "Y starboard +", "z": "Z down +"}
        self._offset_fields = {}
        for col, axis in enumerate(("x", "y", "z")):
            grid.addWidget(QLabel(f"{labels[axis]} (mm)"), 0, col)
            field = QDoubleSpinBox()
            field.setRange(-100000.0, 100000.0)
            field.setDecimals(1)
            field.setSingleStep(0.1)
            self._offset_fields[axis] = field
            grid.addWidget(field, 1, col)

        btn_save = QPushButton("Save Offset")
        btn_save.clicked.connect(self._save_offset)
        grid.addWidget(btn_save, 1, 3)

        self._offset_feedback = QLabel("")

        outer = QVBoxLayout(box)
        outer.addLayout(grid)
        outer.addWidget(self._offset_feedback)
        return box

    def _load_offset(self):
        offset = logic.load_imu_offset()
        for axis, field in self._offset_fields.items():
            field.setValue(float(offset.get(axis, 0.0)))

    def _save_offset(self):
        offset = logic.load_imu_offset()
        for axis, field in self._offset_fields.items():
            offset[axis] = round(field.value(), 1)
        try:
            logic.config_handler.update_data({"imu_offset": offset})
            self.hub.send_full_axis_config()
        except Exception as exc:
            self._offset_feedback.setText(f"Error: {exc}")
            return
        self._start_axis_probe(self._offset_feedback, "Offset saved")

    # --- axis config liveness probe ------------------------------------------------

    def _start_axis_probe(self, feedback_label, success_text):
        """After a successful send_full_axis_config(), check the MCU is still alive.

        UDP 5004 has no ACK: a dropped packet silently leaves the MCU on the old axis remap
        while this screen says "sent". We CANNOT confirm the remap was applied correctly from
        here -- that needs the vehicle physically moved. What we CAN check is liveness: sample
        the 9DOF stream (UDP 5002) briefly and see if it is still producing fresh samples. That
        catches the real failure modes -- MCU crashed, rebooted, or stopped publishing.
        """
        imu = self.hub.imu
        if imu is None:
            feedback_label.setText(f"{success_text} · sent (no IMU receiver available to check)")
            return
        baseline = imu.get_stats().get("packet_count") or 0
        feedback_label.setText(f"{success_text} · sent, confirming IMU stream is alive...")
        QTimer.singleShot(
            AXIS_PROBE_DELAY_MS,
            lambda: self._finish_axis_probe(feedback_label, success_text, imu, baseline),
        )

    def _finish_axis_probe(self, feedback_label, success_text, imu, baseline):
        """Report liveness only -- never claim the axis remap itself was verified."""
        stats = imu.get_stats()
        count = stats.get("packet_count") or 0
        if count > baseline:
            feedback_label.setText(f"{success_text} · sent · IMU stream alive")
        else:
            message = f"{success_text} · sent · NO IMU DATA — config may not have applied"
            feedback_label.setText(message)
            self.notify(message)

    # --- lifecycle ----------------------------------------------------------

    def on_activate(self):
        """One-shot loads, mirroring the page-load fetches in configuration.js."""
        self._fill_gains(logic.load_controller_gains())
        if self.hub.controller:
            self.hub.controller.set_controller_gains(self._read_gains())
        self._gain_badge.setText("READY")
        self._gain_badge.setStyleSheet(_STATUS_STYLES["good"])

        self._load_pid_rates()
        self._load_imu_axes()
        self._load_accel_axes()
        self._load_offset()
