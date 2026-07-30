"""Connection screen — ports `/connection` (`connection.html` + `connection.js`).

Renders the three-proof Nucleo contact check (`hub.connection_proof()`, thresholds 2500/2500/
1200 ms, already implemented verbatim in ServiceHub) and the MCU restart control.

`connection.js` gates the restart button behind `window.confirm("Restart the MCU now?")`. That
gate is kept — restarting the MCU mid-dive is destructive and must not be one stray click away.
What it must NOT be is a modal: `QMessageBox` blocks the Qt event loop, stalling every poller and
the 20 Hz command loop behind it. So the confirmation is two-step and non-blocking — the first
click arms the button, a second click within ARM_TIMEOUT_MS sends, and the arm lapses on its own.
`SystemControlClient.send_reset()` blocks up to ~0.15 s, so it goes through `hub.call_async`.
"""

import json

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from desktop.screens.base import ScreenBase

POLL_INTERVAL_MS = 1000
RESET_STATUS_RESET_MS = 3000
#: How long the restart button stays armed awaiting a second click.
ARM_TIMEOUT_MS = 5000

_BADGE_STYLES = {
    "secondary": "color: palette(text);",
    "success": "color: #3fb950; font-weight: 600;",
    "warning": "color: #d29922; font-weight: 600;",
    "danger": "color: #f85149; font-weight: 600;",
}


class ConnectionScreen(ScreenBase):
    title = "Connection"

    def __init__(self, hub, parent=None):
        super().__init__(hub, parent)

        layout = QVBoxLayout(self)
        layout.addWidget(self.notice_widget)
        layout.addWidget(self._build_proof_box())

        row = QHBoxLayout()
        row.addWidget(self._build_reset_box())
        row.addWidget(self._build_testpy_box())
        layout.addLayout(row)
        layout.addStretch(1)

        self.watch("connection.proof", self.hub.connection_proof, POLL_INTERVAL_MS, self._on_proof)

    # --- Nucleo contact proof -------------------------------------------------

    def _build_proof_box(self):
        box = QGroupBox("Nucleo Contact Proof")
        self._contact_badge = QLabel("WAITING")
        self._contact_badge.setStyleSheet(_BADGE_STYLES["secondary"])

        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self._contact_badge)

        self._best_proof = QLabel("--")
        self._last_ack = QLabel("--")
        self._imu_age = QLabel("--")

        stats = QGridLayout()
        stats.addWidget(QLabel("Best Proof Age"), 0, 0)
        stats.addWidget(QLabel("Command Ack"), 0, 1)
        stats.addWidget(QLabel("IMU Telemetry"), 0, 2)
        stats.addWidget(self._best_proof, 1, 0)
        stats.addWidget(self._last_ack, 1, 1)
        stats.addWidget(self._imu_age, 1, 2)

        self._proof_table = QTableWidget(0, 4)
        self._proof_table.setHorizontalHeaderLabels(["Proof Source", "Status", "Age", "Detail"])
        self._proof_table.verticalHeader().setVisible(False)
        self._proof_table.setEditTriggers(QTableWidget.NoEditTriggers)

        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        self._details.setPlainText("Loading...")

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addLayout(stats)
        outer.addWidget(self._proof_table)
        outer.addWidget(self._details)
        return box

    def _on_proof(self, status):
        proofs = status.get("proofs") or []
        best_age = self._min_active_age(proofs)

        self._set_contact_badge(status.get("connected") is True, best_age)
        self._render_proofs(proofs)

        self._best_proof.setText("--" if best_age is None else f"{round(best_age)} ms")

        uplink = status.get("uplink") or {}
        ack_age = uplink.get("last_ack_age_ms")
        self._last_ack.setText("--" if ack_age is None else f"{round(ack_age)} ms")

        imu = status.get("imu") or {}
        imu_age = imu.get("age_ms")
        self._imu_age.setText("--" if imu_age is None else f"{round(imu_age)} ms")

        self._details.setPlainText(
            json.dumps({"uplink": uplink, "resource": status.get("resource"), "imu": imu}, indent=2)
        )

    @staticmethod
    def _min_active_age(proofs):
        ages = [p["age_ms"] for p in proofs if p.get("active") and p.get("age_ms") is not None]
        return min(ages) if ages else None

    def _set_contact_badge(self, connected, best_age_ms):
        badge = self._contact_badge
        if not connected:
            badge.setText("NO LIVE PROOF")
            badge.setStyleSheet(_BADGE_STYLES["secondary"])
        elif best_age_ms is not None and best_age_ms < 1000:
            badge.setText("LIVE")
            badge.setStyleSheet(_BADGE_STYLES["success"])
        elif best_age_ms is not None and best_age_ms < 3000:
            badge.setText("STALE")
            badge.setStyleSheet(_BADGE_STYLES["warning"])
        else:
            badge.setText("LIVE, SLOW")
            badge.setStyleSheet(_BADGE_STYLES["warning"])

    def _render_proofs(self, proofs):
        table = self._proof_table
        table.setRowCount(len(proofs))
        for row, proof in enumerate(proofs):
            table.setItem(row, 0, QTableWidgetItem(str(proof.get("name") or "--")))
            status_item = QTableWidgetItem("LIVE" if proof.get("active") else "STALE")
            table.setItem(row, 1, status_item)
            age = proof.get("age_ms")
            table.setItem(row, 2, QTableWidgetItem("--" if age is None else f"{round(age)} ms"))
            table.setItem(row, 3, QTableWidgetItem(str(proof.get("detail") or "")))

    # --- Restart MCU ------------------------------------------------------------

    def _build_reset_box(self):
        box = QGroupBox("Restart MCU")
        self._reset_badge = QLabel("READY")
        self._reset_badge.setStyleSheet(_BADGE_STYLES["secondary"])

        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self._reset_badge)

        self._armed = False
        self._btn_reset = QPushButton("Restart MCU")
        self._btn_reset.clicked.connect(self._on_reset_clicked)

        outer = QVBoxLayout(box)
        outer.addLayout(header)
        outer.addWidget(self._btn_reset)
        outer.addStretch(1)

        self._reset_reenable_timer = QTimer(self)
        self._reset_reenable_timer.setSingleShot(True)
        self._reset_reenable_timer.timeout.connect(self._reset_ready)

        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.timeout.connect(self._disarm)
        return box

    def _on_reset_clicked(self):
        """First click arms, second click sends — the non-blocking form of window.confirm()."""
        if self._armed:
            self._disarm()
            self._send_reset()
            return
        self._armed = True
        self._btn_reset.setText("Confirm restart?")
        self._reset_badge.setText("CONFIRM")
        self._reset_badge.setStyleSheet(_BADGE_STYLES["warning"])
        self._arm_timer.start(ARM_TIMEOUT_MS)

    def _disarm(self):
        self._armed = False
        self._arm_timer.stop()
        self._btn_reset.setText("Restart MCU")
        if self._reset_badge.text() == "CONFIRM":
            self._reset_badge.setText("READY")
            self._reset_badge.setStyleSheet(_BADGE_STYLES["secondary"])

    def _send_reset(self):
        client = self.hub.system_control
        if client is None:
            self.notify("System control client unavailable.")
            return
        self._btn_reset.setEnabled(False)
        self._reset_badge.setText("SENDING")
        self._reset_badge.setStyleSheet(_BADGE_STYLES["warning"])
        self.hub.call_async(client.send_reset, on_done=self._on_reset_done, on_error=self._on_reset_error)

    def _on_reset_done(self, _result):
        self._reset_badge.setText("RESET SENT")
        self._reset_badge.setStyleSheet(_BADGE_STYLES["success"])
        self._reset_reenable_timer.start(RESET_STATUS_RESET_MS)

    def _on_reset_error(self, message):
        self._reset_badge.setText("FAILED")
        self._reset_badge.setStyleSheet(_BADGE_STYLES["danger"])
        self.notify(f"MCU restart failed: {message}")
        self._reset_reenable_timer.start(RESET_STATUS_RESET_MS)

    def _reset_ready(self):
        self._reset_badge.setText("READY")
        self._reset_badge.setStyleSheet(_BADGE_STYLES["secondary"])
        self._btn_reset.setEnabled(True)

    def on_deactivate(self):
        """Never leave the restart armed for whoever opens this tab next."""
        self._disarm()

    # --- test.py activation (dead placeholder, ported as-is) -----------------------

    def _build_testpy_box(self):
        box = QGroupBox("test.py Activation")
        btn = QPushButton("TODO")
        btn.setEnabled(False)
        outer = QVBoxLayout(box)
        outer.addWidget(btn)
        outer.addStretch(1)
        return box
