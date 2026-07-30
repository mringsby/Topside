"""ScreenBase — the pattern every ported screen follows.

A screen declares what it watches with `self.watch(...)`; the base class subscribes when the
screen becomes visible and unsubscribes when it does not. That reproduces the Flask behaviour
where a page that was not open issued no polls, and it is why a 60 Hz slider page does not tax
the machine while the operator is on the graphs tab.
"""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLabel, QWidget


class ScreenBase(QWidget):
    """Base for every ported screen.

    Never use QMessageBox for status or errors. A modal dialog blocks the Qt event loop, which
    stalls every poller and the 20 Hz command loop behind it, and makes the screen impossible to
    test headlessly. Use `self.notify(...)` — it is non-blocking and assertable.
    """

    #: Tab label. Set on every subclass.
    title = "Screen"

    #: Emitted on every notify() so tests and the main window can observe messages.
    notified = Signal(str)

    #: How long a transient notice stays on screen. 0 keeps it until replaced.
    NOTICE_TIMEOUT_MS = 6000

    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self._watched = []
        self._active = False
        self._notice = QLabel("")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.clear_notice)

    @property
    def notice_widget(self):
        """Add this to the screen's layout to show notices. Optional but expected."""
        return self._notice

    def notify(self, message):
        """Show a non-blocking notice. Replaces QMessageBox everywhere in this app."""
        self._notice.setText(message)
        self._notice.setVisible(bool(message))
        self.notified.emit(message)
        if message and self.NOTICE_TIMEOUT_MS:
            self._notice_timer.start(self.NOTICE_TIMEOUT_MS)

    def clear_notice(self):
        self._notice.clear()
        self._notice.setVisible(False)

    def watch(self, key, fn, interval_ms, slot):
        """Route a shared poller into `slot` for as long as this screen is visible.

        `key` is shared across screens — two screens watching "controller.state" share one timer.
        `fn` must be a cheap in-memory getter; blocking calls go through `hub.call_async`.
        """
        poller = self.hub.poller(key, fn, interval_ms)
        poller.updated.connect(slot)
        self._watched.append(poller)
        if self._active:
            poller.subscribe()
        return poller

    def showEvent(self, event):
        super().showEvent(event)
        if not self._active:
            self._active = True
            for poller in self._watched:
                poller.subscribe()
        self.on_activate()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._active:
            self._active = False
            for poller in self._watched:
                poller.unsubscribe()
        self.on_deactivate()

    def on_activate(self):
        """Hook for one-shot loads that should not run on a timer (saved configs, presets)."""

    def on_deactivate(self):
        """Hook for screens that must release something when hidden — e.g. stop an override."""
