"""Sparkline widget — a compact rolling-value trend line, no plotting library required.

`screens/graphs.py::_ChartPanel` is a full pyqtgraph time-series chart (zoom, crosshair,
setpoint overlay, CSV export) built for the six-channel IMU graphs screen; it is the wrong tool
for a one-line "is this counter climbing" glance in a status strip. This widget is deliberately
minimal — a fixed-size ring buffer of floats rendered as a polyline with `QPainter` — so any
screen can drop in a trend indicator for a scalar without pulling in pyqtgraph.

Feed it with `add_value(v)` on every poll tick; it keeps the last `max_points` samples and
autoscales to their min/max. `set_alert(bool)` swaps the line color so a rising counter reads as
a problem at a glance instead of just another number (the caller decides what "alert" means for
its own data).
"""

from collections import deque

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QWidget

from desktop import theme


class SparklineWidget(QWidget):
    """Minimal rolling-value trend line. No axes, no labels -- just the shape of the trend."""

    def __init__(self, max_points=60, parent=None):
        super().__init__(parent)
        self._max_points = max_points
        self._values = deque(maxlen=max_points)
        self._alert = False
        self.setMinimumSize(60, 20)
        theme.signals.changed.connect(self.update)

    def add_value(self, value):
        """Append the latest sample and repaint."""
        self._values.append(float(value))
        self.update()

    def clear(self):
        """Drop all samples (e.g. when the source goes unavailable)."""
        self._values.clear()
        self.update()

    def set_alert(self, alert: bool):
        """Switch the line color to flag a rising/problem trend."""
        if alert != self._alert:
            self._alert = alert
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            if len(self._values) < 2:
                return
            rect = self.rect()
            lo = min(self._values)
            hi = max(self._values)
            span = (hi - lo) or 1.0
            n = len(self._values)
            step = rect.width() / max(1, n - 1)

            color = theme.token("danger") if self._alert else theme.token("success")
            pen = QPen(color)
            pen.setWidthF(1.6)
            painter.setPen(pen)

            points = []
            for i, v in enumerate(self._values):
                x = i * step
                y = rect.height() - ((v - lo) / span) * rect.height()
                points.append(QPointF(x, y))
            painter.drawPolyline(points)
        finally:
            painter.end()
