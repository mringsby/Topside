"""Tooling screen — ports `/tooling` (`tooling.html` + `lights.js`, 60 lines).

Two panels: `ToolingLightsPanel` (main lights) and `screens/pilot.py`'s `ManipulatorPanel`, reused
here rather than rebuilt — `manipulator.js` is loaded by both `pilot.html` and `tooling.html`, and
`ManipulatorPanel` already wraps the shared `ManipulatorWidget` (see
`desktop/widgets/manipulator.py`) plus its own `self.watch("manipulator.state", ...)`. Both
screens' manipulator panels feed from the same named poller (250 ms), so opening both tabs does
not double the poll rate.

No `window.confirm(...)` sites in `lights.js` — grepped for `confirm(`, none found. A brightness
slider needs no destructive-action guard.

`lights.js` itself has no repeating poll — `updateLights()` runs once on `DOMContentLoaded` (the
file's leading comment claims `configuration.js` also drives it on an interval, but
`configuration.js` has no such call; grepped, the comment is stale). Reproduced literally:
`ToolingLightsPanel` loads the current level once in `on_activate`, not via `self.watch(...)`. If
the light level changes elsewhere while this tab is open (e.g. from Pilot's HUD slider), this
slider will not follow it live — that mirrors `lights.js`'s actual behaviour, not a gap.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QSlider, QVBoxLayout

from desktop import logic
from desktop.component import Component
from desktop.screens.base import PanelBase, ScreenBase
from desktop.screens.pilot import ManipulatorPanel

#: Throttle outgoing POSTs while dragging — mirrors lights.js's queueLightPost/100ms.
POST_THROTTLE_MS = 100


class ToolingLightsPanel(PanelBase):
    """Main lights brightness slider. WRITES to the vehicle via `hub.controller.set_light()`;
    keep single-instance."""

    title = "Main Lights"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._dragging = False

        box = QGroupBox("Main Lights")
        self._value = QLabel("0%")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 80)
        self._slider.setValue(0)
        self._slider.sliderPressed.connect(self._on_pressed)
        self._slider.valueChanged.connect(self._on_changed)
        self._slider.sliderReleased.connect(self._on_released)

        header = QHBoxLayout()
        header.addWidget(QLabel("Brightness:"))
        header.addWidget(self._value)
        header.addStretch(1)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._slider)
        outer.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)

        self._post_timer = QTimer(self)
        self._post_timer.setSingleShot(True)
        self._post_timer.setInterval(POST_THROTTLE_MS)

    def _load_lights(self):
        ctrl = self.hub.controller
        pct = round(ctrl.get_light() * 100) if ctrl else 0
        self._value.setText(f"{pct}%")
        self._slider.blockSignals(True)
        self._slider.setValue(int(pct))
        self._slider.blockSignals(False)

    def _on_pressed(self):
        self._dragging = True

    def _on_released(self):
        self._dragging = False
        self._send(self._slider.value())

    def _on_changed(self, value):
        self._value.setText(f"{value}%")
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


class ToolingScreen(ScreenBase):
    title = "Tooling"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._lights_panel = ToolingLightsPanel(hub)
        self._manipulator_panel = ManipulatorPanel(hub)

        row = QHBoxLayout()
        row.addWidget(self._lights_panel)
        row.addWidget(self._manipulator_panel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(row)
        layout.addStretch(1)

        # Back-compat attribute aliases. No watch()/on_activate of its own: each panel owns its
        # own lifecycle, and PanelBase.set_active cascades activation down to them.
        self._value_label = self._lights_panel._value
        self._slider = self._lights_panel._slider
        self._manipulator = self._manipulator_panel._manipulator


#: Category "Tooling" — lights only. `ManipulatorPanel` is reused here as a widget (see
#: `ToolingScreen` above) but deliberately NOT re-exported under a second component id: the
#: shell's single-instance check in `shell.py::open_component` is keyed by `component.id`, so
#: registering the same writer class under both "panel.pilot.manipulator" and a Tooling id would
#: let an operator open two independent live manipulator panels at once — exactly the
#: two-writers-racing hazard `duplicable=False` exists to prevent. It stays reachable as a
#: standalone dock via Pilot's `panel.pilot.manipulator` entry.
COMPONENTS = [
    Component(
        id="panel.tooling.lights",
        title="Main Lights",
        factory=ToolingLightsPanel,
        category="Tooling",
        duplicable=False,
        order=0,
    ),
]
