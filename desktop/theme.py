"""Centralised colour tokens for the desktop app: a dark (default) and a light theme.

Every hex that used to be hardcoded inline across `screens/*.py` and `widgets/*.py` lives here
instead, keyed by token name. Screens should read colours through `token(key)` or `badge(variant)`
rather than hardcoding hex strings, so switching the active theme changes every consumer at once.

No widget imports beyond `QPalette`/`QColor`/`QApplication` — this module must stay importable
from anywhere in `desktop/` without pulling in a specific screen.

Module-level dicts elsewhere (e.g. `screens/pilot.py::_BADGE_STYLES`) must NOT be built by calling
`token()`/`badge()` once at import time — that would freeze them to whatever theme is active when
the module is first imported (which happens before `main()` calls `apply()`), regardless of the
theme the user actually has selected. Point such call sites at `BADGE` (see below), which resolves
lazily on each `[...]` access.
"""

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette

#: DARK is the app's original palette -- every hex here matches what was previously hardcoded
#: inline in screens/pilot.py, screens/connection.py, screens/config.py, screens/ip_camera.py,
#: screens/pid_tuning.py and widgets/sparkline.py.
DARK = {
    "success": "#3fb950",
    "danger": "#f85149",
    "warning": "#d29922",
    "muted": "#adb5bd",
    "info": "#0dcaf0",
    "secondary": "#6c757d",
    "plot.bg": "#14151d",
    "plot.fg": "#adb5bd",
    "setpoint": "#ff4d6d",
    "axis.yaw": "#0dcaf0",
    "axis.pitch": "#ffc107",
    "axis.roll": "#198754",
}

#: LIGHT has its OWN hexes, not the dark ones. The dark tokens above (success/warning/muted in
#: particular) were picked against a near-black background and read as low-contrast pastel on a
#: light one. Each value below is darkened until it clears ~4.5:1 (WCAG AA, normal text) against
#: the light theme's window colour (#f3f3f3), computed via the standard WCAG relative-luminance
#: formula:
#:   success  #157347 vs #f3f3f3 ~= 5.3:1
#:   danger   #b02a37 vs #f3f3f3 ~= 5.9:1
#:   warning  #664d03 vs #f3f3f3 ~= 7.2:1
#:   muted    #5c636a vs #f3f3f3 ~= 5.5:1
#:   info     #0b7285 vs #f3f3f3 ~= 5.0:1
#:   setpoint #a4133c vs #f3f3f3 ~= 6.9:1
#: "secondary" backs solid-fill pill badges (pid_tuning.py) rather than text on the window
#: background, so it's judged against its own paired white/black text instead of #f3f3f3.
LIGHT = {
    "success": "#157347",
    "danger": "#b02a37",
    "warning": "#664d03",
    "muted": "#5c636a",
    "info": "#0b7285",
    "secondary": "#495057",
    "plot.bg": "#ffffff",
    "plot.fg": "#5c636a",
    "setpoint": "#a4133c",
    "axis.yaw": "#0b7285",
    "axis.pitch": "#664d03",
    "axis.roll": "#157347",
}

_THEMES = {"dark": DARK, "light": LIGHT}

_active = "dark"


class _ThemeSignals(QObject):
    changed = Signal(str)


signals = _ThemeSignals()


def name():
    """The active theme's name, `"dark"` or `"light"`."""
    return _active


def token(key):
    """Colour hex for `key` under the ACTIVE theme."""
    return _THEMES[_active][key]


#: variant -> token key; `None` means "no override, use the palette's own text colour" (matches
#: the pre-existing "neutral"/"secondary" style strings verbatim, e.g. "color: palette(text);").
_BADGE_ALIASES = {
    "neutral": None,
    "secondary": None,
    "success": "success",
    "good": "success",
    "warning": "warning",
    "warn": "warning",
    "danger": "danger",
    "bad": "danger",
}


def badge(variant):
    """QSS colour style for a badge/status label, e.g. `"color: #3fb950; font-weight: 600;"`.

    Accepts the union of variant vocabularies already used across screens (pilot.py/config.py's
    "good"/"warn"/"bad"/"neutral", connection.py's "success"/"warning"/"danger"/"secondary").
    """
    token_key = _BADGE_ALIASES[variant]
    if token_key is None:
        return "color: palette(text);"
    return f"color: {token(token_key)}; font-weight: 600;"


class _BadgeMap:
    """Dict-like proxy so existing `_BADGE_STYLES["variant"]` call sites keep working unchanged
    while resolving lazily against the active theme (see module docstring)."""

    def __getitem__(self, variant):
        return badge(variant)

    def get(self, variant, default=None):
        try:
            return self[variant]
        except KeyError:
            return default


#: Drop-in replacement for the per-screen `_BADGE_STYLES` / `_FEEDBACK_STYLES` / `_STATUS_STYLES`
#: dicts -- assign `_BADGE_STYLES = theme.BADGE` and every existing `_BADGE_STYLES["tone"]` call
#: site keeps working, now theme-aware.
BADGE = _BadgeMap()


def build_palette(theme_name):
    """A full QPalette for `theme_name` ("dark" or "light")."""
    if theme_name not in _THEMES:
        raise ValueError(f"Unknown theme: {theme_name!r}")

    palette = QPalette()
    if theme_name == "dark":
        window = QColor(37, 37, 38)
        base = QColor(30, 30, 30)
        alt_base = QColor(45, 45, 48)
        text = QColor(220, 220, 220)
        disabled_text = QColor(127, 127, 127)
        button = QColor(45, 45, 48)
        tooltip_base = QColor(45, 45, 48)
        highlight = QColor(38, 79, 120)
        highlight_text = QColor(255, 255, 255)
        link = QColor(100, 170, 255)
    else:
        window = QColor(243, 243, 243)
        base = QColor(255, 255, 255)
        alt_base = QColor(233, 233, 233)
        text = QColor(20, 20, 20)
        disabled_text = QColor(150, 150, 150)
        button = QColor(233, 233, 233)
        tooltip_base = QColor(255, 255, 220)
        highlight = QColor(0, 120, 215)
        highlight_text = QColor(255, 255, 255)
        link = QColor(0, 90, 200)

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QPalette.ColorRole.ToolTipBase, tooltip_base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, button)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("red"))
    palette.setColor(QPalette.ColorRole.Link, link)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, highlight_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    return palette


def apply(app, theme_name):
    """Switch the whole app to `theme_name` ("dark" or "light"). Live -- no widget rebuild needed."""
    global _active
    if theme_name not in _THEMES:
        raise ValueError(f"Unknown theme: {theme_name!r}")

    app.setStyle("Fusion")
    app.setPalette(build_palette(theme_name))
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark if theme_name == "dark" else Qt.ColorScheme.Light)
    _active = theme_name
    signals.changed.emit(theme_name)
