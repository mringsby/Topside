"""Manipulator widget — ports `manipulator.js` (107 lines), shared by Pilot and Tooling.

`manipulator.js` is loaded by both `pilot.html` and `tooling.html` and drives whichever slider
element is present on that page. Here it becomes one `ManipulatorWidget` class that both
`screens/pilot.py` and `screens/tooling.py` instantiate — each screen gets its own widget
instance, but both feed it from the same shared `hub.poller("manipulator.state", ...)` (250 ms,
matching `manipulator.js`'s `setInterval(fetchManipulator, 250)`), via their own `self.watch(...)`
call routed into this widget's `refresh()`. The widget does not poll itself — polling lifecycle
stays owned by `ScreenBase`, per the shard contract.

`Controller.set_manipulator()` is a plain in-memory write (it clamps and, at most, nudges the
already-open bitmask socket) — it is not in PARITY.md §1's blocking-call table, so it is called
directly from the slider's slot, same as `configuration.js`-style screens call `set_pid_rates()`
directly.

Slider range mirrors both templates: `min="-50" max="50" step="1"`, which is also
`Controller.MANIP_MIN_DEG`/`MANIP_MAX_DEG`. Non-finite setpoints are rejected before reaching the
controller, mirroring `POST /api/manipulator`'s `math.isfinite` guard (invariant #5) — a QSlider
can only ever emit an in-range int, so this can only matter if a future caller feeds it something
else, but the guard is cheap and the invariant is explicit.
"""

import math

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QGroupBox, QHBoxLayout, QLabel, QSlider, QVBoxLayout

MIN_DEG = -50
MAX_DEG = 50

#: Throttle outgoing POSTs while dragging — mirrors manipulator.js's queueManipulatorPost/100ms.
POST_THROTTLE_MS = 100


class ManipulatorWidget(QGroupBox):
    """Manipulator target slider plus target/applied/pulse/source readout.

    Call `refresh(payload)` — the shape returned by `hub.manipulator_payload()` — from a screen's
    poller slot. Slider interaction writes straight to `hub.controller.set_manipulator()`.
    """

    def __init__(self, hub, parent=None):
        super().__init__("Manipulator", parent)
        self.hub = hub
        self._dragging = False

        self._target_value = QLabel("0")
        self._applied_value = QLabel("--")
        self._pulse_value = QLabel("--")
        self._source_value = QLabel("--")

        header = QHBoxLayout()
        header.addWidget(QLabel("Target:"))
        header.addWidget(self._target_value)
        header.addWidget(QLabel("deg"))
        header.addStretch(1)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(MIN_DEG, MAX_DEG)
        self._slider.setValue(0)
        self._slider.sliderPressed.connect(self._on_pressed)
        self._slider.valueChanged.connect(self._on_changed)
        self._slider.sliderReleased.connect(self._on_released)

        grid = QGridLayout()
        grid.addWidget(QLabel("Applied"), 0, 0)
        grid.addWidget(QLabel("Pulse"), 0, 1)
        grid.addWidget(QLabel("Source"), 0, 2)
        grid.addWidget(self._applied_value, 1, 0)
        grid.addWidget(self._pulse_value, 1, 1)
        grid.addWidget(self._source_value, 1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._slider)
        layout.addLayout(grid)

        # Send-throttle window, not a poller: a local write-rate limiter (see debug.py's
        # _send_timer for the same "plain QTimer for a local loop" pattern).
        self._post_timer = QTimer(self)
        self._post_timer.setSingleShot(True)
        self._post_timer.setInterval(POST_THROTTLE_MS)

        self._refresh_enabled()

    # --- inbound: poller -> widget --------------------------------------------

    def refresh(self, payload):
        """Feed with the result of `hub.manipulator_payload()`."""
        payload = payload or {}
        target = payload.get("target_deg", payload.get("setpoint_deg", 0.0)) or 0.0
        applied = payload.get("applied_deg")
        pulse = payload.get("pulse_us")
        source = payload.get("source")

        self._target_value.setText(f"{target:.0f}")
        self._applied_value.setText("--" if applied is None else f"{applied:.1f}")
        self._pulse_value.setText("--" if pulse is None else str(pulse))
        self._source_value.setText(source or "--")

        # Don't yank the slider out from under an active drag (manipulator.js's _manipActiveSlider).
        if not self._dragging:
            self._slider.blockSignals(True)
            self._slider.setValue(round(target))
            self._slider.blockSignals(False)

        self._refresh_enabled()

    def _refresh_enabled(self):
        self.setEnabled(self.hub.controller is not None)

    # --- outbound: slider -> controller ----------------------------------------

    def _on_pressed(self):
        self._dragging = True

    def _on_released(self):
        self._dragging = False
        self._send(self._slider.value())

    def _on_changed(self, value):
        self._target_value.setText(str(value))
        self._queue_send(value)

    def _queue_send(self, value):
        """Send immediately, then ignore further sends until the throttle window elapses."""
        if self._post_timer.isActive():
            return
        self._post_timer.start()
        self._send(value)

    def _send(self, value):
        ctrl = self.hub.controller
        if ctrl is None:
            return
        deg = float(value)
        if not math.isfinite(deg):
            return
        ctrl.set_manipulator(deg, source="gui")
