"""CameraWidget — reusable Qt video widget replacing the MJPEG-over-HTTP feeds.

Flask served frames via `generate_*_frames()` in `lib/camera.py`: a JPEG multipart generator
consumed by an `<img>` tag over HTTP. In-process that hop is pointless — the receiver already
keeps the latest encoded JPEG in memory (`get_latest_jpeg_and_seq()`), so this widget reads it
directly and paints it into a `QLabel` via `QPixmap`. `generate_*_frames()` stay in `lib/camera.py`
unused; deleting them is out of scope for this port (PARITY.md §3).

Why a receiver *callable*, not a receiver reference
----------------------------------------------------
`ServiceHub.reassign_ip_camera()` calls `.stop()` on the old `IPCameraReceiver` and assigns a brand
new one to `hub.ip_camera` — the object identity changes. A widget that captured the receiver at
construction time would keep drawing from (and blocking on) a stopped receiver forever. So the
constructor takes `receiver_fn: Callable[[], receiver_or_None]` and calls it on every poll tick,
exactly like `ip_camera.py::_frame_fn` re-reading `self.hub.ip_camera` each time instead of caching
it.

Why a sequence-compare timer, not `wait_for_next_frame()`
----------------------------------------------------------
`wait_for_next_frame(last_seq, timeout=0.25)` is the receiver's real backpressure primitive — it
blocks on a `threading.Condition` until a new frame lands or the timeout elapses. Using it from the
GUI thread is out; routing it through `hub.call_async` would work but means keeping a worker
thread permanently blocked in a wait, re-queuing itself every ~0.25 s indefinitely for the whole
life of the widget, with no clean way to cancel a call that's mid-wait when the screen is hidden
(the thread pool has no cooperative cancellation) other than waiting out the timeout. A plain timer
that reads `get_latest_jpeg_and_seq()` and compares the sequence number is non-blocking, trivially
start/stop-able from `showEvent`/`hideEvent`, and only repaints on an actual new frame — the same
tradeoff `ip_camera.py` already made for its inline poller. This widget generalises that pattern.

Scaling and placeholder behaviour
----------------------------------
Frames are scaled into the label with `Qt.KeepAspectRatio` (unlike `ip_camera.py`'s
`setScaledContents(True)`, which stretches to fill and distorts the image). When the receiver is
`None`, or has no frame yet and offers no `get_placeholder_jpeg()`, a built-in "NO SIGNAL" pixmap is
shown so the screen never looks broken or blank.

`set_aspect_mode()` switches between letterbox (`Qt.KeepAspectRatio`) and cover
(`Qt.KeepAspectRatioByExpanding`) — that is the pilot HUD's Fit/Fill toggle, which is the CSS
`object-fit: contain | cover` the Jinja template used.

Polling lifecycle
------------------
The widget owns its `QTimer` but does not start it on construction. Call `start()` from the owning
screen's `on_activate()` (or `showEvent`) and `stop()` from `on_deactivate()` (or `hideEvent`) —
`ScreenBase` already ties those hooks to tab visibility, so decoding JPEGs for a hidden tab never
happens, matching the requirement that camera polling stop when the screen isn't visible.
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

DEFAULT_INTERVAL_MS = 100
DEFAULT_MIN_SIZE = (320, 180)

_NO_SIGNAL_TEXT = "NO SIGNAL"


def _build_no_signal_pixmap(width=640, height=360):
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.black)
    painter = QPainter(pixmap)
    painter.setPen(Qt.white)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, _NO_SIGNAL_TEXT)
    painter.end()
    return pixmap


class CameraWidget(QWidget):
    """Paints a receiver's latest JPEG into a label, polling on a local timer.

    `receiver_fn` is called on every tick and must return a `lib/camera.py` receiver instance
    (any of `DefaultCameraReceiver`, `RPiCameraReceiver`, `IPCameraReceiver`) or `None`. Nothing
    here touches the network or blocks — every receiver method used
    (`get_latest_jpeg_and_seq()`, `get_placeholder_jpeg()`) is an in-memory read guarded by a lock.
    """

    #: Emitted whenever a new frame or placeholder is painted. Mainly for tests/observability.
    frame_changed = Signal()

    #: Emitted when a JPEG fails to decode. Screens may route this to `self.notify(...)`.
    problem = Signal(str)

    def __init__(self, receiver_fn, interval_ms=DEFAULT_INTERVAL_MS, min_size=DEFAULT_MIN_SIZE, parent=None):
        super().__init__(parent)
        self._receiver_fn = receiver_fn

        # Sentinel states for `_last_seq`: "none" means the no-signal placeholder is showing;
        # "placeholder" means a receiver-provided placeholder JPEG is showing (repainted only
        # once, not on every tick, until real frames start arriving with integer sequence
        # numbers). Any other value is the last real frame sequence number painted.
        self._last_seq = "none"
        self._source_pixmap = None
        self._aspect_mode = Qt.KeepAspectRatio
        self._no_signal_pixmap = _build_no_signal_pixmap(*_scaled_placeholder_size(min_size))

        self.feed_label = QLabel()
        self.feed_label.setAlignment(Qt.AlignCenter)
        self.feed_label.setMinimumSize(*min_size)
        self.feed_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.feed_label.setStyleSheet("background: black; color: white;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.feed_label)

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

        self._show_pixmap(self._no_signal_pixmap)

    # --- lifecycle ------------------------------------------------------------

    def start(self):
        """Begin polling. Call from the owning screen's `on_activate()`/`showEvent`."""
        self._last_seq = "none"
        self._poll()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        """Stop polling. Call from the owning screen's `on_deactivate()`/`hideEvent` — decoding
        JPEGs for an invisible tab is exactly what `ScreenBase` visibility handling prevents."""
        self._timer.stop()

    # --- polling ----------------------------------------------------------------

    def _poll(self):
        cam = self._receiver_fn()
        if cam is None:
            self._set_no_signal()
            return

        jpeg, seq = cam.get_latest_jpeg_and_seq()
        if jpeg is None:
            get_placeholder = getattr(cam, "get_placeholder_jpeg", None)
            placeholder = get_placeholder() if get_placeholder else None
            if placeholder is None:
                self._set_no_signal()
                return
            jpeg = placeholder
            seq = "placeholder"

        if seq == self._last_seq:
            return
        self._last_seq = seq

        pixmap = QPixmap()
        if not pixmap.loadFromData(jpeg, "JPG"):
            self.problem.emit("Received camera frame could not be decoded.")
            return
        self._source_pixmap = pixmap
        self._apply_scaled_pixmap()
        self.frame_changed.emit()

    def _set_no_signal(self):
        if self._last_seq == "none":
            return
        self._last_seq = "none"
        self._show_pixmap(self._no_signal_pixmap)
        self.frame_changed.emit()

    # --- painting -----------------------------------------------------------------

    def _show_pixmap(self, pixmap):
        self._source_pixmap = pixmap
        self._apply_scaled_pixmap()

    def set_aspect_mode(self, mode):
        """Letterbox (`Qt.KeepAspectRatio`) or cover (`Qt.KeepAspectRatioByExpanding`)."""
        self._aspect_mode = mode
        self._apply_scaled_pixmap()

    def _apply_scaled_pixmap(self):
        pixmap = self._source_pixmap
        if pixmap is None or pixmap.isNull():
            return
        size = self.feed_label.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        scaled = pixmap.scaled(size, self._aspect_mode, Qt.SmoothTransformation)
        self.feed_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_scaled_pixmap()


def _scaled_placeholder_size(min_size):
    width, height = min_size
    return max(width, 320), max(height, 180)
