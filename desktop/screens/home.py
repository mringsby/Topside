"""Home screen — ports `/` (`layout.html`).

The Flask page was a landing page: a title, the "verify Nucleo contact first" instruction, the
branch name, and four link cards. Links become tab switches: the panel/screen emits
`navigate_requested` with a tab title and `main.py` resolves it, so nothing here needs a reference
to the other screens.

One deliberate deviation from the template: `layout.html` hardcoded the branch as `gui-rework`.
That string was stale the moment it was typed. `/api/system/git` already existed to report the real
branch, so `BranchPanel` calls `logic.git_info()` through `call_async` (it shells out with a 1 s
timeout) and shows what the checkout actually is.

Decomposed into three read-only panels (`duplicable=True` — nothing here writes to the vehicle):
`AboutPanel` (title box + Open Connection shortcut), `BranchPanel` (the git branch box), and
`LinkCardsPanel` (the four nav cards). `AboutPanel` and `LinkCardsPanel` each expose their own
`navigate_requested`, so they work standalone in a dock; `HomeScreen` re-emits both on its own
`navigate_requested` (and keeps `set_available_tabs`), because `shell.py` duck-types both on
whichever object is registered as the Home component.
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
from desktop.component import Component
from desktop.screens.base import PanelBase, ScreenBase

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


class AboutPanel(PanelBase):
    """The title box: intro blurb + "Open Connection" shortcut. Read-only, safe to duplicate."""

    title = "About"

    #: Emitted with a tab title when the operator clicks the shortcut.
    navigate_requested = Signal(str)

    def __init__(self, hub, notify=None, parent=None):
        super().__init__(hub, parent)
        self._forward_notify = notify

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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_widget)
        layout.addWidget(about_box)

    def notify(self, message):
        if self._forward_notify is not None:
            self._forward_notify(message)
        else:
            super().notify(message)

    def set_available_tabs(self, titles):
        """Called by the main window once all tabs exist, so a dead shortcut reads as disabled."""
        self._btn_connection.setEnabled("Connection" in set(titles))


class BranchPanel(PanelBase):
    """The branch/git box. Read-only, safe to duplicate."""

    title = "Branch"

    def __init__(self, hub, notify=None, parent=None):
        super().__init__(hub, parent)
        self._forward_notify = notify

        self._branch_label = QLabel("...")
        branch_box = QGroupBox("Branch")
        branch_layout = QVBoxLayout(branch_box)
        branch_layout.addWidget(self._branch_label)
        branch_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_widget)
        layout.addWidget(branch_box)

    def notify(self, message):
        if self._forward_notify is not None:
            self._forward_notify(message)
        else:
            super().notify(message)

    def on_activate(self):
        """One-shot, not a poller: the branch cannot change while the app is running."""
        if self._branch_label.text() not in ("", "..."):
            return
        self.hub.call_async(logic.git_info, on_done=self._on_git, on_error=self._on_git_error)

    def _on_git(self, info):
        self._branch_label.setText(f"<b>{(info or {}).get('branch', 'unknown')}</b>")

    def _on_git_error(self, message):
        self._branch_label.setText("<b>unknown</b>")
        self.notify(f"Could not read git branch: {message}")


class LinkCardsPanel(PanelBase):
    """The four nav cards. Read-only, safe to duplicate."""

    title = "Quick Links"

    #: Emitted with a tab title when the operator clicks a card.
    navigate_requested = Signal(str)

    def __init__(self, hub, notify=None, parent=None):
        super().__init__(hub, parent)
        self._forward_notify = notify

        self._cards = {}
        cards = QGridLayout()
        for index, (title, blurb) in enumerate(LINK_CARDS):
            card = LinkCard(title, blurb, self._navigator(title))
            self._cards[title] = card
            cards.addWidget(card, index // 2, index % 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_widget)
        layout.addLayout(cards)
        layout.addStretch(1)

    def notify(self, message):
        if self._forward_notify is not None:
            self._forward_notify(message)
        else:
            super().notify(message)

    def _navigator(self, title):
        return lambda: self.navigate_requested.emit(title)

    def set_available_tabs(self, titles):
        """Called by the main window once all tabs exist, so dead links read as disabled."""
        available = set(titles)
        for title, card in self._cards.items():
            card.set_available(title in available)


class HomeScreen(ScreenBase):
    title = "Home"

    #: Emitted with a tab title when the operator clicks a nav card or the Connection shortcut.
    #: `shell.py` duck-types this on whichever object is registered as the Home component; kept
    #: here (re-emitting both child panels' signals) so Home stays wired even though the cards and
    #: the shortcut now live in their own panels.
    navigate_requested = Signal(str)

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._about_panel = AboutPanel(hub, notify=self.notify)
        self._branch_panel = BranchPanel(hub, notify=self.notify)
        self._link_cards_panel = LinkCardsPanel(hub, notify=self.notify)
        self._about_panel.navigate_requested.connect(self.navigate_requested.emit)
        self._link_cards_panel.navigate_requested.connect(self.navigate_requested.emit)

        top = QHBoxLayout()
        top.addWidget(self._about_panel, 2)
        top.addWidget(self._branch_panel, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(top)
        layout.addWidget(self._link_cards_panel)
        layout.addStretch(1)

    def set_available_tabs(self, titles):
        """Called by the main window once all tabs exist, so dead links read as disabled."""
        self._about_panel.set_available_tabs(titles)
        self._link_cards_panel.set_available_tabs(titles)


#: Every panel here is read-only, so all three are independently placeable AND duplicable —
#: nothing writes to the vehicle. `HomeScreen` stays registered as the whole landing page for the
#: same reason: a read-only screen has no single-instance hazard.
COMPONENTS = [
    Component(
        id="panel.home.about",
        title="Home: About",
        factory=AboutPanel,
        category="Home",
        duplicable=True,
        order=0,
    ),
    Component(
        id="panel.home.branch",
        title="Home: Branch",
        factory=BranchPanel,
        category="Home",
        duplicable=True,
        order=1,
    ),
    Component(
        id="panel.home.links",
        title="Home: Quick Links",
        factory=LinkCardsPanel,
        category="Home",
        duplicable=True,
        order=2,
    ),
]
