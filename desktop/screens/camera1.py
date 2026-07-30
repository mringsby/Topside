"""Camera 1 screen — ports `/Camera1` (`camera1.html`).

The Flask template was minimal: a heading and a bare `<img src="{{ url_for('video_feed') }}">`
with no companion JS, so there was no client-side status polling for this page. `video_feed`
served `hub.default_camera` (PARITY.md §3), a local capture device — not an ROV camera — so it is
expected to show "no signal" on a machine with no webcam.

The video itself is `widgets/camera.py::CameraWidget`, reading `hub.default_camera` directly
instead of going over HTTP multipart. A small LIVE/OFFLINE badge is added on top of the original
template, polled via `self.watch(...)` from `GET /api/camera/status` -> `hub.default_camera.get_status()`
(also listed in PARITY.md §3) — purely additive status feedback, consistent with how
`ip_camera.py` surfaces the equivalent status for its own camera.
"""

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from desktop.screens.base import ScreenBase
from desktop.widgets.camera import CameraWidget

STATUS_INTERVAL_MS = 3000

_FEEDBACK_STYLES = {
    "neutral": "color: palette(text);",
    "good": "color: #3fb950;",
    "bad": "color: #f85149;",
}


class Camera1Screen(ScreenBase):
    title = "Camera 1"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        heading = QLabel("<h2>Camera 1 Feed</h2>")

        self._state_badge = QLabel("OFFLINE")
        self._state_badge.setStyleSheet(_FEEDBACK_STYLES["neutral"])

        header = QHBoxLayout()
        header.addWidget(heading)
        header.addStretch(1)
        header.addWidget(self._state_badge)

        self._camera = CameraWidget(lambda: self.hub.default_camera, min_size=(480, 270))
        self._camera.problem.connect(self.notify)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addLayout(header)
        layout.addWidget(self._camera, 1)

        self.watch("default_camera.status", self._status_fn, STATUS_INTERVAL_MS, self._on_status)

    # --- status -----------------------------------------------------------------

    def _status_fn(self):
        cam = self.hub.default_camera
        return cam.get_status() if cam is not None else {"connected": False}

    def _on_status(self, status):
        if status and status.get("connected"):
            self._state_badge.setText("LIVE")
            self._state_badge.setStyleSheet(_FEEDBACK_STYLES["good"])
        else:
            self._state_badge.setText("OFFLINE")
            self._state_badge.setStyleSheet(_FEEDBACK_STYLES["bad"])

    # --- lifecycle ----------------------------------------------------------

    def on_activate(self):
        self._camera.start()

    def on_deactivate(self):
        self._camera.stop()
