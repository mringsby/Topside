"""IP camera screen — ports `/ip-camera` and `/Camera2` (both render `ip_camera.html` +
`ip_camera.js`; `camera2.html` is dead per PARITY.md §2, not ported).

Preset CRUD and the manual-IP "Apply" reassign live in `IpCameraControlPanel`. `hub.reassign_ip_camera()`
tears down the old RTSP receiver and connects a new one — a multi-second blocking call per
PARITY.md §1 — so it always runs through `hub.call_async`. The 3 s status poll is a cheap in-memory
read and goes through `self.watch(...)`.

Video comes from the shared `widgets/camera.py::CameraWidget`, wrapped by `CameraViewPanel`. The
widget is handed a *callable* returning the receiver rather than the receiver itself —
`reassign_ip_camera` replaces that object outright, so a cached reference would keep painting the
dead stream.

Decomposed into two panels:

  * `CameraViewPanel` — just the video. Read-only, `duplicable=True`.
  * `IpCameraControlPanel` — the IP/URL form, presets, and Apply. Apply WRITES to the vehicle
    (reassigns the live RTSP receiver), so this stays single-instance (`duplicable=False`).

`IpCameraScreen` composes both side by side, same as before.
"""

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
from desktop.screens.base import PanelBase, ScreenBase
from desktop.widgets.camera import CameraWidget

STATUS_INTERVAL_MS = 3000

_FEEDBACK_STYLES = theme.BADGE


class CameraViewPanel(PanelBase):
    """Just the video feed. Read-only, safe to duplicate."""

    title = "IP Camera View"

    def __init__(self, hub, notify=None, parent=None):
        super().__init__(hub, parent)
        self._forward_notify = notify

        # The receiver is passed as a callable, not an object: reassign swaps hub.ip_camera.
        self._camera = CameraWidget(lambda: self.hub.ip_camera, min_size=(480, 270))
        self._camera.problem.connect(self.notify)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_widget)
        layout.addWidget(self._camera, 1)

    def notify(self, message):
        if self._forward_notify is not None:
            self._forward_notify(message)
        else:
            super().notify(message)

    def on_activate(self):
        self._camera.start()

    def on_deactivate(self):
        """Stop decoding JPEGs for a tab nobody is looking at."""
        self._camera.stop()


class IpCameraControlPanel(PanelBase):
    """IP/URL form, saved presets, and Apply. WRITES to the vehicle (reassigns the RTSP
    receiver), so this must never be duplicated live."""

    title = "IP Camera Controls"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._presets = []

        box = self._build_panel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_widget)
        layout.addWidget(box)

        self.watch("ip_camera.camera_status", self._status_fn, STATUS_INTERVAL_MS, self._on_status)

    # --- panel -------------------------------------------------------------------

    def _build_panel(self):
        box = QGroupBox("IP Camera")
        self._state_badge = QLabel("LOADING")
        self._state_badge.setStyleSheet(_FEEDBACK_STYLES["neutral"])

        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self._state_badge)

        self._active_ip_label = QLabel("--")
        self._active_url_label = QLabel("--")

        self._ip_input = QLineEdit(logic.DEFAULT_IP_CAMERA_IP)
        btn_apply = QPushButton("Apply")
        btn_apply.clicked.connect(self._apply_ip)
        self._btn_apply = btn_apply
        apply_row = QHBoxLayout()
        apply_row.addWidget(self._ip_input)
        apply_row.addWidget(btn_apply)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("pool")
        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self._save_preset)
        save_row = QHBoxLayout()
        save_row.addWidget(self._name_input)
        save_row.addWidget(btn_save)

        self._preset_combo = QComboBox()
        btn_load = QPushButton("Load")
        btn_load.clicked.connect(self._load_preset)
        btn_delete = QPushButton("Delete")
        btn_delete.clicked.connect(self._delete_preset)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self._preset_combo, 1)
        preset_row.addWidget(btn_load)
        preset_row.addWidget(btn_delete)

        self._feedback = QLabel("Ready.")
        self._feedback.setWordWrap(True)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(QLabel("Active:"))
        outer.addWidget(self._active_ip_label)
        outer.addWidget(QLabel("URL:"))
        outer.addWidget(self._active_url_label)
        outer.addWidget(QLabel("Manual IP"))
        outer.addLayout(apply_row)
        outer.addWidget(QLabel("Preset name"))
        outer.addLayout(save_row)
        outer.addWidget(QLabel("Saved presets"))
        outer.addLayout(preset_row)
        outer.addWidget(self._feedback)
        outer.addStretch(1)
        box.setMaximumWidth(360)
        return box

    def _set_feedback(self, text, tone="neutral"):
        self._feedback.setText(text)
        self._feedback.setStyleSheet(_FEEDBACK_STYLES[tone])

    # --- status -----------------------------------------------------------------

    def _status_fn(self):
        return self.hub.camera_status()

    def _on_status(self, result):
        _active_ip, _active_url, status = result
        self._set_state(status)

    def _set_state(self, status):
        if status and status.get("connected"):
            self._state_badge.setText("LIVE")
            self._state_badge.setStyleSheet(_FEEDBACK_STYLES["good"])
        else:
            self._state_badge.setText("OFFLINE")
            self._state_badge.setStyleSheet(_FEEDBACK_STYLES["bad"])

    # --- presets ----------------------------------------------------------------

    def _render_presets(self, presets):
        self._presets = presets or []
        combo = self._preset_combo
        combo.clear()
        if not self._presets:
            combo.addItem("No presets", None)
            return
        for preset in self._presets:
            combo.addItem(f"{preset['name']} - {preset['ip']}", preset)

    def _selected_preset(self):
        index = self._preset_combo.currentIndex()
        if index < 0:
            return None
        return self._preset_combo.itemData(index)

    def _select_preset_by_name(self, name):
        for index in range(self._preset_combo.count()):
            preset = self._preset_combo.itemData(index)
            if preset and preset.get("name") == name:
                self._preset_combo.setCurrentIndex(index)
                return

    def _save_preset(self):
        name = self._name_input.text().strip()
        ip = self._ip_input.text().strip()
        if not name or not ip:
            self._set_feedback("Enter preset name and IP.", "warn")
            return
        if not logic.valid_name(name):
            self._set_feedback("Invalid preset name.", "bad")
            return
        coerced_ip = logic.coerce_ipv4(ip)
        if not coerced_ip:
            self._set_feedback("Invalid IPv4 address.", "bad")
            return
        presets = logic.upsert_ip_camera_preset(name, coerced_ip)
        self._render_presets(presets)
        self._select_preset_by_name(name)
        self._set_feedback("Preset saved.", "good")

    def _delete_preset(self):
        preset = self._selected_preset()
        if not preset:
            self._set_feedback("Select a preset.", "warn")
            return
        presets = logic.delete_ip_camera_preset(preset["name"])
        if presets is None:
            self._set_feedback("Preset not found.", "bad")
            return
        self._render_presets(presets)
        self._set_feedback("Preset deleted.", "good")

    def _load_preset(self):
        preset = self._selected_preset()
        if not preset:
            self._set_feedback("Select a preset.", "warn")
            return
        self._name_input.setText(preset["name"])
        self._ip_input.setText(preset["ip"])
        self._set_feedback("Preset loaded.", "neutral")

    # --- apply / reassign -----------------------------------------------------------

    def _apply_ip(self):
        ip = self._ip_input.text().strip()
        coerced_ip = logic.coerce_ipv4(ip)
        if not coerced_ip:
            self._set_feedback("Enter an IP address.", "warn")
            return
        self._btn_apply.setEnabled(False)
        self._set_feedback("Applying IP...", "warn")
        self.hub.call_async(
            lambda: self.hub.reassign_ip_camera(coerced_ip),
            on_done=self._on_apply_done,
            on_error=self._on_apply_error,
        )

    def _on_apply_done(self, result):
        active_ip, active_url, status = result
        self._active_ip_label.setText(active_ip or "--")
        self._active_url_label.setText(active_url or "--")
        self._set_state(status)
        self._set_feedback("Camera reassigned.", "good")
        self._btn_apply.setEnabled(True)

    def _on_apply_error(self, message):
        self._set_feedback(f"Apply failed: {message}", "bad")
        self._btn_apply.setEnabled(True)

    # --- lifecycle ----------------------------------------------------------

    def on_activate(self):
        """One-shot load, mirroring ip_camera.js's loadConfigs() call on page load."""
        section = logic.get_ip_camera_config()
        self._render_presets(section["presets"])
        active_ip, active_url, status = self.hub.camera_status()
        self._active_ip_label.setText(active_ip or "--")
        self._active_url_label.setText(active_url or "--")
        if active_ip:
            self._ip_input.setText(active_ip)
        self._set_state(status)


class IpCameraScreen(ScreenBase):
    title = "IP Camera"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._camera_panel = CameraViewPanel(hub, notify=self.notify)
        self._control_panel = IpCameraControlPanel(hub)

        row = QHBoxLayout()
        row.addWidget(self._camera_panel, 1)
        row.addWidget(self._control_panel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(row)

    # --- back-compat delegate ----------------------------------------------------------
    # Nothing outside this module referenced screen internals before the split, but the camera
    # widget identity is the one thing worth keeping stable in case something starts.

    @property
    def _camera(self):
        return self._camera_panel._camera


#: The control panel WRITES to the vehicle (Apply reassigns the live RTSP receiver), so it must
#: stay single-instance. The video view only ever reads, so it is free to duplicate.
COMPONENTS = [
    Component(
        id="panel.ip_camera.view",
        title="IP Camera: View",
        factory=CameraViewPanel,
        category="Cameras",
        duplicable=True,
        order=0,
    ),
    Component(
        id="panel.ip_camera.controls",
        title="IP Camera: Controls",
        factory=IpCameraControlPanel,
        category="Cameras",
        duplicable=False,
        order=1,
    ),
]
