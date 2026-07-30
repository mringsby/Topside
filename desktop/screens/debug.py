"""Debug override screen — the reference implementation for the port.

Ports `/debug` (`debug.html` + `debug.js`). Shard agents: read this file before writing yours.
It is deliberately small and deliberately shows every pattern the other screens need:

  * a shared poller driving a read-only view          -> _on_status
  * a fast local send loop that is NOT a poller       -> _send_timer
  * a blocking call pushed off the GUI thread         -> _clear_override
  * graceful degradation when a service is absent     -> _refresh_enabled
  * non-blocking error reporting, never QMessageBox   -> notify()
  * a screen that must release state when hidden      -> on_deactivate

Behavioural parity with debug.js: sliders are integers -100..100 shown as +/-1.00, double-click
zeroes an axis, override sends at 20 Hz (SEND_INTERVAL_MS = 50) while active, status refreshes
every 500 ms, and every exit path (Disable, STOP ALL, leaving the screen) zeroes the sliders and
clears the override.
"""

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from desktop import logic
from desktop.screens.base import ScreenBase

SEND_INTERVAL_MS = 50
STATUS_INTERVAL_MS = 500

AXIS_LABELS = [
    ("surge", "Surge"),
    ("sway", "Sway"),
    ("heave", "Heave"),
    ("roll", "Roll"),
    ("pitch", "Pitch"),
    ("yaw", "Yaw"),
]


class AxisSlider(QGroupBox):
    """One labelled -1.00..+1.00 axis slider. Double-click to zero."""

    def __init__(self, axis, label, parent=None):
        super().__init__(parent)
        self.axis = axis

        self._value_label = QLabel("0.00")
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(-100, 100)
        self._slider.setValue(0)
        self._slider.valueChanged.connect(self._on_changed)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{label}</b>"))
        header.addStretch(1)
        header.addWidget(self._value_label)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._slider)
        self._on_changed(0)

    def value(self):
        return self._slider.value() / 100.0

    def reset(self):
        self._slider.setValue(0)

    def mouseDoubleClickEvent(self, event):
        self.reset()
        super().mouseDoubleClickEvent(event)

    def _on_changed(self, _raw):
        value = self.value()
        self._value_label.setText(f"{value:+.2f}" if abs(value) > 0.005 else "0.00")


class DebugScreen(ScreenBase):
    title = "Debug"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._override_active = False

        self._sliders = {}
        grid = QGridLayout()
        for index, (axis, label) in enumerate(AXIS_LABELS):
            widget = AxisSlider(axis, label)
            self._sliders[axis] = widget
            grid.addWidget(widget, index // 3, index % 3)

        self._status_badge = QLabel("INACTIVE")
        self._btn_enable = QPushButton("Enable Override")
        self._btn_disable = QPushButton("Disable Override")
        self._btn_zero = QPushButton("Zero Sliders")
        self._btn_stop = QPushButton("STOP ALL")
        self._btn_disable.setEnabled(False)

        self._btn_enable.clicked.connect(self._enable_override)
        self._btn_disable.clicked.connect(self._stop_all)
        self._btn_zero.clicked.connect(self._zero_sliders)
        self._btn_stop.clicked.connect(self._stop_all)

        controls = QHBoxLayout()
        controls.addWidget(self._btn_enable)
        controls.addWidget(self._btn_disable)
        controls.addWidget(self._btn_zero)
        controls.addStretch(1)
        controls.addWidget(self._status_badge)
        controls.addWidget(self._btn_stop)

        self._status_view = QPlainTextEdit()
        self._status_view.setReadOnly(True)
        self._status_view.setPlainText("Loading...")

        override_box = QGroupBox("Debug Override")
        override_layout = QVBoxLayout(override_box)
        override_layout.addLayout(controls)
        override_layout.addLayout(grid)

        sends_box = QGroupBox("Topside Sends")
        sends_layout = QVBoxLayout(sends_box)
        sends_layout.addWidget(self._status_view)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addWidget(override_box)
        layout.addWidget(sends_box, 1)

        # A plain QTimer, not a hub poller: this is a local write loop, not shared read state.
        self._send_timer = QTimer(self)
        self._send_timer.setInterval(SEND_INTERVAL_MS)
        self._send_timer.timeout.connect(self._send_override)

        self.watch("rov.status", self._rov_status, STATUS_INTERVAL_MS, self._on_status)
        self._refresh_enabled()

    # --- data ---------------------------------------------------------------

    def _rov_status(self):
        """Replaces GET /api/rov/status. Cheap in-memory reads only."""
        hub = self.hub
        udp_rx, udp_err = hub.resource.get_udp_counters() if hub.resource else (0, 0)
        return {
            "command": hub.bitmask.get_command() if hub.bitmask else {},
            "uplink": hub.bitmask.get_uplink_status() if hub.bitmask else {},
            "resource": {"udp_rx_count": udp_rx, "udp_rx_errors": udp_err},
        }

    def _on_status(self, status):
        self._status_view.setPlainText(json.dumps(status, indent=2))

    # --- override -----------------------------------------------------------

    def _enable_override(self):
        ctrl = self.hub.controller
        if ctrl is None:
            return
        # Matches the route's 423 path: a killed controller refuses overrides outright.
        if ctrl.is_killed():
            self.hub.neutralize_thruster_command()
            self.notify("Controls are killed — rearm before overriding.")
            return
        self._set_override_active(True)
        self._send_override()
        self._send_timer.start()

    def _send_override(self):
        """20 Hz. set_debug_override is an in-memory write, so the GUI thread is fine here."""
        ctrl = self.hub.controller
        if not self._override_active or ctrl is None:
            return
        if ctrl.is_killed():
            self._stop_all()
            return
        values = {axis: widget.value() for axis, widget in self._sliders.items()}
        ctrl.set_debug_override(logic.debug_override_axes(values))

    def _stop_all(self):
        self._set_override_active(False)
        self._zero_sliders()
        self._clear_override()

    def _clear_override(self):
        """Clearing may replay PID setpoints over UDP, so it goes off the GUI thread."""
        hub = self.hub
        ctrl = hub.controller
        if ctrl is not None:
            ctrl.clear_debug_override()

        client = hub.setpoint_override
        if client is None:
            return

        def work():
            if ctrl is not None and ctrl.is_pid_enabled():
                return hub.send_active_pid_setpoints()
            return client.clear_override()

        hub.call_async(work, on_error=self._on_error)

    def _zero_sliders(self):
        for widget in self._sliders.values():
            widget.reset()

    def _set_override_active(self, active):
        self._override_active = active
        if not active:
            self._send_timer.stop()
        self._status_badge.setText("ACTIVE" if active else "INACTIVE")
        self._refresh_enabled()

    def _refresh_enabled(self):
        available = self.hub.controller is not None
        self.setEnabled(True)
        self._btn_enable.setEnabled(available and not self._override_active)
        self._btn_disable.setEnabled(available and self._override_active)
        self._btn_stop.setEnabled(available)
        for widget in self._sliders.values():
            widget.setEnabled(available)

    def _on_error(self, message):
        self.notify(f"Debug override: {message}")

    # --- lifecycle ----------------------------------------------------------

    def on_deactivate(self):
        """Never leave an override running behind the operator's back."""
        if self._override_active:
            self._stop_all()
