"""Pilot screen — ports `/pilot` (`pilot.html` + `pilot.js`, 269 lines).

A full-screen HUD: live IP camera feed, depth readout, ARUCO marker log, a lights slider, and the
shared `ManipulatorWidget` (see `desktop/widgets/manipulator.py` — `manipulator.js` is loaded by
both this screen and `screens/tooling.py`; it is not duplicated here).

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

from desktop import logic
from desktop.screens.base import ScreenBase
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

_BADGE_STYLES = {
    "neutral": "color: palette(text);",
    "good": "color: #3fb950; font-weight: 600;",
    "warn": "color: #d29922; font-weight: 600;",
    "bad": "color: #f85149; font-weight: 600;",
}


class PilotScreen(ScreenBase):
    title = "Pilot"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)
        self._loaded_once = False
        self._fit_contain = False
        self._light_dragging = False
        self._aruco_enabled = False

        top_row = QHBoxLayout()
        top_row.addWidget(self._build_camera_box(), 2)
        top_row.addWidget(self._build_aruco_box(), 1)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._build_depth_box())
        bottom_row.addWidget(self._build_lights_box())
        self._manipulator = ManipulatorWidget(hub)
        bottom_row.addWidget(self._manipulator)
        bottom_row.addWidget(self._build_status_box())

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(top_row, 1)
        layout.addLayout(bottom_row)

        self._light_post_timer = QTimer(self)
        self._light_post_timer.setSingleShot(True)
        self._light_post_timer.setInterval(LIGHT_POST_THROTTLE_MS)

        self._mission_timer = QTimer(self)
        self._mission_timer.setInterval(MISSION_TIMER_MS)
        self._mission_timer.timeout.connect(self._update_mission_time)

        self.watch("ip_camera.status", self._camera_status_fn, CAMERA_STATUS_INTERVAL_MS, self._on_camera_status)
        self.watch("depth.section", self._depth_fn, DEPTH_INTERVAL_MS, self._on_depth)
        self.watch("lights.pct", self._lights_fn, LIGHTS_INTERVAL_MS, self._on_lights)
        self.watch("aruco.log", self._aruco_fn, ARUCO_INTERVAL_MS, self._on_aruco)
        self.watch(
            "manipulator.state", self.hub.manipulator_payload, MANIPULATOR_INTERVAL_MS, self._manipulator.refresh
        )

    # --- camera feed ------------------------------------------------------------

    def _build_camera_box(self):
        box = QGroupBox("Camera Feed")
        # Shared with the IP camera and Camera 1 screens. Takes a callable, not the receiver
        # itself, because reassign_ip_camera swaps hub.ip_camera outright.
        self._camera = CameraWidget(lambda: self.hub.ip_camera, min_size=(480, 270))
        self._camera.problem.connect(self.notify)
        self._camera.frame_changed.connect(self._on_first_frame)

        self._camera_badge = QLabel("CONNECTING")
        self._camera_badge.setStyleSheet(_BADGE_STYLES["neutral"])

        self._btn_reconnect = QPushButton("Reconnect")
        self._btn_reconnect.clicked.connect(self._on_reconnect_clicked)
        self._btn_fit = QPushButton("Fit")
        self._btn_fit.clicked.connect(self._on_fit_clicked)

        header = QHBoxLayout()
        header.addWidget(self._camera_badge)
        header.addStretch(1)
        header.addWidget(self._btn_reconnect)
        header.addWidget(self._btn_fit)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._camera, 1)
        return box

    def _on_first_frame(self):
        """The badge distinguishes "connected" from "actually painting frames"."""
        if not self._loaded_once:
            self._loaded_once = True
            self._set_camera_badge(True)

    def _camera_status_fn(self):
        cam = self.hub.ip_camera
        return cam.get_status() if cam else {"connected": False}

    def _on_camera_status(self, status):
        connected = bool(status.get("connected"))
        self._set_camera_badge(connected)
        self._cam_dot.setStyleSheet(_BADGE_STYLES["good" if connected else "bad"])
        self._cam_dot.setText("CAM: OK" if connected else "CAM: OFFLINE")

    def _set_camera_badge(self, connected):
        if connected:
            if self._loaded_once:
                self._camera_badge.setText("LIVE")
                self._camera_badge.setStyleSheet(_BADGE_STYLES["good"])
            else:
                self._camera_badge.setText("WAITING FOR FIRST FRAME")
                self._camera_badge.setStyleSheet(_BADGE_STYLES["warn"])
        else:
            self._camera_badge.setText("CAMERA OFFLINE")
            self._camera_badge.setStyleSheet(_BADGE_STYLES["bad"])

    def _on_fit_clicked(self):
        self._fit_contain = not self._fit_contain
        self._btn_fit.setText("Fill" if self._fit_contain else "Fit")
        self._camera.set_aspect_mode(Qt.KeepAspectRatio if self._fit_contain else Qt.KeepAspectRatioByExpanding)

    def _on_reconnect_clicked(self):
        ip = self.hub.ip_camera_active_ip or logic.DEFAULT_IP_CAMERA_IP
        self._btn_reconnect.setEnabled(False)
        self._camera_badge.setText("RECONNECTING")
        self._camera_badge.setStyleSheet(_BADGE_STYLES["warn"])
        self.hub.call_async(
            lambda: self.hub.reassign_ip_camera(ip),
            on_done=self._on_reconnect_done,
            on_error=self._on_reconnect_error,
        )

    def _on_reconnect_done(self, _result):
        self._btn_reconnect.setEnabled(True)
        self._loaded_once = False
        self._set_camera_badge(False)

    def _on_reconnect_error(self, message):
        self._btn_reconnect.setEnabled(True)
        self.notify(f"Camera reconnect failed: {message}")

    # --- depth (placeholder — nothing ever writes this section) ----------------

    def _build_depth_box(self):
        box = QGroupBox("Depth")
        self._depth_value = QLabel("--.-")
        self._depth_target = QLabel("--.-")
        grid = QGridLayout()
        grid.addWidget(QLabel("Depth (m)"), 0, 0)
        grid.addWidget(QLabel("Target (m)"), 0, 1)
        grid.addWidget(self._depth_value, 1, 0)
        grid.addWidget(self._depth_target, 1, 1)
        outer = QVBoxLayout(box)
        outer.addLayout(grid)
        return box

    def _depth_fn(self):
        return logic.data_handler.get_section("depth")

    def _on_depth(self, data):
        data = data or {}
        dpt = data.get("dpt")
        dpt_set = data.get("dptSet")
        self._depth_value.setText("--.-" if dpt is None else f"{float(dpt):.1f}")
        self._depth_target.setText("--.-" if dpt_set is None else f"{float(dpt_set):.1f}")

    # --- lights -------------------------------------------------------------------

    def _build_lights_box(self):
        box = QGroupBox("Lights")
        self._light_value = QLabel("0%")
        self._light_slider = QSlider(Qt.Horizontal)
        self._light_slider.setRange(0, 80)
        self._light_slider.setValue(0)
        self._light_slider.sliderPressed.connect(self._on_light_pressed)
        self._light_slider.valueChanged.connect(self._on_light_changed)
        self._light_slider.sliderReleased.connect(self._on_light_released)

        header = QHBoxLayout()
        header.addWidget(QLabel("Level:"))
        header.addWidget(self._light_value)
        header.addStretch(1)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._light_slider)
        return box

    def _lights_fn(self):
        ctrl = self.hub.controller
        return round(ctrl.get_light() * 100) if ctrl else 0

    def _on_lights(self, pct):
        self._light_value.setText(f"{pct}%")
        if not self._light_dragging:
            self._light_slider.blockSignals(True)
            self._light_slider.setValue(int(pct))
            self._light_slider.blockSignals(False)

    def _on_light_pressed(self):
        self._light_dragging = True

    def _on_light_released(self):
        self._light_dragging = False
        self._send_light(self._light_slider.value())

    def _on_light_changed(self, value):
        self._light_value.setText(f"{value}%")
        self._queue_light_send(value)

    def _queue_light_send(self, value):
        if self._light_post_timer.isActive():
            return
        self._light_post_timer.start()
        self._send_light(value)

    def _send_light(self, value):
        ctrl = self.hub.controller
        if ctrl is None:
            return
        pct = logic.clamp(float(value), 0.0, 100.0)
        ctrl.set_light(pct / 100.0)

    # --- ARUCO ----------------------------------------------------------------

    def _build_aruco_box(self):
        box = QGroupBox("ARUCO")
        self._aruco_badge = QLabel("OFF")
        self._aruco_badge.setStyleSheet(_BADGE_STYLES["neutral"])
        self._btn_aruco_toggle = QPushButton("Start")
        self._btn_aruco_toggle.clicked.connect(self._on_aruco_toggle)
        self._btn_aruco_clear = QPushButton("Clear")
        self._btn_aruco_clear.clicked.connect(self._on_aruco_clear)

        header = QHBoxLayout()
        header.addWidget(self._aruco_badge)
        header.addStretch(1)
        header.addWidget(self._btn_aruco_toggle)
        header.addWidget(self._btn_aruco_clear)

        self._aruco_visible = QLabel("--")
        self._aruco_log = QListWidget()

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(QLabel("Visible"))
        outer.addWidget(self._aruco_visible)
        outer.addWidget(QLabel("Order"))
        outer.addWidget(self._aruco_log, 1)
        return box

    def _aruco_fn(self):
        logger = self.hub.aruco_logger
        return logger.snapshot() if logger else None

    def _on_aruco(self, log):
        self._render_aruco(log)

    def _render_aruco(self, log):
        log = log or {}
        self._aruco_enabled = bool(log.get("enabled"))
        self._aruco_badge.setText("ON" if self._aruco_enabled else "OFF")
        self._aruco_badge.setStyleSheet(_BADGE_STYLES["good" if self._aruco_enabled else "neutral"])
        self._btn_aruco_toggle.setText("Stop" if self._aruco_enabled else "Start")

        visible_ids = log.get("visible_ids") or []
        self._aruco_visible.setText(", ".join(str(i) for i in visible_ids) if visible_ids else "--")

        self._aruco_log.clear()
        for entry in log.get("entries") or []:
            self._aruco_log.addItem(f"ID {entry.get('id')}")

    def _on_aruco_toggle(self):
        logger = self.hub.aruco_logger
        if logger is None:
            return
        log = logger.stop() if self._aruco_enabled else logger.start()
        self._render_aruco(log)

    def _on_aruco_clear(self):
        logger = self.hub.aruco_logger
        if logger is None:
            return
        self._render_aruco(logger.clear())

    # --- status dots + mission timer -------------------------------------------

    def _build_status_box(self):
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
        return box

    def _update_mission_time(self):
        elapsed = int(time.monotonic() - self._mission_start)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self._mission_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    # --- lifecycle ----------------------------------------------------------

    def on_activate(self):
        """Mission clock restarts each time this tab is opened, mirroring a fresh page load."""
        self._mission_start = time.monotonic()
        self._update_mission_time()
        self._mission_timer.start()
        self._camera.start()

    def on_deactivate(self):
        self._mission_timer.stop()
        self._camera.stop()
