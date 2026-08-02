"""Component registry — the single shared file in the port.

Shard agents MUST NOT edit this file. Each shard adds only new modules under `desktop/screens/`
and exports a `COMPONENTS` list from its own module; the integrator adds the import and the
concatenation here when merging. That is what keeps parallel shards from colliding.

A *component* is anything the shell can put in a dock: a whole screen, or one panel lifted out of
one. Both are the same type, so the Components menu and the preset serializer need no special
cases.

**A decomposed screen keeps its whole-screen component, and declares what it `owns`.** Opening
the Pilot screen reserves every `panel.pilot.*` id, so the standalone manipulator panel is refused
while Pilot is open, and vice versa — you can never end up with two live widgets driving the same
actuator. Retiring the composites instead would close the same hole, but it would also mean the
Classic layout could no longer be "the ten screens tabified", which is the one arrangement every
operator already knows.

`owns` is derived from the id prefix, so adding a panel to a screen module automatically extends
that screen's reservation. The one exception is stated explicitly: Tooling reuses Pilot's
manipulator panel, so it reserves an id from another module's prefix.
"""

from desktop.component import Component
from desktop.screens.config import COMPONENTS as CONFIG_COMPONENTS
from desktop.screens.config import ConfigScreen
from desktop.screens.connection import COMPONENTS as CONNECTION_COMPONENTS
from desktop.screens.connection import ConnectionScreen
from desktop.screens.debug import COMPONENTS as DEBUG_COMPONENTS
from desktop.screens.debug import DebugScreen
from desktop.screens.graphs import COMPONENTS as GRAPH_COMPONENTS
from desktop.screens.graphs import GraphsScreen
from desktop.screens.home import COMPONENTS as HOME_COMPONENTS
from desktop.screens.home import HomeScreen
from desktop.screens.ip_camera import COMPONENTS as IP_CAMERA_COMPONENTS
from desktop.screens.ip_camera import IpCameraScreen
from desktop.screens.logs import COMPONENTS as LOG_COMPONENTS
from desktop.screens.logs import LogsScreen
from desktop.screens.pid_tuning import COMPONENTS as PID_COMPONENTS
from desktop.screens.pid_tuning import PidTuningScreen
from desktop.screens.pilot import COMPONENTS as PILOT_COMPONENTS
from desktop.screens.pilot import PilotScreen
from desktop.screens.tooling import COMPONENTS as TOOLING_COMPONENTS
from desktop.screens.tooling import ToolingScreen
from desktop.screens.workspaces import COMPONENTS as WORKSPACE_COMPONENTS

#: Panel components, grouped by the screen they were lifted out of.
PANEL_COMPONENTS = (
    list(PILOT_COMPONENTS)
    + list(TOOLING_COMPONENTS)
    + list(DEBUG_COMPONENTS)
    + list(PID_COMPONENTS)
    + list(GRAPH_COMPONENTS)
    + list(CONFIG_COMPONENTS)
    + list(CONNECTION_COMPONENTS)
    + list(LOG_COMPONENTS)
    + list(HOME_COMPONENTS)
    + list(IP_CAMERA_COMPONENTS)
)


def _ids(prefix):
    return tuple(c.id for c in PANEL_COMPONENTS if c.id.startswith(prefix))


def _screen(component_id, screen_cls, duplicable=False, order=0, owns=()):
    return Component(
        id=component_id,
        title=screen_cls.title,
        factory=screen_cls,
        category="Screens",
        duplicable=duplicable,
        owns=tuple(owns),
        order=order,
    )


#: Whole-screen components, in the order they appeared as tabs. Each declares the panels it is
#: built from so the single-instance guard covers its contents, not just its own id.
SCREEN_COMPONENTS = [
    _screen("screen.home", HomeScreen, duplicable=True, order=0, owns=_ids("panel.home.")),
    _screen("screen.pilot", PilotScreen, order=1, owns=_ids("panel.pilot.")),
    # Tooling embeds Pilot's manipulator panel but deliberately does NOT reserve that id: the
    # tabbed build already shipped a manipulator on both screens, so claiming it here would stop
    # Classic from opening Pilot and Tooling together. Two manipulators is last-write-wins on a
    # one-shot command, not a racing loop — unlike the debug override, which is properly guarded.
    _screen("screen.tooling", ToolingScreen, order=2, owns=_ids("panel.tooling.")),
    _screen("screen.debug", DebugScreen, order=3, owns=_ids("panel.debug.")),
    _screen("screen.pid_tuning", PidTuningScreen, order=4, owns=_ids("panel.pid_tuning.")),
    _screen("screen.graphs", GraphsScreen, duplicable=True, order=5, owns=_ids("panel.graphs.")),
    _screen("screen.config", ConfigScreen, order=6, owns=_ids("panel.config.")),
    _screen("screen.connection", ConnectionScreen, order=7, owns=_ids("panel.connection.")),
    _screen("screen.logs", LogsScreen, duplicable=True, order=8, owns=_ids("panel.logs.")),
    _screen("screen.ip_camera", IpCameraScreen, order=9, owns=_ids("panel.ip_camera.")),
]

#: Every component the shell can open.
COMPONENTS = list(SCREEN_COMPONENTS) + PANEL_COMPONENTS + list(WORKSPACE_COMPONENTS)

BY_ID = {c.id: c for c in COMPONENTS}
BY_TITLE = {c.title: c for c in COMPONENTS}

#: Menu grouping order. Categories not listed sort alphabetically after these.
CATEGORY_ORDER = [
    "Screens",
    "Pilot",
    "Tooling",
    "Debug",
    "PID",
    "Graphs",
    "Config",
    "Connection",
    "Logs",
    "Cameras",
    "Home",
    "Workspace",
]


def categories():
    """Component categories in menu order, each with its components in `order` order."""
    grouped = {}
    for component in COMPONENTS:
        grouped.setdefault(component.category, []).append(component)
    known = [c for c in CATEGORY_ORDER if c in grouped]
    rest = sorted(name for name in grouped if name not in CATEGORY_ORDER)
    return [(name, sorted(grouped[name], key=lambda c: c.order)) for name in known + rest]
