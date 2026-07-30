"""Tooling screen — ports `/tooling` (`tooling.html` + `lights.js`, 60 lines).

Two cards: main lights and the manipulator. The manipulator card is the shared
`ManipulatorWidget` (see `desktop/widgets/manipulator.py`) — `manipulator.js` is loaded by both
this screen and `screens/pilot.py`; it is not duplicated here. Both screens' manipulator widgets
are fed by the same named poller (`"manipulator.state"`, 250 ms), so opening both tabs does not
double the poll rate.

No `window.confirm(...)` sites in `lights.js` — grepped for `confirm(`, none found. A brightness
slider needs no destructive-action guard.

`lights.js` itself has no repeating poll — `updateLights()` runs once on `DOMContentLoaded` (the
file's leading comment claims `configuration.js` also drives it on an interval, but
`configuration.js` has no such call; grepped, the comment is stale). Reproduced literally: this
screen loads the current level once in `on_activate`, not via `self.watch(...)`. If the light
level changes elsewhere while this tab is open (e.g. from Pilot's HUD slider), this slider will not
follow it live — that mirrors `lights.js`'s actual behaviour, not a gap.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QSlider, QVBoxLayout

from desktop import logic
from desktop.screens.base import ScreenBase
from desktop.widgets.manipulator import ManipulatorWidget

MANIPULATOR_INTERVAL_MS = 250

#: Throttle outgoing POSTs while dragging — mirrors lights.js's queueLightPost/100ms.
POST_THROTTLE_MS = 100


class ToolingScreen(ScreenBase):
    title = "Tooling"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._dragging = False

        row = QHBoxLayout()
        row.addWidget(self._build_lights_box())
        self._manipulator = ManipulatorWidget(hub)
        row.addWidget(self._manipulator)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(row)
        layout.addStretch(1)

        self._post_timer = QTimer(self)
        self._post_timer.setSingleShot(True)
        self._post_timer.setInterval(POST_THROTTLE_MS)

        self.watch(
            "manipulator.state", self.hub.manipulator_payload, MANIPULATOR_INTERVAL_MS, self._manipulator.refresh
        )

    # --- Main Lights ---------------------------------------------------------

    def _build_lights_box(self):
        box = QGroupBox("Main Lights")
        self._value_label = QLabel("0%")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 80)
        self._slider.setValue(0)
        self._slider.sliderPressed.connect(self._on_pressed)
        self._slider.valueChanged.connect(self._on_changed)
        self._slider.sliderReleased.connect(self._on_released)

        header = QHBoxLayout()
        header.addWidget(QLabel("Brightness:"))
        header.addWidget(self._value_label)
        header.addStretch(1)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._slider)
        outer.addStretch(1)
        return box

    def _load_lights(self):
        ctrl = self.hub.controller
        pct = round(ctrl.get_light() * 100) if ctrl else 0
        self._value_label.setText(f"{pct}%")
        self._slider.blockSignals(True)
        self._slider.setValue(int(pct))
        self._slider.blockSignals(False)

    def _on_pressed(self):
        self._dragging = True

    def _on_released(self):
        self._dragging = False
        self._send(self._slider.value())

    def _on_changed(self, value):
        self._value_label.setText(f"{value}%")
        self._queue_send(value)

    def _queue_send(self, value):
        if self._post_timer.isActive():
            return
        self._post_timer.start()
        self._send(value)

    def _send(self, value):
        ctrl = self.hub.controller
        if ctrl is None:
            return
        pct = logic.clamp(float(value), 0.0, 100.0)
        ctrl.set_light(pct / 100.0)

    # --- lifecycle ----------------------------------------------------------

    def on_activate(self):
        """One-shot load, matching lights.js's single fetch on page load (see module docstring)."""
        self._load_lights()
