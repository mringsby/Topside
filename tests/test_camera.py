import numpy as np

from lib.aruco_logger import ArucoPipelineLogger
from lib.camera import IPCameraReceiver


class FakeArucoDetector:
    def detect_markers(self, frame):
        return [], None, []

    def marker_detections(self, corners, ids):
        return [
            {"id": 3, "center": (300, 100)},
            {"id": 2, "center": (200, 100)},
        ]

    def draw_detected_markers(self, frame, corners, ids):
        return frame


def test_ip_camera_frame_updates_aruco_logger():
    logger = ArucoPipelineLogger()
    logger.start()
    camera = IPCameraReceiver("rtsp://example.invalid/stream", marker_logger=logger)
    camera._detector = FakeArucoDetector()

    camera._set_frame(np.zeros((20, 20, 3), dtype=np.uint8))

    snapshot = logger.snapshot()
    assert [entry["id"] for entry in snapshot["entries"]] == [2, 3]
    assert snapshot["visible_ids"] == [2, 3]
    assert camera.get_latest_jpeg() is not None
