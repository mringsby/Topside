"""PanelBase / ScreenBase — the pattern every dockable component follows.

A panel declares what it watches with `self.watch(...)`; the base class subscribes when the panel
becomes active and unsubscribes when it does not. That reproduces the Flask behaviour where a page
that was not open issued no polls, and it is why a 60 Hz slider panel does not tax the machine
while the operator is looking at the graphs.

**Activation is not driven by `showEvent` alone.** A `QDockWidget` tabbed *behind* another tab
fires no hide event and keeps `isVisible() == True` — `raise_()` produces no show/hide events at
all. So a dock-hosted panel hands activation over to `QDockWidget.visibilityChanged` via
`bind_host()`, which is correct for every case (closed, tabbed-behind, minimized, window hidden).
Un-hosted panels — a plain layout, or a test that constructs a screen directly — keep the old
show/hide behaviour.

A `ScreenBase` is just a panel that happens to fill a whole dock.
"""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLabel, QWidget

from desktop import theme


class PanelBase(QWidget):
    """Base for every dockable, hub-backed component.

    Never use QMessageBox for status or errors. A modal dialog blocks the Qt event loop, which
    stalls every poller and the 20 Hz command loop behind it, and makes the panel impossible to
    test headlessly. Use `self.notify(...)` — it is non-blocking and assertable.
    """

    #: Dock title. Set on every subclass.
    title = "Panel"

    #: Emitted on every notify() so tests and the shell can observe messages.
    notified = Signal(str)

    #: How long a transient notice stays on screen. 0 keeps it until replaced.
    NOTICE_TIMEOUT_MS = 6000

    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self.hub = hub
        #: (poller, slot) pairs. The slot is kept so teardown() can disconnect *this* panel
        #: without severing every other panel's connection to the same shared poller.
        self._watched = []
        self._active = False
        self._host_driven = False
        self._notice = QLabel("")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.clear_notice)
        theme.signals.changed.connect(self._on_theme_changed)

    @property
    def notice_widget(self):
        """Add this to the panel's layout to show notices. Optional but expected."""
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
        """Route a shared poller into `slot` for as long as this panel is active.

        `key` is shared across panels — two panels watching "controller.state" share one timer.
        `fn` must be a cheap in-memory getter; blocking calls go through `hub.call_async`.
        """
        poller = self.hub.poller(key, fn, interval_ms)
        poller.updated.connect(slot)
        self._watched.append((poller, slot))
        if self._active:
            poller.subscribe()
        return poller

    # --- lifecycle -------------------------------------------------------------------------

    def bind_host(self, dock):
        """Hand activation to the dock. Called by the shell, never by the panel itself."""
        self._host_driven = True
        dock.visibilityChanged.connect(self.set_active)

    def set_active(self, active):
        """Subscribe or release every watched poller, then run the activate hook.

        Idempotent, so the duplicate calls produced by the child cascade cost nothing.
        """
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        for poller, _slot in self._watched:
            poller.subscribe() if active else poller.unsubscribe()
        # findChildren is transitive on purpose: a panel nested inside a QGroupBox is a
        # grandchild, so direct children alone would miss it.
        for child in self.findChildren(PanelBase):
            child.set_active(active)
        (self.on_activate if active else self.on_deactivate)()

    def teardown(self):
        """Release shared-poller refcounts before this panel is destroyed.

        Screens used to live for the whole process, so a leaked subscribe() was impossible. A
        closeable dock makes it the default failure: close an active panel without this and the
        poller's refcount never returns to 0, so its timer runs forever.
        """
        for child in self.findChildren(PanelBase):
            child.teardown()
        self.set_active(False)
        for poller, slot in self._watched:
            poller.updated.disconnect(slot)
        self._watched.clear()

    def _host_managed(self):
        """True when this panel, or any ancestor panel, has handed activation to a dock.

        Walks up rather than caching a flag: children can be built after bind_host() runs, and a
        child that self-activates on showEvent would poll while its dock sits behind another tab.
        """
        node = self
        while node is not None:
            if isinstance(node, PanelBase) and node._host_driven:
                return True
            node = node.parentWidget()
        return False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._host_managed():
            self.set_active(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._host_managed():
            self.set_active(False)

    def on_activate(self):
        """Hook for one-shot loads that should not run on a timer (saved configs, presets)."""

    def on_deactivate(self):
        """Hook for panels that must release something when hidden — e.g. stop an override."""

    def _on_theme_changed(self, theme_name):
        """Re-apply styling that does not refresh itself on the next poll tick. Default no-op."""


class ScreenBase(PanelBase):
    """A panel that fills a whole dock. Kept as its own name because all ten screens subclass it."""

    title = "Screen"
