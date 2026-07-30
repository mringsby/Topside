"""Home screen — ports `/` (`layout.html`).

The Flask page was a landing page: a title, the "verify Nucleo contact first" instruction, the
branch name, and four link cards. Links become tab switches: the screen emits `navigate_requested`
with a tab title and `main.py` resolves it, so this screen needs no reference to the other screens.

One deliberate deviation from the template: `layout.html` hardcoded the branch as `gui-rework`.
That string was stale the moment it was typed. `/api/system/git` already existed to report the real
branch, so this screen calls `logic.git_info()` through `call_async` (it shells out with a 1 s
timeout) and shows what the checkout actually is.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from desktop import logic
from desktop.screens.base import ScreenBase

#: (tab title, blurb) — mirrors the four link cards in layout.html.
LINK_CARDS = [
    ("IP Camera", "Live camera view and IP presets."),
    ("Tooling", "Lights and manipulator controls."),
    ("Config", "Axis mapping, gains, and offsets."),
    ("Debug", "Override sliders, telemetry, and logs."),
]


class LinkCard(QFrame):
    """One clickable nav card. Disabled when its target screen is not registered yet."""

    def __init__(self, title, blurb, on_click, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        heading = QLabel(f"<b>{title}</b>")
        body = QLabel(blurb)
        body.setWordWrap(True)

        self._button = QPushButton(f"Open {title}")
        self._button.clicked.connect(on_click)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addWidget(self._button)

    def set_available(self, available):
        self._button.setEnabled(available)
        self._button.setToolTip("" if available else "Not ported yet")


class HomeScreen(ScreenBase):
    title = "Home"

    #: Emitted with a tab title when the operator clicks a nav card.
    navigate_requested = Signal(str)

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        intro = QLabel(
            "Start in the Connection tab and verify Nucleo contact before using camera, tooling, debug, or pilot."
        )
        intro.setWordWrap(True)

        self._btn_connection = QPushButton("Open Connection")
        self._btn_connection.clicked.connect(lambda: self.navigate_requested.emit("Connection"))

        about_box = QGroupBox("UiASub Topside")
        about_layout = QVBoxLayout(about_box)
        about_layout.addWidget(intro)
        about_layout.addWidget(self._btn_connection)
        about_layout.addStretch(1)

        self._branch_label = QLabel("...")
        branch_box = QGroupBox("Branch")
        branch_layout = QVBoxLayout(branch_box)
        branch_layout.addWidget(self._branch_label)
        branch_layout.addStretch(1)

        top = QHBoxLayout()
        top.addWidget(about_box, 2)
        top.addWidget(branch_box, 1)

        self._cards = {}
        cards = QGridLayout()
        for index, (title, blurb) in enumerate(LINK_CARDS):
            card = LinkCard(title, blurb, self._navigator(title))
            self._cards[title] = card
            cards.addWidget(card, index // 2, index % 2)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(top)
        layout.addLayout(cards)
        layout.addStretch(1)

    def set_available_tabs(self, titles):
        """Called by the main window once all tabs exist, so dead links read as disabled."""
        available = set(titles)
        self._btn_connection.setEnabled("Connection" in available)
        for title, card in self._cards.items():
            card.set_available(title in available)

    # --- data ---------------------------------------------------------------

    def on_activate(self):
        """One-shot, not a poller: the branch cannot change while the app is running."""
        if self._branch_label.text() not in ("", "..."):
            return
        self.hub.call_async(logic.git_info, on_done=self._on_git, on_error=self._on_git_error)

    def _navigator(self, title):
        return lambda: self.navigate_requested.emit(title)

    def _on_git(self, info):
        self._branch_label.setText(f"<b>{(info or {}).get('branch', 'unknown')}</b>")

    def _on_git_error(self, message):
        self._branch_label.setText("<b>unknown</b>")
        self.notify(f"Could not read git branch: {message}")
