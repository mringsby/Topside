"""The component descriptor.

Lives in its own module rather than in `registry.py` so that screen modules can declare their
own `COMPONENTS` without importing the registry that imports them back.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Component:
    """One dockable thing. `id` is serialized into saved workspaces, so it must stay stable."""

    id: str
    title: str
    factory: Callable  # (hub) -> PanelBase
    category: str = "Screens"
    #: True only for read-only displays. A component that WRITES to the vehicle must stay
    #: single-instance: two live debug-override panels would each run a 20 Hz command loop and
    #: make the killed -> override -> joystick priority in Controller.update() non-deterministic.
    duplicable: bool = False
    #: Component ids this one contains. A composite screen declares the panels it is built from,
    #: so opening the screen reserves them too. Without this the single-instance rule is keyed on
    #: id alone, and opening "Pilot" plus the standalone "Manipulator" panel would give you two
    #: live widgets driving the same actuator — different ids, so the guard would never fire.
    owns: tuple = ()
    #: Ordering hint within a category; ties fall back to insertion order.
    order: int = field(default=0, compare=False)

    def claims(self):
        """Every id this component occupies while open: itself plus everything it contains."""
        return (self.id, *self.owns)
