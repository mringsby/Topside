"""IMU graphs screen — the pyqtgraph rebuild of `/graphs` (`graphs.js`, 400 lines + `graphs.html`).

Ports six rolling time-series charts (yaw, pitch, roll, and their rates) with a shared 100 ms
poll, per-chart Y-axis locking, a crosshair readout, zoom/pan and CSV export. Chart.js +
chartjs-plugin-zoom + hammer.js is replaced by `pyqtgraph.PlotWidget`; the interaction contract
(scroll = zoom time axis, shift+scroll = zoom Y axis, drag = pan, double-click = reset zoom) is
reproduced with a small `ViewBox` subclass rather than by any plugin.

Data sources (see PARITY.md §3):
  * `GET /api/sensors` -> `hub.imu.get_stats()["last_data"]` (yaw/pitch/roll/yr/pr/rr), read
    directly off the receiver instead of the `data.json` round-trip the route used.
  * `GET /api/control/telemetry` -> `hub.control_telem.get_latest()["setpoint"]`, overlaid as a
    dashed line on the yaw/pitch/roll charts only (rate charts have no PID setpoint).

Both are cheap in-memory getters, so they are read straight from the shared `self.watch(...)`
poller like every other screen — no `hub.call_async` is needed here, there is no blocking call
on this screen.

Ring buffers are plain `deque`s trimmed to the selected time window every tick, mirroring
`trimData()` in graphs.js. `PlotDataItem.setData` is reused every tick (never recreated), which
is what keeps six live charts at 10 Hz cheap to redraw.
"""

import time
from bisect import bisect_left
from collections import deque
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from desktop.component import Component
from desktop.screens.base import PanelBase, ScreenBase

pg.setConfigOptions(antialias=True)
pg.setConfigOption("background", "#14151d")
pg.setConfigOption("foreground", "#adb5bd")

POLL_MS = 100  # matches graphs.js POLL_MS

#: (key, label, unit, color, has_setpoint) — same six channels/colors as chartDefs in graphs.js.
CHART_DEFS = [
    ("yaw", "Yaw", "deg", "#0dcaf0", True),
    ("yr", "Yaw Rate", "deg/s", "#0dcaf0", False),
    ("pitch", "Pitch", "deg", "#ffc107", True),
    ("pr", "Pitch Rate", "deg/s", "#ffc107", False),
    ("roll", "Roll", "deg", "#198754", True),
    ("rr", "Roll Rate", "deg/s", "#198754", False),
]
SETPOINT_COLOR = "#ff4d6d"

#: (seconds, label) — matches the #time-window <select> options in graphs.html.
WINDOW_OPTIONS = [(10, "10 s"), (30, "30 s"), (60, "60 s"), (120, "2 min"), (300, "5 min")]
DEFAULT_WINDOW_SEC = 30


class _ZoomViewBox(pg.ViewBox):
    """Wheel = zoom time axis, Shift+wheel = zoom Y axis, double-click = reset zoom.

    Reproduces the chartjs-plugin-zoom config in graphs.js (`mode: function(ctx) {...}` picking
    x vs y based on `evt.shiftKey`), which is not pyqtgraph's default wheel behaviour (which
    scales both axes together).
    """

    def wheelEvent(self, ev, axis=None):
        shift = bool(ev.modifiers() & Qt.ShiftModifier)
        super().wheelEvent(ev, axis=1 if shift else 0)

    def mouseClickEvent(self, ev):
        if ev.double():
            ev.accept()
            self.enableAutoRange(x=True, y=True)
            self.autoRange()
            return
        super().mouseClickEvent(ev)


class _GraphPlotWidget(pg.PlotWidget):
    """PlotWidget that reports when the mouse leaves it, to hide the crosshair readout."""

    def __init__(self, view_box, on_leave, parent=None):
        super().__init__(parent=parent, viewBox=view_box)
        self._on_leave = on_leave

    def leaveEvent(self, event):
        self._on_leave()
        super().leaveEvent(event)


def sample_imu(hub):
    """Replaces GET /api/sensors + GET /api/control/telemetry. Cheap in-memory reads only.

    Module-level so every chart panel can feed itself from the shared "graphs.sample" poller —
    six panels watching one key share a single timer, which is what makes a chart free to
    duplicate into as many docks as the operator wants.
    """
    last_data = {}
    if hub.imu is not None:
        last_data = hub.imu.get_stats().get("last_data") or {}
    setpoints = {}
    if hub.control_telem is not None:
        setpoints = hub.control_telem.get_latest().get("setpoint") or {}
    return {"imu": last_data, "setpoint": setpoints}


class ChartPanel(PanelBase):
    """One rolling time-series chart: main curve, optional setpoint overlay, zero line,
    crosshair readout and a Y-axis min/max lock. Equivalent to one `.graph-card` + its
    `makeChart()` call in graphs.js.

    Self-sufficient: it polls for its own data, so it works both inside `GraphsScreen`'s grid and
    alone in its own dock. `standalone` adds the per-chart control row that the grid supplies
    once for all six.
    """

    def __init__(
        self,
        hub,
        key,
        label,
        unit,
        color,
        has_setpoint,
        window_sec=DEFAULT_WINDOW_SEC,
        standalone=False,
        parent=None,
    ):
        super().__init__(hub, parent)
        self.title = label
        self.key = key
        self.label = label
        self.unit = unit
        self.has_setpoint = has_setpoint
        self.window_sec = window_sec
        self._paused = False
        self._start = time.monotonic()

        self._xs = deque()
        self._ys = deque()
        self._sp_xs = deque()
        self._sp_ys = deque()
        self._lock_min = None
        self._lock_max = None

        self.plot = _GraphPlotWidget(_ZoomViewBox(), self._hide_crosshair)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", unit)
        # Zero line: pyqtgraph clips it to the current view, so it silently disappears when
        # 0 is out of range — the same behaviour as zeroLinePlugin's `if (yScale.min > 0 ...)`.
        self.plot.addLine(y=0, pen=pg.mkPen(color=(255, 255, 255, 60), style=Qt.DashLine))

        self.curve = self.plot.plot([], [], pen=pg.mkPen(color=color, width=1.5), name=label)
        self.setpoint_curve = None
        if has_setpoint:
            self.plot.addLegend(offset=(-10, 10))
            sp_pen = pg.mkPen(color=SETPOINT_COLOR, width=2, style=Qt.DashLine)
            self.setpoint_curve = self.plot.plot([], [], pen=sp_pen, name=f"{label} Setpoint")

        line_pen = pg.mkPen(color=(255, 255, 255, 100), style=Qt.DashLine)
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=line_pen)
        self._hline = pg.InfiniteLine(angle=0, movable=False, pen=line_pen)
        self._vline.setVisible(False)
        self._hline.setVisible(False)
        self.plot.addItem(self._vline, ignoreBounds=True)
        self.plot.addItem(self._hline, ignoreBounds=True)
        # Keep the proxy alive on the instance — SignalProxy is not retained anywhere else.
        self._mouse_proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._on_mouse_moved)

        self._readout = QLabel(" ")
        self._readout.setStyleSheet("color: #adb5bd; font-size: 11px;")

        self._min_edit = QLineEdit()
        self._min_edit.setPlaceholderText("min")
        self._min_edit.setValidator(QDoubleValidator())
        self._min_edit.setFixedWidth(60)
        self._max_edit = QLineEdit()
        self._max_edit.setPlaceholderText("max")
        self._max_edit.setValidator(QDoubleValidator())
        self._max_edit.setFixedWidth(60)
        self._auto_btn = QPushButton("Auto")
        self._auto_btn.setFixedWidth(50)
        self._min_edit.editingFinished.connect(self._apply_lock)
        self._max_edit.editingFinished.connect(self._apply_lock)
        self._auto_btn.clicked.connect(self._clear_lock)

        header = QHBoxLayout()
        header.addWidget(self._min_edit)
        header.addWidget(QLabel("to"))
        header.addWidget(self._max_edit)
        header.addWidget(QLabel(unit))
        header.addWidget(self._auto_btn)
        header.addStretch(1)

        box = QGroupBox(label)
        inner = QVBoxLayout(box)
        inner.addLayout(header)
        if standalone:
            inner.addLayout(self._build_standalone_controls())
        inner.addWidget(self.plot, 1)
        inner.addWidget(self._readout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)

        self.watch("graphs.sample", lambda: sample_imu(self.hub), POLL_MS, self._on_sample)

    def _build_standalone_controls(self):
        """The subset of the grid's toolbar that makes sense for a single chart in its own dock."""
        combo = QComboBox()
        for value, text in WINDOW_OPTIONS:
            combo.addItem(text, value)
        combo.setCurrentIndex([v for v, _ in WINDOW_OPTIONS].index(self.window_sec))
        combo.currentIndexChanged.connect(lambda index: self.set_window(combo.itemData(index)))

        pause = QPushButton("Pause")
        pause.clicked.connect(
            lambda: (self.set_paused(not self._paused), pause.setText("Resume" if self._paused else "Pause"))
        )
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        reset = QPushButton("Reset Zoom")
        reset.clicked.connect(self.reset_zoom)

        row = QHBoxLayout()
        row.addWidget(QLabel("Window:"))
        row.addWidget(combo)
        row.addWidget(pause)
        row.addWidget(clear)
        row.addWidget(reset)
        row.addStretch(1)
        return row

    def set_paused(self, paused):
        self._paused = bool(paused)

    def _on_sample(self, sample):
        """One tick of the shared poller, narrowed to this chart's own channel."""
        if self._paused:
            return
        value = sample["imu"].get(self.key, 0)
        if not isinstance(value, (int, float)) or value != value:  # NaN check
            value = 0.0
        setpoint = None
        if self.has_setpoint:
            raw = sample["setpoint"].get(self.key)
            setpoint = float(raw) if isinstance(raw, (int, float)) else None
        self.add_sample(time.monotonic() - self._start, float(value), setpoint)

    # --- data ---------------------------------------------------------------

    def add_sample(self, t, y, setpoint=None):
        self._xs.append(t)
        self._ys.append(y)
        if self.setpoint_curve is not None:
            self._sp_xs.append(t)
            self._sp_ys.append(setpoint)
        self._trim(t)
        self.curve.setData(list(self._xs), list(self._ys))
        if self.setpoint_curve is not None:
            # NaN (not None) so pyqtgraph's connect="finite" breaks the line at missing points,
            # matching Chart.js's default spanGaps=false.
            sp_values = [v if v is not None else float("nan") for v in self._sp_ys]
            self.setpoint_curve.setData(list(self._sp_xs), sp_values, connect="finite")
        self._refresh_view()

    def _trim(self, now):
        cutoff = now - self.window_sec
        while self._xs and self._xs[0] < cutoff:
            self._xs.popleft()
            self._ys.popleft()
        while self._sp_xs and self._sp_xs[0] < cutoff:
            self._sp_xs.popleft()
            self._sp_ys.popleft()

    def clear(self):
        self._xs.clear()
        self._ys.clear()
        self._sp_xs.clear()
        self._sp_ys.clear()
        self.curve.setData([], [])
        if self.setpoint_curve is not None:
            self.setpoint_curve.setData([], [])
        self._hide_crosshair()
        self.reset_zoom()

    def reset_zoom(self):
        vb = self.plot.getViewBox()
        vb.enableAutoRange(x=True, y=True)
        vb.autoRange()

    def set_window(self, window_sec):
        self.window_sec = window_sec

    def samples(self):
        """Plain lists for CSV export: (times, values, setpoint-values-or-None)."""
        return list(self._xs), list(self._ys), list(self._sp_ys)

    # --- Y-axis lock ----------------------------------------------------------
    # Recomputed every tick from the currently-windowed data, same as graphs.js's
    # applyYLocks(): an unlocked bound tracks the data, a locked bound is pinned. That means an
    # unlocked axis silently overrides a manual drag-pan within one tick — a quirk inherited
    # from the source page, not introduced here.

    def _refresh_view(self):
        values = list(self._ys)
        if self.setpoint_curve is not None:
            values += [v for v in self._sp_ys if v is not None]
        if values:
            data_min, data_max = min(values), max(values)
        else:
            data_min, data_max = -1.0, 1.0
        if data_min == data_max:
            data_min -= 1.0
            data_max += 1.0
        pad = (data_max - data_min) * 0.08
        lo = self._lock_min if self._lock_min is not None else data_min - pad
        hi = self._lock_max if self._lock_max is not None else data_max + pad
        if lo >= hi:
            hi = lo + 1e-3
        self.plot.getViewBox().setYRange(lo, hi, padding=0)

    def _apply_lock(self):
        min_text = self._min_edit.text().strip()
        max_text = self._max_edit.text().strip()
        self._lock_min = float(min_text) if min_text else None
        self._lock_max = float(max_text) if max_text else None
        self._refresh_view()

    def _clear_lock(self):
        self._min_edit.clear()
        self._max_edit.clear()
        self._lock_min = None
        self._lock_max = None
        self.reset_zoom()

    # --- crosshair --------------------------------------------------------

    def _on_mouse_moved(self, evt):
        scene_pos = evt[0]
        vb = self.plot.getViewBox()
        if not self._xs or not vb.sceneBoundingRect().contains(scene_pos):
            self._hide_crosshair()
            return
        x = vb.mapSceneToView(scene_pos).x()
        xs = list(self._xs)
        idx = bisect_left(xs, x)
        if idx <= 0:
            idx = 0
        elif idx >= len(xs):
            idx = len(xs) - 1
        elif abs(xs[idx - 1] - x) <= abs(xs[idx] - x):
            idx -= 1
        t = xs[idx]
        y = list(self._ys)[idx]
        self._vline.setPos(t)
        self._hline.setPos(y)
        self._vline.setVisible(True)
        self._hline.setVisible(True)
        parts = [f"t = {t:.1f} s", f"{self.label}: {y:.2f} {self.unit}"]
        if self.setpoint_curve is not None:
            sp_ys = list(self._sp_ys)
            if idx < len(sp_ys) and sp_ys[idx] is not None:
                parts.append(f"Setpoint: {sp_ys[idx]:.2f} {self.unit}")
        self._readout.setText("    ".join(parts))

    def _hide_crosshair(self):
        self._vline.setVisible(False)
        self._hline.setVisible(False)
        self._readout.setText(" ")


class GraphsScreen(ScreenBase):
    title = "Graphs"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._start = time.monotonic()
        self._paused = False
        self._window_sec = DEFAULT_WINDOW_SEC

        self._panels = {}
        grid = QGridLayout()
        grid.setSpacing(8)
        for index, (key, label, unit, color, has_setpoint) in enumerate(CHART_DEFS):
            panel = ChartPanel(hub, key, label, unit, color, has_setpoint, self._window_sec)
            self._panels[key] = panel
            grid.addWidget(panel, index // 2, index % 2)

        self._window_combo = QComboBox()
        for value, text in WINDOW_OPTIONS:
            self._window_combo.addItem(text, value)
        self._window_combo.setCurrentIndex([v for v, _ in WINDOW_OPTIONS].index(DEFAULT_WINDOW_SEC))
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)

        self._btn_pause = QPushButton("Pause")
        self._btn_pause.clicked.connect(self._toggle_pause)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.clicked.connect(self._clear_all)
        self._btn_reset_zoom = QPushButton("Reset Zoom (all)")
        self._btn_reset_zoom.clicked.connect(self._reset_all_zoom)
        self._btn_export = QPushButton("Export CSV")
        self._btn_export.clicked.connect(self._export_csv)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Window:"))
        controls.addWidget(self._window_combo)
        controls.addWidget(self._btn_pause)
        controls.addWidget(self._btn_clear)
        controls.addWidget(self._btn_reset_zoom)
        controls.addWidget(self._btn_export)
        controls.addStretch(1)

        help_label = QLabel(
            "Scroll to zoom time axis. Shift+scroll to zoom Y-axis. Drag to pan. Double-click a "
            "chart to reset its zoom. Set min/max to lock Y-axis. Hover for readout. Yaw, pitch, "
            "and roll include the active PID setpoint."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #888; font-size: 11px;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(controls)
        layout.addWidget(help_label)
        layout.addLayout(grid, 1)

        # No watch() of its own: each ChartPanel polls itself, and PanelBase.set_active cascades
        # activation down to them. Six panels on one poller key still share a single timer.

    # --- controls -------------------------------------------------------------

    def _on_window_changed(self, index):
        value = self._window_combo.itemData(index)
        self._window_sec = value
        for panel in self._panels.values():
            panel.set_window(value)

    def _toggle_pause(self):
        self._paused = not self._paused
        for panel in self._panels.values():
            panel.set_paused(self._paused)
        self._btn_pause.setText("Resume" if self._paused else "Pause")

    def _clear_all(self):
        for panel in self._panels.values():
            panel.clear()

    def _reset_all_zoom(self):
        for panel in self._panels.values():
            panel.reset_zoom()

    def _export_csv(self):
        """Replaces exportCSV() in graphs.js: one CSV, all channels aligned by time.

        Every panel is fed from the same `_on_sample` tick, so their sample counts and
        timestamps stay in lockstep — the yaw panel's timeline can be used as the master index.
        """
        yaw_xs, yaw_ys, yaw_sp = self._panels["yaw"].samples()
        if not yaw_xs:
            self.notify("No data to export.")
            return
        _, pitch_ys, pitch_sp = self._panels["pitch"].samples()
        _, roll_ys, roll_sp = self._panels["roll"].samples()
        _, yr_ys, _ = self._panels["yr"].samples()
        _, pr_ys, _ = self._panels["pr"].samples()
        _, rr_ys, _ = self._panels["rr"].samples()

        default_name = f"imu_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export IMU Data", default_name, "CSV files (*.csv)")
        if not path:
            return

        header = (
            "time_s,yaw_deg,yaw_setpoint_deg,pitch_deg,pitch_setpoint_deg,"
            "roll_deg,roll_setpoint_deg,yaw_rate_dps,pitch_rate_dps,roll_rate_dps"
        )
        lines = [header]
        for i, t in enumerate(yaw_xs):
            row = [
                yaw_ys[i],
                yaw_sp[i] if i < len(yaw_sp) else None,
                pitch_ys[i] if i < len(pitch_ys) else None,
                pitch_sp[i] if i < len(pitch_sp) else None,
                roll_ys[i] if i < len(roll_ys) else None,
                roll_sp[i] if i < len(roll_sp) else None,
                yr_ys[i] if i < len(yr_ys) else None,
                pr_ys[i] if i < len(pr_ys) else None,
                rr_ys[i] if i < len(rr_ys) else None,
            ]
            cells = [f"{t:.2f}"] + [f"{v:.3f}" if v is not None else "" for v in row]
            lines.append(",".join(cells))

        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            self.notify(f"Export failed: {exc}")
            return
        self.notify(f"Exported {len(yaw_xs)} samples to {path}")


def _chart_factory(key, label, unit, color, has_setpoint):
    """Component factories take only `hub`, so each chart's fixed config is closed over here."""

    def build(hub):
        return ChartPanel(hub, key, label, unit, color, has_setpoint, standalone=True)

    return build


#: Every chart is independently placeable AND duplicable — nothing here writes to the vehicle,
#: so two copies of the yaw chart in two windows is harmless. `GraphsScreen` stays registered as
#: the all-six grid, for the same reason: a read-only screen has no single-instance hazard.
COMPONENTS = [
    Component(
        id=f"panel.graphs.{key}",
        title=f"{label} chart",
        factory=_chart_factory(key, label, unit, color, has_setpoint),
        category="Graphs",
        duplicable=True,
        order=index,
    )
    for index, (key, label, unit, color, has_setpoint) in enumerate(CHART_DEFS)
]
