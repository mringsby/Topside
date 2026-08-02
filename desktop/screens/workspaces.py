"""Workspaces panel — save, load and delete named window layouts.

The panel never touches a `Shell` directly: `PanelBase.__init__` only receives `hub`, and capturing
or rebuilding the actual dock/window tree is `Shell`'s job (`capture_preset()` / `load_preset()`).
Instead this panel emits `save_requested(name)` / `load_requested(name)`, which the shell connects
to its own preset machinery; the shell then calls `report()` with the result and `refresh()` to
pick up the new preset list. Deleting a preset touches only `logic.py`, so it needs no shell
involvement at all.

All naming rules (the `Classic` reserved name, the name pattern) live in `desktop/logic.py`
(`save_workspace_preset` / `delete_workspace_preset`) — this panel surfaces their `message` rather
than re-deriving it.

Deleting a saved layout follows the same two-step arm as `pid_tuning.py::_delete_config` /
`connection.py::_on_reset_clicked`: no `QMessageBox`, just a relabelled button that must be clicked
twice within `DELETE_ARM_TIMEOUT_MS`.
"""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from desktop import logic, theme
from desktop.component import Component
from desktop.screens.base import PanelBase

#: How long the Delete button stays armed awaiting a second click.
DELETE_ARM_TIMEOUT_MS = 5000


class WorkspacesPanel(PanelBase):
    title = "Workspaces"

    #: Layout name to save the current window arrangement under. The shell captures the preset
    #: (`Shell.capture_preset()`) and writes it (`logic.save_workspace_preset`).
    save_requested = Signal(str)
    #: Layout name to rebuild. The shell rebuilds every window (`Shell.load_preset()`).
    load_requested = Signal(str)

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._delete_armed = False
        self._delete_arm_timer = QTimer(self)
        self._delete_arm_timer.setSingleShot(True)
        self._delete_arm_timer.timeout.connect(self._disarm_delete)
        self._badge_variant = "neutral"

        root = QVBoxLayout(self)
        root.addWidget(self.notice_widget)

        current_row = QHBoxLayout()
        current_row.addWidget(QLabel("Current layout:"))
        self._current_label = QLabel("--")
        current_row.addWidget(self._current_label)
        current_row.addStretch(1)
        root.addLayout(current_row)

        box = QGroupBox("Saved Layouts")
        box_layout = QVBoxLayout(box)

        save_row = QHBoxLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Layout name")
        self._btn_save = QPushButton("Save")
        self._btn_save.clicked.connect(self._on_save_clicked)
        save_row.addWidget(self._name_edit)
        save_row.addWidget(self._btn_save)
        box_layout.addLayout(save_row)

        select_row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.addItem("Load layout", "")
        self._btn_load = QPushButton("Load")
        self._btn_load.clicked.connect(self._on_load_clicked)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete_clicked)
        self._status_badge = QLabel("-")
        self._status_badge.setStyleSheet(theme.badge(self._badge_variant))
        select_row.addWidget(self._combo)
        select_row.addWidget(self._btn_load)
        select_row.addWidget(self._btn_delete)
        select_row.addWidget(self._status_badge)
        box_layout.addLayout(select_row)

        root.addWidget(box)
        root.addStretch(1)

    # --- lifecycle -----------------------------------------------------------

    def on_activate(self):
        self.refresh()

    def on_deactivate(self):
        self._disarm_delete()

    def _on_theme_changed(self, theme_name):
        self._status_badge.setStyleSheet(theme.badge(self._badge_variant))

    # --- public API used by the shell -----------------------------------------

    def refresh(self):
        """Re-read saved presets and the last-loaded name. Called on activate and after a save."""
        self._refresh_list()
        self._current_label.setText(logic.load_last_workspace())

    def report(self, message):
        """Forward a result from the shell's save/load work to the operator."""
        self.notify(message)

    # --- combo box -------------------------------------------------------------

    def _refresh_list(self):
        presets = logic.load_workspace_presets()
        current = self._combo.currentData()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("Load layout", "")
        for name in sorted(presets.keys()):
            self._combo.addItem(name, name)
        if current:
            index = self._combo.findData(current)
            if index >= 0:
                self._combo.setCurrentIndex(index)
        self._combo.blockSignals(False)

    def _set_badge(self, text, variant="neutral"):
        self._badge_variant = variant
        self._status_badge.setText(text)
        self._status_badge.setStyleSheet(theme.badge(variant))

    # --- save / load -----------------------------------------------------------

    def _on_save_clicked(self):
        name = self._name_edit.text().strip()
        self.save_requested.emit(name)

    def _on_load_clicked(self):
        name = self._combo.currentData()
        if not name:
            self._set_badge("Select a layout", "warning")
            return
        self.load_requested.emit(name)

    # --- delete (two-step arm, not a modal) -------------------------------------

    def _disarm_delete(self):
        self._delete_armed = False
        self._delete_arm_timer.stop()
        self._btn_delete.setText("Delete")

    def _on_delete_clicked(self):
        name = self._combo.currentData()
        if not name:
            self._set_badge("Select a layout", "warning")
            return

        if not self._delete_armed:
            self._delete_armed = True
            self._btn_delete.setText("Confirm delete?")
            self._set_badge("Confirm", "warning")
            self._delete_arm_timer.start(DELETE_ARM_TIMEOUT_MS)
            return
        self._disarm_delete()

        ok, message = logic.delete_workspace_preset(name)
        self._set_badge("Deleted" if ok else "Error", "success" if ok else "danger")
        self.report(message)
        if ok:
            self.refresh()


COMPONENTS = [
    Component(
        id="panel.workspaces",
        title="Workspaces",
        factory=WorkspacesPanel,
        category="Workspace",
        duplicable=False,
        order=0,
    )
]
