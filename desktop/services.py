"""ServiceHub — the composition root and the Qt bridge for lib/ services.

Replaces Flask's `app.config`. Where a route did `current_app.config.get("CONTROLLER")` and
returned 503 when absent, a screen does `hub.controller` and disables its widgets when it is None.
That degradation is required: it is what lets screens be built and tested without hardware.

Two mechanisms every screen uses:

  hub.poller(key, fn, interval_ms)   named, shared, reference-counted polling
  hub.call_async(fn, on_done=...)    anything that blocks on the MCU or the network

Screens must never call a blocking lib/ function directly from a slot. See PARITY.md §1.
"""

import os
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from desktop import logic
from lib.aruco_logger import ArucoPipelineLogger
from lib.axis_config_sender import send_axis_config
from lib.bitmask import init_bitmask
from lib.camera import init_ip_camera, init_rpi_camera
from lib.control_telemetry import init_control_telemetry
from lib.controller import Controller
from lib.log_udp_receiver import init_log_stream
from lib.net_transport import DEFAULT_ROV_HOST
from lib.ninedof_receiver import init_imu_receiver
from lib.resource_receiver import init_resource_receiver
from lib.runtime_paths import ensure_data_dir
from lib.setpoint_override import init_setpoint_override
from lib.system_control_client import SystemControlClient


def _env_flag(name, default="false"):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Poller(QObject):
    """Calls a non-blocking getter on a QTimer and fans the result out to subscribers.

    Reference-counted: the timer only runs while at least one screen is subscribed, so a screen
    that is not on top costs nothing. This reproduces the Flask behaviour where a page that was
    not open issued no polls.

    The wrapped callable runs on the GUI thread, so it must be a cheap in-memory getter
    (`get_stats()`, `get_latest()`, `get_status()`). Anything that touches the network goes
    through `ServiceHub.call_async` instead.
    """

    updated = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, interval_ms, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._subscribers = 0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def subscribe(self):
        self._subscribers += 1
        if self._subscribers == 1:
            self._timer.start()
            self._tick()  # deliver immediately instead of waiting a full interval

    def unsubscribe(self):
        self._subscribers = max(0, self._subscribers - 1)
        if self._subscribers == 0:
            self._timer.stop()

    def set_interval(self, interval_ms):
        self._timer.setInterval(interval_ms)

    def _tick(self):
        try:
            result = self._fn()
        except Exception as exc:  # a dead service must not kill the timer
            self.failed.emit(str(exc))
            return
        self.updated.emit(result)


class _CallSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class _AsyncCall(QRunnable):
    def __init__(self, fn, signals):
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:
            traceback.print_exc()
            self._signals.error.emit(str(exc))
            return
        self._signals.done.emit(result)


class ServiceHub(QObject):
    """Owns every lib/ service. Constructed once, passed to every screen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pollers = {}
        self._pool = QThreadPool(self)
        # MCU round-trips are serialised by the protocol anyway; a small pool keeps
        # a slow RTSP reconnect from starving quick calls.
        self._pool.setMaxThreadCount(4)
        self._build()

    # --- construction (mirrors app.py) ---------------------------------------

    def _build(self):
        ensure_data_dir()
        config = logic.config_handler

        self.bitmask = init_bitmask(rate_hz=20.0, host=DEFAULT_ROV_HOST, port=12345)

        self.controller = Controller(bitmask_client=self.bitmask, rate_hz=60.0)
        self.controller.start()

        self.imu = init_imu_receiver(port=5002)

        self.aruco_logger = ArucoPipelineLogger()

        saved_axes = config.get_section("imu_axes")
        if saved_axes:
            self.imu.set_axis_mapping(saved_axes)
        saved_accel_axes = config.get_section("accel_axes")
        if saved_accel_axes:
            self.imu.set_accel_mapping(saved_accel_axes)

        # Full axis config (YPR remap, accel remap, CoM offset) goes to the MCU on startup.
        send_axis_config(
            imu_axes=saved_axes,
            accel_axes=saved_accel_axes,
            offset=config.get_section("imu_offset"),
            host=DEFAULT_ROV_HOST,
        )

        self.rpi_camera = init_rpi_camera(
            host=os.getenv("RPI_CAMERA_BIND", "0.0.0.0"),
            port=int(os.getenv("RPI_CAMERA_PORT", "6969")),
            latency_ms=int(os.getenv("RPI_CAMERA_LATENCY_MS", "12")),
            out_width=int(os.getenv("RPI_CAMERA_OUT_WIDTH", "960")),
            out_height=int(os.getenv("RPI_CAMERA_OUT_HEIGHT", "540")),
            jpeg_quality=int(os.getenv("RPI_CAMERA_JPEG_QUALITY", "70")),
            flip_180=_env_flag("RPI_CAMERA_FLIP_180"),
            marker_logger=self.aruco_logger,
        )

        ip_camera_config = config.get_section("ip_camera") or {}
        active_ip = ip_camera_config.get("active_ip") or logic.DEFAULT_IP_CAMERA_IP
        url = os.getenv("IP_CAMERA_URL")
        if url:
            active_ip = None
        else:
            url = logic.camera_url_for_ip(active_ip)
        self.ip_camera_settings = {
            "out_width": int(os.getenv("IP_CAMERA_OUT_WIDTH", "960")),
            "out_height": int(os.getenv("IP_CAMERA_OUT_HEIGHT", "540")),
            "jpeg_quality": int(os.getenv("IP_CAMERA_JPEG_QUALITY", "70")),
            "flip_180": _env_flag("IP_CAMERA_FLIP_180"),
        }
        self.ip_camera_active_ip = active_ip
        self.ip_camera_active_url = url
        self.ip_camera = init_ip_camera(
            url=url,
            marker_logger=self.aruco_logger,
            **self.ip_camera_settings,
        )

        self.resource = init_resource_receiver(port=12346)
        self.bitmask.set_resource_monitor(self.resource)

        self.setpoint_override = init_setpoint_override(resource_monitor=self.resource)
        self.controller.set_setpoint_client(self.setpoint_override)
        self.controller.set_pid_rates(config.get_section("pid_setpoint_rates") or {})
        self.controller.set_controller_gains(config.get_section("controller_gains") or {})

        self.control_telem = init_control_telemetry(port=5005)
        self.log_stream = init_log_stream(port=5006)
        self.system_control = SystemControlClient()

    # --- Qt plumbing ---------------------------------------------------------

    def poller(self, key, fn, interval_ms):
        """Return the named poller, creating it on first use.

        Screens sharing a key share one timer — e.g. pilot and tooling both watch the
        controller without doubling the poll rate.
        """
        poller = self._pollers.get(key)
        if poller is None:
            poller = Poller(fn, interval_ms, parent=self)
            self._pollers[key] = poller
        return poller

    def call_async(self, fn, on_done=None, on_error=None):
        """Run a blocking call off the GUI thread; deliver the result back on it.

        Mandatory for every call in the PARITY.md §1 table. Callbacks fire on the GUI thread,
        so they may touch widgets directly.
        """
        signals = _CallSignals(self)
        if on_done is not None:
            signals.done.connect(on_done)
        if on_error is not None:
            signals.error.connect(on_error)
        self._pool.start(_AsyncCall(fn, signals))
        return signals

    # --- hub-bound operations ------------------------------------------------

    def send_full_axis_config(self):
        """Axis remap and CoM offset go to the MCU together — never send a partial update."""
        send_axis_config(
            imu_axes=logic.load_imu_axes(),
            accel_axes=logic.load_accel_axes(),
            offset=logic.load_imu_offset(),
        )

    def save_pid_rates(self, rates):
        cleaned = logic.clean_pid_rates(rates)
        logic.config_handler.update_data({"pid_setpoint_rates": cleaned})
        if self.controller:
            self.controller.set_pid_rates(cleaned)
        return cleaned

    def save_controller_gains(self, gains):
        cleaned = logic.clean_controller_gains(gains)
        logic.config_handler.update_data({"controller_gains": cleaned})
        if self.controller:
            self.controller.set_controller_gains(cleaned)
        return cleaned

    def neutralize_thruster_command(self):
        """Force manual command output to neutral.

        INVARIANT: only Controller writes thruster commands. The direct bitmask write is the
        no-controller fallback only — do not reach for it anywhere else.
        """
        neutral = logic.neutral_axis_values()
        ctrl = self.controller
        manip = ctrl.get_manipulator()["setpoint_norm"] if ctrl else 0.0
        if ctrl:
            ctrl.clear_debug_override()
            ctrl.apply_manual_axes_once(neutral, source="GUI")
        elif self.bitmask:
            self.bitmask.set_from_axes(**neutral, manip=manip)
        return neutral

    def send_active_pid_setpoints(self):
        """Blocking — replays the override. Call via call_async."""
        ctrl = self.controller
        client = self.setpoint_override
        setpoints = ctrl.get_pid_setpoints() if ctrl else {}
        if not client:
            return {}
        client.clear_override()
        if setpoints:
            return client.send_override(setpoints, replay_attempts=5, replay_delay=0.1)
        return client.get_state()

    def manipulator_payload(self):
        import time

        ctrl = self.controller
        state = ctrl.get_manipulator() if ctrl else {}
        latest = self.control_telem.get_latest() if self.control_telem else {}
        manip = latest.get("manipulator") if isinstance(latest, dict) else {}
        now = time.time()
        updated_at = state.get("updated_at")
        telem_ts = latest.get("timestamp") if isinstance(latest, dict) else None
        return {
            "ok": bool(ctrl),
            "target_deg": state.get("setpoint_deg", 0.0),
            "setpoint_deg": state.get("setpoint_deg", 0.0),
            "source": state.get("source", "unknown"),
            "updated_age_ms": None if updated_at is None else max(0.0, (now - updated_at) * 1000.0),
            "applied_deg": manip.get("deg") if isinstance(manip, dict) else None,
            "pulse_us": manip.get("pulse_us") if isinstance(manip, dict) else None,
            "telemetry_age_ms": None if telem_ts is None else max(0.0, (now - telem_ts) * 1000.0),
        }

    def connection_proof(self):
        """Three independent proofs that the Nucleo is alive. Age thresholds are load-bearing."""
        import time

        uplink = self.bitmask.get_uplink_status() if self.bitmask else {}
        resource_stats = self.resource.get_stats() if self.resource else {}
        imu_stats = self.imu.get_stats() if self.imu else {}

        proofs = [
            {
                "name": "Command ACK",
                "active": logic.live_from_age(uplink.get("last_ack_age_ms"), 2500),
                "age_ms": uplink.get("last_ack_age_ms"),
                "detail": "Nucleo UDP counter changed after Topside command packets",
            },
            {
                "name": "Resource telemetry",
                "active": logic.live_from_age(resource_stats.get("last_age_ms"), 2500),
                "age_ms": resource_stats.get("last_age_ms"),
                "detail": "Nucleo resource packet on UDP 12346",
            },
            {
                "name": "IMU telemetry",
                "active": logic.live_from_age(imu_stats.get("age_ms"), 1200),
                "age_ms": imu_stats.get("age_ms"),
                "detail": "Nucleo IMU packet on UDP 5002",
            },
        ]
        connected = any(proof["active"] for proof in proofs)
        return {
            "ok": True,
            "connected": connected,
            "status": "live" if connected else "offline",
            "generated_at": time.time(),
            "proofs": proofs,
            "uplink": uplink,
            "resource": resource_stats,
            "imu": imu_stats,
        }

    def camera_status(self):
        status = self.ip_camera.get_status() if self.ip_camera else {"connected": False}
        active_url = (
            status.get("url") or self.ip_camera_active_url or logic.camera_url_for_ip(logic.DEFAULT_IP_CAMERA_IP)
        )
        active_ip = self.ip_camera_active_ip or logic.ip_from_url(active_url) or logic.DEFAULT_IP_CAMERA_IP
        return active_ip, active_url, status

    def reassign_ip_camera(self, ip):
        """Blocking — tears down and reconnects the RTSP receiver. Call via call_async."""
        url = logic.camera_url_for_ip(ip)
        if self.ip_camera:
            self.ip_camera.stop()
        self.ip_camera = init_ip_camera(
            url=url,
            marker_logger=self.aruco_logger,
            **self.ip_camera_settings,
        )
        self.ip_camera_active_ip = ip
        self.ip_camera_active_url = url

        section = logic.get_ip_camera_config()
        section["active_ip"] = ip
        logic.save_ip_camera_config(section)
        return self.camera_status()

    # --- teardown ------------------------------------------------------------

    def shutdown(self):
        """Mirrors app.py's atexit hook. Every service gets a chance to stop.

        Idempotent: the shell calls this when the last window closes and `main()` calls it again
        from its `finally`. With several windows open a second pass must not re-stop services
        that are already down.
        """
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        for poller in self._pollers.values():
            poller._timer.stop()
        self._pool.waitForDone(2000)

        for name, method in (
            ("controller", "stop"),
            ("bitmask", "stop"),
            ("imu", "stop"),
            ("rpi_camera", "stop"),
            ("ip_camera", "stop"),
            ("resource", "stop"),
            ("control_telem", "stop"),
            ("log_stream", "stop"),
            ("setpoint_override", "close"),
            ("system_control", "close"),
        ):
            service = getattr(self, name, None)
            if service is None:
                continue
            try:
                getattr(service, method)()
            except Exception:
                traceback.print_exc()
