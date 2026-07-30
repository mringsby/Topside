"""Desktop entry point. Replaces app.py's Flask server with a Qt event loop.

Run with:  uv run python -m desktop
"""

import signal
import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QTabWidget

from desktop import registry
from desktop.services import ServiceHub
from lib.runtime_paths import data_dir


class MainWindow(QMainWindow):
    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.setWindowTitle("UiASub Topside")
        self.resize(1280, 860)

        self._tabs = QTabWidget()
        screens = []
        for screen_cls in registry.SCREENS:
            screen = screen_cls(hub)
            screens.append(screen)
            self._tabs.addTab(screen, screen_cls.title)

        for title, owner in registry.PENDING:
            placeholder = QLabel(f"{title} — not yet ported ({owner})")
            placeholder.setEnabled(False)
            index = self._tabs.addTab(placeholder, title)
            self._tabs.setTabEnabled(index, False)

        # Screens ask to navigate by tab title; only the window knows the tab order.
        live_titles = [cls.title for cls in registry.SCREENS]
        for screen in screens:
            if hasattr(screen, "navigate_requested"):
                screen.navigate_requested.connect(self.show_screen)
            if hasattr(screen, "set_available_tabs"):
                screen.set_available_tabs(live_titles)

        self.setCentralWidget(self._tabs)
        self.statusBar().showMessage(f"Data directory: {data_dir()}")

    def show_screen(self, title):
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == title and self._tabs.isTabEnabled(index):
                self._tabs.setCurrentIndex(index)
                return

    def closeEvent(self, event):
        self.hub.shutdown()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    # Let Ctrl+C in a terminal kill the app instead of being swallowed by the Qt loop.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    hub = ServiceHub()
    window = MainWindow(hub)
    window.show()
    try:
        return app.exec()
    finally:
        hub.shutdown()


if __name__ == "__main__":
    sys.exit(main())
