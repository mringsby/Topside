"""The workspace shell — a snap-to-grid canvas of components, not a fixed set of screens.

`Shell` is the app-level coordinator: it owns the one `ServiceHub`, every open `WorkspaceWindow`,
and the record of which components are open where. `WorkspaceWindow` is one grid canvas; there can
be several, on several monitors, all sharing the one hub.

Placement lives in `desktop/grid.py`; this module only decides *what* is on a canvas and *where a
preset says it goes*. Three rules carry the weight here:

**Only `Shell` shuts the hub down.** The old single window called `hub.shutdown()` straight from
its `closeEvent`. With N windows that would kill every service the moment the *first* one closed,
leaving the rest driving dead sockets in silence. Windows report their close upward instead, and
the hub goes down when the last one does.

**Components that write to the vehicle are single-instance.** Two live debug-override panels
would each run a 20 Hz command loop, making the killed -> override -> joystick priority in
`Controller.update()` non-deterministic. `Component.duplicable` is the single source of truth and
`open_component` is the only gate.

**A workspace starts empty.** There is no default arrangement any more: the operator builds one
from the Components menu and saves it. `Classic` survives only as a named preset — the ten screens
stacked full-width — because the grid has no tabs for it to reproduce.
"""

import base64

from PySide6.QtCore import QByteArray, QObject, Signal, qVersion
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication, QMainWindow

from desktop import logic, registry, theme
from desktop.grid import GridCanvas, Placement
from lib.runtime_paths import data_dir

#: Bumped when the preset schema changes. 1 was the dock shell's opaque `saveState()` blob; 2
#: stores each component's cells. `_placement_from` is what lets a 1 still load.
PRESET_VERSION = 2


class WorkspaceWindow(QMainWindow):
    """One grid canvas. Owns no services and never shuts anything down."""

    def __init__(self, shell, index=0):
        super().__init__()
        self.shell = shell
        self.index = index
        self.setObjectName(f"workspace.{index}")
        self.setWindowTitle("UiASub Topside" if index == 0 else f"UiASub Topside — Workspace {index + 1}")
        self.resize(1280, 860)
        self.canvas = GridCanvas(self)
        self.setCentralWidget(self.canvas)
        self.canvas.tile_closed.connect(self._on_tile_closed)
        self._theme_actions = {}
        self._build_menus()
        self.statusBar().showMessage(f"Data directory: {data_dir()}")
        theme.signals.changed.connect(self._sync_theme_actions)

    @property
    def tiles(self):
        return self.canvas.tiles

    # --- menus ---------------------------------------------------------------------------

    def _build_menus(self):
        components_menu = self.menuBar().addMenu("&Components")
        for category, components in registry.categories():
            target = components_menu if len(registry.categories()) == 1 else components_menu.addMenu(category)
            for component in components:
                action = QAction(component.title, self)
                action.triggered.connect(lambda _checked=False, c=component: self.shell.open_component(c, self))
                target.addAction(action)

        self.workspace_menu = self.menuBar().addMenu("&Workspace")
        self._rebuild_workspace_menu()
        self.shell.presets_changed.connect(self._rebuild_workspace_menu)

        view_menu = self.menuBar().addMenu("&View")
        theme_menu = view_menu.addMenu("&Theme")
        group = QActionGroup(self)
        group.setExclusive(True)
        for name, label in (("dark", "&Dark"), ("light", "&Light")):
            action = QAction(label, self, checkable=True)
            action.setChecked(theme.name() == name)
            action.triggered.connect(lambda _checked=False, n=name: self.shell.set_theme(n))
            group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[name] = action

    def _rebuild_workspace_menu(self):
        """Saved layouts are one click from the menu bar; naming a new one needs the panel."""
        menu = self.workspace_menu
        menu.clear()
        new_window = QAction("New &Window", self)
        new_window.triggered.connect(lambda: self.shell.new_window())
        menu.addAction(new_window)
        manage = QAction("&Manage Layouts…", self)
        manage.triggered.connect(lambda: self.shell.reveal("Workspaces", self))
        menu.addAction(manage)
        menu.addSeparator()
        for name in sorted(logic.load_workspace_presets()):
            action = QAction(name, self)
            action.triggered.connect(lambda _checked=False, n=name: self.shell.load_preset(n))
            menu.addAction(action)

    def _sync_theme_actions(self, theme_name):
        """Keep every window's radio buttons in step when any one of them changes the theme."""
        for name, action in self._theme_actions.items():
            action.setChecked(name == theme_name)

    # --- tiles ---------------------------------------------------------------------------

    def add_tile(self, component, panel, object_name, placement=None):
        """The canvas binds the panel to the tile itself — see `GridCanvas.add_tile`."""
        tile = self.canvas.add_tile(component.title, panel, placement)
        tile.setObjectName(object_name)
        return tile

    def _on_tile_closed(self, tile):
        """The tile's X button. The shell owns removal, because it owns the instance bookkeeping."""
        self.shell.close_tile(self, tile)

    def notify(self, message):
        """Non-modal status, matching PanelBase.notify()'s contract. Assertable in tests."""
        self.statusBar().showMessage(message, 6000)

    def closeEvent(self, event):
        for tile in list(self.tiles):
            panel = tile.widget()
            if panel is not None and hasattr(panel, "teardown"):
                panel.teardown()
        self.canvas.tiles.clear()
        self.shell.window_closed(self)
        super().closeEvent(event)


class Shell(QObject):
    """Owns the hub, the windows, and the open-component bookkeeping."""

    #: Every window rebuilds its Workspace menu from this.
    presets_changed = Signal()

    def __init__(self, hub, app=None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.app = app or QApplication.instance()
        self.windows = []
        #: component id -> list of {"window", "dock", "panel"}
        self.instances = {}
        self._counters = {}
        self._loading = False
        if self.app is not None:
            # A floating dock closing must never be mistaken for the last window closing.
            self.app.setQuitOnLastWindowClosed(False)

    # --- windows -------------------------------------------------------------------------

    def new_window(self, show=True):
        window = WorkspaceWindow(self, index=len(self.windows))
        self.windows.append(window)
        if show:
            window.show()
        return window

    def window_closed(self, window):
        if window in self.windows:
            self.windows.remove(window)
        for records in self.instances.values():
            records[:] = [r for r in records if r["window"] is not window]
        if not self.windows and not self._loading:
            self.shutdown()

    def shutdown(self):
        self.hub.shutdown()
        if self.app is not None:
            self.app.quit()

    # --- components ----------------------------------------------------------------------

    def _next_object_name(self, component):
        """Unique per instance, so a preset can tell two copies of a duplicable component apart.

        Only the trailing ordinal is ever parsed back (`_sync_counters`), so a preset written by
        the dock shell — whose names were `dock.<id>.<n>` — still restores.
        """
        ordinal = self._counters.get(component.id, 0)
        self._counters[component.id] = ordinal + 1
        return f"tile.{component.id}.{ordinal}"

    def _records(self, component_id):
        return self.instances.setdefault(component_id, [])

    def _claimed_ids(self):
        """Every component id currently occupied, counting what composites reserve on their behalf."""
        claimed = {}
        for records in self.instances.values():
            for record in records:
                for claim in record["component"].claims():
                    claimed.setdefault(claim, []).append(record)
        return claimed

    def open_component(self, component, window, object_name=None, placement=None):
        """Add `component` to `window`, or surface the existing one if it may not be duplicated.

        The guard covers what a component *contains*, not just its own id: opening the Pilot
        screen reserves the manipulator panel too, so the standalone manipulator is refused while
        Pilot is open — and vice versa. Read-only ids never conflict, however many copies exist.
        """
        claimed = self._claimed_ids()
        blocked = [claim for claim in component.claims() if claim in claimed and not registry.BY_ID[claim].duplicable]
        if blocked:
            holder = claimed[blocked[0]][0]
            holder["window"].canvas.reveal(holder["tile"])
            holder["window"].activateWindow()
            clash = registry.BY_ID[blocked[0]].title
            if holder["component"].id == component.id:
                window.notify(f"{component.title} is already open elsewhere — scrolled into view.")
            else:
                window.notify(f"{clash} is already open in {holder['component'].title} — scrolled into view.")
            return None

        panel = component.factory(self.hub)
        tile = window.add_tile(component, panel, object_name or self._next_object_name(component), placement)
        record = {"window": window, "tile": tile, "panel": panel, "component": component}
        self._records(component.id).append(record)
        self._wire_panel(panel, window)
        return record

    def close_tile(self, window, tile):
        """The X button removes the component for real, so it can be added again cleanly."""
        record = self._record_for_tile(tile)
        if record is None:
            return
        self._records(record["component"].id).remove(record)
        record["panel"].teardown()
        window.canvas.remove_tile(tile)

    def _wire_panel(self, panel, window):
        """Screens ask to navigate by title; in a dock world that means 'reveal that component'."""
        if hasattr(panel, "navigate_requested"):
            panel.navigate_requested.connect(lambda title: self.reveal(title, window))
        if hasattr(panel, "set_available_tabs"):
            panel.set_available_tabs([c.title for c in registry.COMPONENTS if c.category == "Screens"])
        if hasattr(panel, "save_requested"):
            panel.save_requested.connect(lambda name, p=panel: self._save_requested(name, p))
        if hasattr(panel, "load_requested"):
            panel.load_requested.connect(self.load_preset)

    def _save_requested(self, name, panel):
        """The panel owns the name field; only the shell can see every window to capture."""
        ok, message = logic.save_workspace_preset(name, self.capture_preset())
        if ok:
            logic.save_last_workspace(name)
            self.presets_changed.emit()
        if hasattr(panel, "refresh"):
            panel.refresh()
        if hasattr(panel, "report"):
            panel.report(message)
        return ok

    def reveal(self, title, window=None):
        """Scroll a component into view wherever it lives, or open it in `window` if it is not."""
        component = registry.BY_TITLE.get(title)
        if component is None:
            return None
        for record in self._records(component.id):
            record["window"].canvas.reveal(record["tile"])
            record["window"].activateWindow()
            return record
        target = window or (self.windows[0] if self.windows else self.new_window())
        return self.open_component(component, target)

    # --- theme ---------------------------------------------------------------------------

    def set_theme(self, theme_name):
        theme.apply(self.app, theme_name)
        logic.save_theme(theme_name)

    # --- presets -------------------------------------------------------------------------

    def capture_preset(self):
        """Serialize every open window.

        Cell placements are plain integers rather than an opaque `saveState()` blob: the grid owns
        its own geometry, so a preset stays readable, diffable, and immune to a Qt upgrade
        invalidating it. The *window's* own geometry stays a Qt blob — screen-relative placement
        and DPI are exactly what `saveGeometry()` handles better than four integers would.
        """
        windows = []
        for window in self.windows:
            components = []
            for tile in window.tiles:
                record = self._record_for_tile(tile)
                if record is None:
                    continue
                placement = tile.placement
                components.append(
                    {
                        "id": record["component"].id,
                        "object_name": tile.objectName(),
                        "col": placement.col,
                        "row": placement.row,
                        "cols": placement.cols,
                        "rows": placement.rows,
                    }
                )
            windows.append({"geometry_b64": _encode(window.saveGeometry()), "components": components})
        return {"version": PRESET_VERSION, "qt_version": qVersion(), "windows": windows}

    def _record_for_tile(self, tile):
        for records in self.instances.values():
            for record in records:
                if record["tile"] is tile:
                    return record
        return None

    def load_empty(self):
        """One empty workspace. The startup default, and the floor whenever a preset fails."""
        window = self.windows[0] if self.windows else self.new_window()
        return window

    def load_preset(self, name):
        """Rebuild every window from a saved layout. The hub is never rebuilt."""
        preset = logic.load_workspace_presets().get(name) if name else None
        if preset is None:
            # A layout deleted since it was last used must not start the app with no windows.
            if not self.windows:
                self.load_empty()
            return False
        self._loading = True
        try:
            for window in list(self.windows):
                window.close()
            self.instances.clear()
            self._counters.clear()
            for spec in preset.get("windows", []):
                self._restore_window(spec)
        finally:
            self._loading = False
        if not self.windows:  # an empty or unusable preset must not leave the operator with nothing
            self.load_empty()
            return False
        logic.save_last_workspace(name)
        self.presets_changed.emit()
        return True

    def _restore_window(self, spec):
        window = self.new_window(show=False)
        geometry = spec.get("geometry_b64")
        if geometry:
            window.restoreGeometry(_decode(geometry))
        # Show before placing: column width is a fraction of the canvas, so tiles restored into an
        # unshown window would all be sized against a zero-width viewport.
        window.show()
        for entry in spec.get("components", []):
            component = registry.BY_ID.get(entry["id"])
            if component is None:
                continue  # a component removed since the preset was saved: skip, do not fail
            self.open_component(
                component,
                window,
                object_name=entry.get("object_name"),
                placement=_placement_from(entry),
            )
        self._sync_counters()
        return window

    def _sync_counters(self):
        """Keep generated object names from colliding with ones a preset already restored."""
        for component_id, records in self.instances.items():
            highest = -1
            for record in records:
                tail = record["tile"].objectName().rsplit(".", 1)[-1]
                if tail.isdigit():
                    highest = max(highest, int(tail))
            self._counters[component_id] = max(self._counters.get(component_id, 0), highest + 1)


def _placement_from(entry):
    """Cell placement out of a preset entry, or None to let the canvas find a free slot.

    A version-1 preset (the dock shell's) carries no cells at all, and a hand-written one may
    carry only some. Either way the component still opens — it just gets auto-placed.
    """
    if not all(key in entry for key in ("col", "row", "cols", "rows")):
        return None
    return Placement(int(entry["col"]), int(entry["row"]), int(entry["cols"]), int(entry["rows"]))


def _encode(qbytearray):
    return base64.b64encode(bytes(qbytearray)).decode("ascii")


def _decode(text):
    return QByteArray(base64.b64decode(text.encode("ascii")))
