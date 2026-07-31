"""Logs screen — surfaces the Zephyr firmware log stream (UDP 5006).

`hub.log_stream` (`lib/log_udp_receiver.py::LogStreamReceiver`) already runs every session,
buffering the last `max_entries` lines in memory and writing every line to disk. Nothing in the
GUI read it before this screen; this is the missing frontend only.

`get_recent(limit)` is a cheap in-memory read of a ring buffer (no MCU/network round trip), so
it is polled directly through `self.watch(...)` — no `call_async` needed.

Entries carry no id or sequence number, so incremental rendering tracks the last `(ts, message)`
pair it drew and looks for that pair in each fresh poll to find only what is new. If the ring
buffer rolled over faster than the poll interval and the marker can't be found, the whole visible
list is redrawn from the retained local cache instead of duplicating or losing rows.

Clear wipes the QTextEdit and the local render cache, but never `LogStreamReceiver`'s buffer or
the on-disk log — every line is still recorded, and new lines keep arriving normally.
"""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from desktop.screens.base import ScreenBase

POLL_INTERVAL_MS = 400
#: Matches LogStreamReceiver's default max_entries — no point asking for more than it can hold.
FETCH_LIMIT = 500
#: Local render cache retained across polls so a filter toggle can re-render without re-polling.
CACHE_LIMIT = 1000

_LEVELS = (
    ("D", "Debug"),
    ("I", "Info"),
    ("W", "Warn"),
    ("E", "Error"),
)

_LEVEL_COLOR = {
    "E": "#f85149",
    "W": "#d29922",
    "I": "palette(text)",
    "D": "#8b949e",
}


class LogsScreen(ScreenBase):
    title = "Logs"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        #: Local cache of entries rendered so far, used to re-render on a filter change.
        self._known: list[dict] = []
        #: (ts, message) of the last entry drawn from the receiver — the incremental marker.
        self._last_seen = None

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(self._build_toolbar())

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet("font-family: monospace;")
        layout.addWidget(self._view, 1)

        if self.hub.log_stream is None:
            self._set_enabled(False)
            self.notify("Log stream unavailable — no receiver.")
        else:
            self.watch("logs.recent", self._read_recent, POLL_INTERVAL_MS, self._on_recent)

    # --- UI construction ---------------------------------------------------

    def _build_toolbar(self):
        row = QHBoxLayout()
        row.addWidget(QLabel("Level:"))

        self._level_checks = {}
        for code, label in _LEVELS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.toggled.connect(self._on_filter_changed)
            self._level_checks[code] = box
            row.addWidget(box)

        row.addStretch(1)

        self._autoscroll_check = QCheckBox("Autoscroll")
        self._autoscroll_check.setChecked(True)
        row.addWidget(self._autoscroll_check)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.clicked.connect(self._on_clear)
        row.addWidget(self._btn_clear)

        self._toolbar_widgets = [*self._level_checks.values(), self._autoscroll_check, self._btn_clear]
        return row

    def _set_enabled(self, enabled):
        for widget in self._toolbar_widgets:
            widget.setEnabled(enabled)
        self._view.setEnabled(enabled)

    # --- polling -------------------------------------------------------------

    def _read_recent(self):
        return self.hub.log_stream.get_recent(FETCH_LIMIT)

    def _on_recent(self, entries):
        if not entries:
            return

        if self._last_seen is None:
            self._known = list(entries[-CACHE_LIMIT:])
            self._last_seen = self._entry_key(entries[-1])
            self._append_entries(entries)
            return

        idx = self._find_marker(entries, self._last_seen)
        if idx is None:
            # Buffer rolled over faster than the poll interval -- the marker is gone.
            # Re-render the whole visible list from what's fresh instead of guessing.
            self._known = list(entries[-CACHE_LIMIT:])
            self._last_seen = self._entry_key(entries[-1])
            self._render_all()
            return

        new_entries = entries[idx + 1 :]
        if new_entries:
            self._known.extend(new_entries)
            if len(self._known) > CACHE_LIMIT:
                self._known = self._known[-CACHE_LIMIT:]
            self._append_entries(new_entries)
            self._last_seen = self._entry_key(entries[-1])

    @staticmethod
    def _entry_key(entry):
        return (entry.get("ts"), entry.get("message"))

    @classmethod
    def _find_marker(cls, entries, marker):
        for idx in range(len(entries) - 1, -1, -1):
            if cls._entry_key(entries[idx]) == marker:
                return idx
        return None

    # --- rendering -------------------------------------------------------------

    def _visible(self, entry):
        code = entry.get("level", "I")
        check = self._level_checks.get(code, self._level_checks["I"])
        return check.isChecked()

    def _render_all(self):
        self._view.clear()
        self._append_entries(self._known)

    def _append_entries(self, entries):
        to_draw = [entry for entry in entries if self._visible(entry)]
        if not to_draw:
            return
        scrollbar = self._view.verticalScrollBar()
        prev_value = scrollbar.value()
        cursor = QTextCursor(self._view.document())
        cursor.movePosition(QTextCursor.End)
        for entry in to_draw:
            cursor.insertHtml(self._format_entry(entry))
            cursor.insertBlock()
        if self._autoscroll_check.isChecked():
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(prev_value)

    @staticmethod
    def _format_entry(entry):
        ts = entry.get("ts")
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3] if ts else "--:--:--.---"
        level = entry.get("level", "I")
        color = _LEVEL_COLOR.get(level, _LEVEL_COLOR["I"])
        weight = "font-weight:600;" if level == "E" else ""
        message = html.escape(str(entry.get("message", "")))
        return f'<span style="color:{color};{weight}">[{stamp}] [{level}] {message}</span>'

    # --- toolbar actions ---------------------------------------------------

    def _on_filter_changed(self, _checked):
        self._render_all()

    def _on_clear(self):
        """Clear the view and the local render cache.

        Never touches the receiver's buffer or the on-disk log — both keep every line. Dropping
        `_known` is what makes Clear stick: a later filter toggle re-renders from that cache, so
        leaving it populated would repaint the rows the user just cleared. `_last_seen` is kept so
        the next poll still appends only genuinely new entries.
        """
        self._view.clear()
        self._known = []
