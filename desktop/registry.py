"""Screen registry — the single shared file in the port.

Shard agents MUST NOT edit this file. Each shard adds only new modules under `desktop/screens/`
and reports its class name; the integrator adds the import and the SCREENS entry when merging.
That is what keeps five parallel shards from colliding.
"""

from desktop.screens.camera1 import Camera1Screen
from desktop.screens.config import ConfigScreen
from desktop.screens.connection import ConnectionScreen
from desktop.screens.debug import DebugScreen
from desktop.screens.graphs import GraphsScreen
from desktop.screens.home import HomeScreen
from desktop.screens.ip_camera import IpCameraScreen
from desktop.screens.pid_tuning import PidTuningScreen
from desktop.screens.pilot import PilotScreen
from desktop.screens.tooling import ToolingScreen

#: Ported screens, in tab order.
SCREENS = [
    HomeScreen,
    PilotScreen,
    ToolingScreen,
    DebugScreen,
    PidTuningScreen,
    GraphsScreen,
    ConfigScreen,
    ConnectionScreen,
    IpCameraScreen,
    Camera1Screen,
]

#: Was: screens awaiting a shard, rendered as disabled tabs. All ten are ported, so this is empty.
#: `main.py` still reads it — keep the hook rather than deleting it.
PENDING = []
