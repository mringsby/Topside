"""Pilot screen — ports `/pilot` (`pilot.html` + `pilot.js`, 269 lines).

A full-screen HUD: live IP camera feed, depth readout, ARUCO marker log, a lights slider, status
dots + mission clock, and the shared `ManipulatorWidget` (see `desktop/widgets/manipulator.py` —
`manipulator.js` is loaded by both this screen and `screens/tooling.py`; it is not duplicated
here).

Decomposed into six dockable panels (`CameraPanel`, `ArucoPanel`, `DepthPanel`, `LightsPanel`,
`ManipulatorPanel`, `StatusPanel`), following the pattern set by `screens/graphs.py`: each panel
does its own `self.watch(...)` and is self-sufficient in a standalone dock; `PilotScreen` just
composes them and lets `PanelBase.set_active`'s child cascade drive their activation.
`ManipulatorPanel` is also imported by `screens/tooling.py` rather than rebuilt there.

No `window.confirm(...)` sites in `pilot.js` — grepped for `confirm(`, none found. Nothing on this
screen is destructive enough to warrant one either: lights and the manipulator are continuous
controls, and the camera Reconnect button only forces the already-self-reconnecting RTSP receiver
to reconnect sooner.

`/api/depth` is a placeholder per PARITY.md §3 — `pilot.js` has always displayed whatever is
sitting in `data.json`'s "depth" section, and nothing writes it. Reproduced as-is, not invented.

The camera feed has no MJPEG `<img>` tag to reconnect in this port; `IPCameraReceiver` already
retries its own RTSP connection on a background thread (`RECONNECT_DELAY = 3.0s` in
`lib/camera.py`), so "Reconnect" here calls `hub.reassign_ip_camera()` (async — it's the
multi-second RTSP-reconnect call from PARITY.md §1) against the same IP to force an immediate
retry, which is the closest faithful analog to the browser reloading `<img src>`.
"""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from desktop import logic, theme
from desktop.component import Component
from desktop.screens.base import PanelBase, ScreenBase
from desktop.widgets.camera import CameraWidget
from desktop.widgets.manipulator import ManipulatorWidget

CAMERA_STATUS_INTERVAL_MS = 5000
DEPTH_INTERVAL_MS = 1000
LIGHTS_INTERVAL_MS = 3000
ARUCO_INTERVAL_MS = 1000
MANIPULATOR_INTERVAL_MS = 250
MISSION_TIMER_MS = 1000

#: Throttle outgoing light POSTs while dragging — mirrors pilot.js's queueLightPost/100ms.
LIGHT_POST_THROTTLE_MS = 100

_BADGE_STYLES = theme.BADGE


# --- shared poll getters (module-level so more than one panel can feed from the same key) --------


def camera_status(hub):
    """Replaces GET /api/ip-camera/status. Cheap in-memory read; shared by CameraPanel and
    StatusPanel so opening either (or both) costs only one timer."""
    cam = hub.ip_camera
    return cam.get_status() if cam else {"connected": False}


def depth_section(hub):
    """Placeholder per PARITY.md §3 — nothing ever writes hub's "depth" data.json section."""
    return logic.data_handler.get_section("depth")


def lights_pct(hub):
    ctrl = hub.controller
    return round(ctrl.get_light() * 100) if ctrl else 0


def aruco_snapshot(hub):
    logger = hub.aruco_logger
    return logger.snapshot() if logger else None


class CameraPanel(PanelBase):
    """Live IP camera feed: connection badge, Reconnect and Fit/Fill toggle.

    Read-only from the operator's point of view (Reconnect just nudges an already-reconnecting
    receiver), so it is safe to duplicate into more than one dock.
    """

    title = "Camera Feed"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._loaded_once = False
        self._fit_contain = False

        box = QGroupBox("Camera Feed")
        # Shared with the IP camera and Camera 1 screens. Takes a callable, not the receiver
        # itself, because reassign_ip_camera swaps hub.ip_camera outright.
        self._camera = CameraWidget(lambda: self.hub.ip_camera, min_size=(480, 270))
        self._camera.problem.connect(self.notify)
        self._camera.frame_changed.connect(self._on_first_frame)

        self._badge = QLabel("CONNECTING")
        self._badge.setStyleSheet(_BADGE_STYLES["neutral"])

        self._btn_reconnect = QPushButton("Reconnect")
        self._btn_reconnect.clicked.connect(self._on_reconnect_clicked)
        self._btn_fit = QPushButton("Fit")
        self._btn_fit.clicked.connect(self._on_fit_clicked)

        header = QHBoxLayout()
        header.addWidget(self._badge)
        header.addStretch(1)
        header.addWidget(self._btn_reconnect)
        header.addWidget(self._btn_fit)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._camera, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_widget)
        layout.addWidget(box, 1)

        self.watch(
            "ip_camera.status", lambda: camera_status(self.hub), CAMERA_STATUS_INTERVAL_MS, self._on_camera_status
        )

    def _on_first_frame(self):
        """The badge distinguishes "connected" from "actually painting frames"."""
        if not self._loaded_once:
            self._loaded_once = True
            self._set_badge(True)

    def _on_camera_status(self, status):
        self._set_badge(bool(status.get("connected")))

    def _set_badge(self, connected):
        if connected:
            if self._loaded_once:
                self._badge.setText("LIVE")
                self._badge.setStyleSheet(_BADGE_STYLES["good"])
            else:
                self._badge.setText("WAITING FOR FIRST FRAME")
                self._badge.setStyleSheet(_BADGE_STYLES["warn"])
        else:
            self._badge.setText("CAMERA OFFLINE")
            self._badge.setStyleSheet(_BADGE_STYLES["bad"])

    def _on_fit_clicked(self):
        self._fit_contain = not self._fit_contain
        self._btn_fit.setText("Fill" if self._fit_contain else "Fit")
        self._camera.set_aspect_mode(Qt.KeepAspectRatio if self._fit_contain else Qt.KeepAspectRatioByExpanding)

    def _on_reconnect_clicked(self):
        ip = self.hub.ip_camera_active_ip or logic.DEFAULT_IP_CAMERA_IP
        self._btn_reconnect.setEnabled(False)
        self._badge.setText("RECONNECTING")
        self._badge.setStyleSheet(_BADGE_STYLES["warn"])
        self.hub.call_async(
            lambda: self.hub.reassign_ip_camera(ip),
            on_done=self._on_reconnect_done,
            on_error=self._on_reconnect_error,
        )

    def _on_reconnect_done(self, _result):
        self._btn_reconnect.setEnabled(True)
        self._loaded_once = False
        self._set_badge(False)

    def _on_reconnect_error(self, message):
        self._btn_reconnect.setEnabled(True)
        self.notify(f"Camera reconnect failed: {message}")

    def on_activate(self):
        self._camera.start()

    def on_deactivate(self):
        self._camera.stop()


class ArucoPanel(PanelBase):
    """ARUCO marker log: badge, start/stop toggle, clear, visible-IDs readout, order log."""

    title = "ARUCO"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._enabled = False

        box = QGroupBox("ARUCO")
        self._badge = QLabel("OFF")
        self._badge.setStyleSheet(_BADGE_STYLES["neutral"])
        self._btn_toggle = QPushButton("Start")
        self._btn_toggle.clicked.connect(self._on_aruco_toggle)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.clicked.connect(self._on_aruco_clear)

        header = QHBoxLayout()
        header.addWidget(self._badge)
        header.addStretch(1)
        header.addWidget(self._btn_toggle)
        header.addWidget(self._btn_clear)

        self._visible = QLabel("--")
        self._log = QListWidget()

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(QLabel("Visible"))
        outer.addWidget(self._visible)
        outer.addWidget(QLabel("Order"))
        outer.addWidget(self._log, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)

        self.watch("aruco.log", lambda: aruco_snapshot(self.hub), ARUCO_INTERVAL_MS, self._on_aruco)

    def _on_aruco(self, log):
        self._render_aruco(log)

    def _render_aruco(self, log):
        log = log or {}
        self._enabled = bool(log.get("enabled"))
        self._badge.setText("ON" if self._enabled else "OFF")
        self._badge.setStyleSheet(_BADGE_STYLES["good" if self._enabled else "neutral"])
        self._btn_toggle.setText("Stop" if self._enabled else "Start")

        visible_ids = log.get("visible_ids") or []
        self._visible.setText(", ".join(str(i) for i in visible_ids) if visible_ids else "--")

        self._log.clear()
        for entry in log.get("entries") or []:
            self._log.addItem(f"ID {entry.get('id')}")

    def _on_aruco_toggle(self):
        logger = self.hub.aruco_logger
        if logger is None:
            return
        log = logger.stop() if self._enabled else logger.start()
        self._render_aruco(log)

    def _on_aruco_clear(self):
        logger = self.hub.aruco_logger
        if logger is None:
            return
        self._render_aruco(logger.clear())


class DepthPanel(PanelBase):
    """Depth + target readout. Placeholder per PARITY.md §3 — nothing writes this section."""

    title = "Depth"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        box = QGroupBox("Depth")
        self._value = QLabel("--.-")
        self._target = QLabel("--.-")
        grid = QGridLayout()
        grid.addWidget(QLabel("Depth (m)"), 0, 0)
        grid.addWidget(QLabel("Target (m)"), 0, 1)
        grid.addWidget(self._value, 1, 0)
        grid.addWidget(self._target, 1, 1)
        outer = QVBoxLayout(box)
        outer.addLayout(grid)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)

        self.watch("depth.section", lambda: depth_section(self.hub), DEPTH_INTERVAL_MS, self._on_depth)

    def _on_depth(self, data):
        data = data or {}
        dpt = data.get("dpt")
        dpt_set = data.get("dptSet")
        self._value.setText("--.-" if dpt is None else f"{float(dpt):.1f}")
        self._target.setText("--.-" if dpt_set is None else f"{float(dpt_set):.1f}")


class LightsPanel(PanelBase):
    """Lights level slider — polls the controller's current level and writes to it.

    WRITES to the vehicle via `hub.controller.set_light()`; keep single-instance.
    """

    title = "Lights"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._dragging = False

        box = QGroupBox("Lights")
        self._value = QLabel("0%")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 80)
        self._slider.setValue(0)
        self._slider.sliderPressed.connect(self._on_pressed)
        self._slider.valueChanged.connect(self._on_changed)
        self._slider.sliderReleased.connect(self._on_released)

        header = QHBoxLayout()
        header.addWidget(QLabel("Level:"))
        header.addWidget(self._value)
        header.addStretch(1)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._slider)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)

        self._post_timer = QTimer(self)
        self._post_timer.setSingleShot(True)
        self._post_timer.setInterval(LIGHT_POST_THROTTLE_MS)

        self.watch("lights.pct", lambda: lights_pct(self.hub), LIGHTS_INTERVAL_MS, self._on_lights)

    def _on_lights(self, pct):
        self._value.setText(f"{pct}%")
        if not self._dragging:
            self._slider.blockSignals(True)
            self._slider.setValue(int(pct))
            self._slider.blockSignals(False)

    def _on_pressed(self):
        self._dragging = True

    def _on_released(self):
        self._dragging = False
        self._send(self._slider.value())

    def _on_changed(self, value):
        self._value.setText(f"{value}%")
        self._queue_send(value)

    def _queue_send(self, value):
        if self._post_timer.isActive():
            return
        self._post_timer.start()
        self._send(value)

    def _send(self, value):
        ctrl = self.hub.controller
        if ctrl is None:
            return
        pct = logic.clamp(float(value), 0.0, 100.0)
        ctrl.set_light(pct / 100.0)


class ManipulatorPanel(PanelBase):
    """Thin dockable wrapper around the shared `ManipulatorWidget` (also used by tooling.py).

    WRITES to the vehicle via the slider inside `ManipulatorWidget`; keep single-instance.
    """

    title = "Manipulator"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._manipulator = ManipulatorWidget(hub)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._manipulator)

        self.watch(
            "manipulator.state", self.hub.manipulator_payload, MANIPULATOR_INTERVAL_MS, self._manipulator.refresh
        )


class StatusPanel(PanelBase):
    """Camera-link dot, static system-OK dot, and a mission clock that restarts on activation."""

    title = "Status"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        box = QGroupBox("Status")
        self._cam_dot = QLabel("CAM: --")
        self._cam_dot.setStyleSheet(_BADGE_STYLES["neutral"])
        # hud-conn-dot is never touched by pilot.js — always a static green dot. Ported as-is.
        self._sys_dot = QLabel("SYS: OK")
        self._sys_dot.setStyleSheet(_BADGE_STYLES["good"])
        self._mission_label = QLabel("00:00:00")

        outer = QVBoxLayout(box)
        outer.addWidget(self._cam_dot)
        outer.addWidget(self._sys_dot)
        outer.addWidget(self._mission_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box)

        self._mission_timer = QTimer(self)
        self._mission_timer.setInterval(MISSION_TIMER_MS)
        self._mission_timer.timeout.connect(self._update_mission_time)
        self._mission_start = time.monotonic()

        self.watch(
            "ip_camera.status", lambda: camera_status(self.hub), CAMERA_STATUS_INTERVAL_MS, self._on_camera_status
        )

    def _on_camera_status(self, status):
        connected = bool(status.get("connected"))
        self._cam_dot.setStyleSheet(_BADGE_STYLES["good" if connected else "bad"])
        self._cam_dot.setText("CAM: OK" if connected else "CAM: OFFLINE")

    def _update_mission_time(self):
        elapsed = int(time.monotonic() - self._mission_start)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self._mission_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def on_activate(self):
        """Mission clock restarts each time this panel is shown, mirroring a fresh page load."""
        self._mission_start = time.monotonic()
        self._update_mission_time()
        self._mission_timer.start()

    def on_deactivate(self):
        self._mission_timer.stop()


class PilotScreen(ScreenBase):
    title = "Pilot"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        self._camera_panel = CameraPanel(hub)
        self._aruco_panel = ArucoPanel(hub)
        self._depth_panel = DepthPanel(hub)
        self._lights_panel = LightsPanel(hub)
        self._manipulator_panel = ManipulatorPanel(hub)
        self._status_panel = StatusPanel(hub)

        top_row = QHBoxLayout()
        top_row.addWidget(self._camera_panel, 2)
        top_row.addWidget(self._aruco_panel, 1)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._depth_panel)
        bottom_row.addWidget(self._lights_panel)
        bottom_row.addWidget(self._manipulator_panel)
        bottom_row.addWidget(self._status_panel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(top_row, 1)
        layout.addLayout(bottom_row)

        # Back-compat attribute aliases — tests (and any other external code) reach into these
        # names directly (e.g. test_desktop_screens.py's screen._aruco_badge / _btn_aruco_toggle /
        # _aruco_log / _on_aruco_toggle). No watch() of its own: each panel polls itself, and
        # PanelBase.set_active cascades activation down to them, same as GraphsScreen.
        self._camera = self._camera_panel._camera
        self._camera_badge = self._camera_panel._badge
        self._btn_reconnect = self._camera_panel._btn_reconnect
        self._btn_fit = self._camera_panel._btn_fit

        self._aruco_badge = self._aruco_panel._badge
        self._btn_aruco_toggle = self._aruco_panel._btn_toggle
        self._btn_aruco_clear = self._aruco_panel._btn_clear
        self._aruco_visible = self._aruco_panel._visible
        self._aruco_log = self._aruco_panel._log
        self._on_aruco_toggle = self._aruco_panel._on_aruco_toggle
        self._on_aruco_clear = self._aruco_panel._on_aruco_clear
        self._render_aruco = self._aruco_panel._render_aruco

        self._depth_value = self._depth_panel._value
        self._depth_target = self._depth_panel._target

        self._light_slider = self._lights_panel._slider
        self._light_value = self._lights_panel._value

        self._manipulator = self._manipulator_panel._manipulator

        self._cam_dot = self._status_panel._cam_dot
        self._sys_dot = self._status_panel._sys_dot
        self._mission_label = self._status_panel._mission_label

    def _aruco_fn(self):
        """Back-compat: some tests construct PilotScreen via __new__ (bypassing __init__ and
        thus ArucoPanel) and call this directly, so it must stay a real method on the class
        rather than an instance alias set in __init__."""
        return aruco_snapshot(self.hub)


#: Category "Pilot" — each panel is independently placeable. Camera/ARUCO/Depth/Status are
#: read-only displays, so duplicating them into more than one dock is harmless. Lights and the
#: Manipulator WRITE to the vehicle (invariant: only Controller writes thruster/light/manipulator
#: commands), so two live copies would race on the wire — kept single-instance.
COMPONENTS = [
    Component(
        id="panel.pilot.camera", title="Camera Feed", factory=CameraPanel, category="Pilot", duplicable=True, order=0
    ),
    Component(id="panel.pilot.aruco", title="ARUCO", factory=ArucoPanel, category="Pilot", duplicable=True, order=1),
    Component(id="panel.pilot.depth", title="Depth", factory=DepthPanel, category="Pilot", duplicable=True, order=2),
    Component(
        id="panel.pilot.lights", title="Lights", factory=LightsPanel, category="Pilot", duplicable=False, order=3
    ),
    Component(
        id="panel.pilot.manipulator",
        title="Manipulator",
        factory=ManipulatorPanel,
        category="Pilot",
        duplicable=False,
        order=4,
    ),
    Component(id="panel.pilot.status", title="Status", factory=StatusPanel, category="Pilot", duplicable=True, order=5),
]
