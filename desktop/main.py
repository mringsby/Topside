"""Desktop entry point. Replaces app.py's Flask server with a Qt event loop.

Run with:  uv run python -m desktop

The window itself lives in `desktop/shell.py`: `Shell` owns the one `ServiceHub` and every
`WorkspaceWindow`, so this file only builds the application, applies the saved theme, and hands
over. Shutdown is `Shell`'s call — the `finally` here is belt-and-braces for a crash on the way
up, and `ServiceHub.shutdown()` is idempotent so the double call is harmless.
"""

import signal
import sys

from PySide6.QtWidgets import QApplication

from desktop import logic, theme
from desktop.services import ServiceHub
from desktop.shell import Shell


def main():
    app = QApplication(sys.argv)
    theme.apply(app, logic.load_theme())
    # Let Ctrl+C in a terminal kill the app instead of being swallowed by the Qt loop.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    hub = ServiceHub()
    shell = Shell(hub, app=app)
    # No saved layout, or one that can no longer be restored, opens an empty workspace rather
    # than nothing — Shell.load_preset handles both on its own.
    shell.load_preset(logic.load_last_workspace())
    try:
        return app.exec()
    finally:
        hub.shutdown()


if __name__ == "__main__":
    sys.exit(main())
