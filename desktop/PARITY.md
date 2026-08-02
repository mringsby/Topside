# Desktop port — parity spec

Source of truth for the PySide6 port. Every behaviour in the Flask app that must survive is listed here.
Derived from a full read of `routes.py` (1348 lines, 85 routes), `app.py`, all templates and all JS.

> **Status: the port is complete and Flask is deleted.** `routes.py`, `app.py`, `static/` and
> `docs/swagger.yml` no longer exist — `git log` has them. This file is now a *historical* spec: it
> records what each retired endpoint became and why, which is the only remaining explanation of
> behaviour that has no other documentation. Read it before changing anything that used to be a route.
> Route paths below (`/api/...`) name the retired HTTP API, not anything callable today.

**Rule: no endpoint disappears without appearing in this table with a disposition.**

---

## 1. Architecture

Flask is removed entirely. `lib/` is unchanged — it never imported Flask and does not know the GUI exists.

```
app.py          -> desktop/main.py       composition root, same services, no Flask
routes.py       -> desktop/screens/*.py  each route's body becomes a direct lib/ call
static/js       -> desktop/screens/*.py  polling loops become ServiceHub pollers
static/templates-> Qt widgets
```

`desktop/services.py::ServiceHub` replaces `app.config`. Where a route did
`current_app.config.get("CONTROLLER")` and 503'd when absent, a screen does `hub.controller` and
disables its widgets when `None`. That degradation behaviour is required — it is what lets screens be
built and tested without hardware.

The foundation is three files:

| File | Contents |
|---|---|
| `desktop/logic.py` | Pure, no Qt: validation, clamping, coercion, persisted-settings IO. Lifted verbatim from `routes.py`. |
| `desktop/services.py` | `ServiceHub` (service construction + teardown), `Poller`, `call_async`, hub-bound operations. |
| `desktop/screens/base.py` | `ScreenBase` — poller lifecycle tied to visibility, `notify()`. |

**Never use `QMessageBox`.** A modal dialog blocks the Qt event loop, which stalls every poller and
the 20 Hz command loop behind it, and makes the screen impossible to test headlessly. Use
`self.notify("...")` from `ScreenBase`. This bit the reference screen during Phase 0; do not
reintroduce it.

### The data.json indirection

`ninedof_receiver`, `resource_receiver` and `control_telemetry` write their latest packet into
`data.json` on arrival; routes then read it back off disk. In-process that round-trip is pointless —
screens read the receiver object directly. **Keep the writes** (they are the on-disk record and
`test_protocols.py` asserts on them); drop only the read-back.

### Blocking calls — mandatory rule

Flask handled each request on its own thread. Qt has one event loop. These calls block and **must** go
through `ServiceHub.call_async()`, never be invoked directly from a slot:

| Call | Worst case |
|---|---|
| `request_pid_gains(timeout=2.0)` | 2.0 s |
| `send_pid_gains(timeout=1.0, max_retries=3)` | 3.0 s |
| `send_pid_gains(timeout=0.5, max_retries=2)` (killswitch) | 1.0 s |
| `SetpointOverride.send_override(replay_attempts=5, replay_delay=0.1)` | 0.5 s |
| `SystemControlClient.send_reset()` | 0.15 s |
| `init_ip_camera()` on reassign | RTSP connect, seconds |
| `_git_info()` subprocess | 1.0 s |

---

## 2. Screen inventory

| # | Flask route | JS (lines) | New module | Shard |
|---|---|---|---|---|
| 1 | `/` `layout.html` | — | `screens/home.py` | Opus |
| 2 | `/debug` | `debug.js` (135) | `screens/debug.py` | **Opus — reference screen** |
| 3 | `/pid-tuning` | `pid_tuning.js` (816) | `screens/pid_tuning.py` | A |
| 4 | `/graphs` | `graphs.js` (400) | `screens/graphs.py` | B |
| 5 | `/pilot` | `pilot.js` (269) + `manipulator.js` (107) | `screens/pilot.py` | C |
| 6 | `/tooling` | `lights.js` (60) + `manipulator.js` | `screens/tooling.py` | C |
| 7 | `/config` | `configuration.js` (317) | `screens/config.py` | D |
| 8 | `/connection` | `connection.js` (126) | `screens/connection.py` | D |
| 9 | `/ip-camera`, `/Camera2` | `ip_camera.js` (181) | `screens/ip_camera.py` | D |
| 10 | `/docs`, `/docs/swagger.yml` | — | **dropped** — see §5 | — |

`manipulator.js` is loaded by both `pilot.html` and `tooling.html`; it becomes one reusable
`widgets/manipulator.py` used by both screens. Shard C owns it.

### Not ported — verified dead

No template references these and no route renders them. Confirmed by grepping every `<script src>`,
`{% extends %}` and `{% include %}`:

`battery.js`, `depth.js`, `thrusters.js`, `sensors.js`, `resource_monitor.js`, `controller.js`
(~615 lines), plus templates `camera2.html`, `_controller.html`, `_config_modal.html`.

`fest.js` (93) loads on every page via `base.html` and makes no network calls — cosmetic. Not ported.

---

## 3. Endpoint → replacement

`hub` is the `ServiceHub`. Every route body collapses to the call in the right column.

### Camera / video
| Route | Replacement |
|---|---|
| `/rpi_video_feed` | `widgets/camera.py` polls `hub.rpi_camera.get_latest_jpeg_and_seq()` |
| `/video_feed` | **removed** — local camera feature dropped |
| `/ip_video_feed` | same, `hub.ip_camera` |
| `GET /api/rpi_camera/status` | `hub.rpi_camera.get_status()` |
| `GET /api/camera/status` | **removed** — local camera feature dropped |
| `GET /api/ip_camera/status` | `hub.ip_camera.get_status()` |

The MJPEG multipart framing and its no-cache headers disappear — the widget reads the JPEG buffer
directly. Keep `wait_for_next_frame(last_seq, timeout=0.25)`; it is the existing backpressure
mechanism and maps onto a poller cleanly. `generate_*_frames()` in `lib/camera.py` become unused but
**stay** — deleting them is out of scope for this port.

### IP camera config
| Route | Replacement |
|---|---|
| `GET /api/ip_camera/configs` | `_get_ip_camera_config()` + `_camera_status_payload()` → move to `services.py` |
| `POST /api/ip_camera/configs` | same validation: name matches `^[\w\s\-\.]+$`, IP via `_coerce_ipv4`, presets sorted case-insensitively, replace-by-name |
| `DELETE /api/ip_camera/configs/<name>` | 404-equivalent = show error, list unchanged |
| `POST /api/ip_camera/reassign` | **async** — `stop()` old, `init_ip_camera()` new, update hub + persist `active_ip` |

### ARUCO
| Route | Replacement |
|---|---|
| `GET /api/aruco-log` | `hub.aruco_logger.snapshot()` |
| `POST /api/aruco-log/{start,stop,clear}` | `.start()` / `.stop()` / `.clear()` |

### Static data.json reads — placeholders, reproduce as-is
| Route | Replacement | Note |
|---|---|---|
| `GET /api/thrusters` | `data_handler.get_section("thrusters")` | never written — static |
| `GET /api/battery` | `get_section("battery")` | never written — static |
| `GET /api/depth` | `get_section("depth")` | never written — static |
| `GET /api/sensors` | **`hub.imu.get_stats()`** | was disk read-back; now direct |
| `GET /api/resources` | **`hub.resource.get_stats()`**, falling back to `DEFAULT_RESOURCES` | was disk read-back |

### Controller
| Route | Replacement |
|---|---|
| `GET /api/lights` | `round(hub.controller.get_light() * 100)` |
| `POST /api/lights` | clamp 0..100, `set_light(pct / 100.0)` |
| `GET/POST /api/manipulator` | `_manipulator_payload(ctrl, control_telem)` → `services.py`; setter is `set_manipulator(deg, source="gui")`, rejects non-finite |
| `GET /api/command/status` | composite: uplink + `get_input_status()` + `get_control_state()` + UDP counters + override state |
| `GET /api/control/state` | `hub.controller.get_control_state()` |
| `GET/POST /api/controller/gains` | `_load_controller_gains()` / `_save_controller_gains()`; clamp master and each of 6 axes to 0..1 |
| `POST /api/control/killswitch` | `ctrl.kill()`, **async** zero-gain send, then `clear_override()` |
| `POST /api/control/rearm` | `ctrl.rearm()` + `clear_override()` |
| `GET /api/rov/status` | `bm.get_command()` + `get_uplink_status()` + control state + UDP counters |
| `POST /api/rov/command` | legacy HTTP-only entry point — **dropped**, see §5 |

### Connection / system
| Route | Replacement |
|---|---|
| `GET /api/connection/status` | `_connection_proof_payload()` → `services.py` verbatim; 3 proofs with thresholds 2500/2500/1200 ms |
| `GET /api/control/telemetry` | `hub.control_telem.get_latest()` + `get_stats()` |
| `GET /api/control/telemetry/history` | `get_history(limit)` |
| `GET /api/logs/live` | `hub.log_stream.get_recent(limit)`, limit clamped 1..500 |
| `POST /api/system/reset` | **async** `hub.system_control.send_reset()` |
| `GET /api/system/git` | **async** `_git_info()` |
| `GET /api/setpoint/status` | `hub.setpoint_override.get_state()` |

### IMU config
| Route | Replacement |
|---|---|
| `GET /api/imu/status` | `hub.imu.get_stats()` |
| `POST/DELETE /api/imu/tare` | `imu.tare()` / `imu.clear_tare()` |
| `GET/POST /api/imu/offset` | persist `imu_offset`, round to 1 decimal, then `_send_full_axis_config()` |
| `GET/POST /api/imu/axes` | validate against `{+,-}{yaw,pitch,roll}`, persist, `imu.set_axis_mapping()`, `_send_full_axis_config()` |
| `GET/POST /api/imu/accel_axes` | validate against `{+,-}{x,y,z}`, persist, `imu.set_accel_mapping()`, `_send_full_axis_config()` |

Any axis/offset change must re-send the **full** axis config packet — the MCU takes remap and offset
together in one packet. Do not send partial updates.

### Debug override
| Route | Replacement |
|---|---|
| `POST /api/debug/override` | clamp each axis to ±1, **negate yaw**, `ctrl.set_debug_override(axes)`; refuse when killed |
| `POST /api/debug/attitude_setpoint` | `_coerce_attitude_setpoints()` then **async** `send_override()` |
| `POST /api/debug/clear` | `clear_debug_override()`; if PID enabled re-send active setpoints, else `clear_override()` |

The yaw negation on `/api/debug/override` is real and load-bearing — sign convention differs between
the slider UI and the controller. Preserve it.

### PID
| Route | Replacement |
|---|---|
| `POST /api/pid/start` | IMU sanity gate → `ctrl.start_pid()` → **async** `send_override()`; rollback `stop_pid(clear=False)` on failure |
| `POST /api/pid/setpoints` | `_coerce_attitude_setpoints()`, `set_pid_setpoints()`, re-send only when PID active |
| `DELETE /api/pid/setpoints/<axis>` | `clear_pid_setpoint(axis)`, re-send remaining when active |
| `POST /api/pid/stop` | `stop_pid(clear=...)` + `clear_override()` |
| `GET/POST /api/pid/rates` | clamp each to 0..90 deg/s, persist, `ctrl.set_pid_rates()` |
| `POST /api/pid/zero_all` | `stop_pid()`, neutralize, `clear_override()`, **async** zero gains |
| `GET/POST /api/pid/gains` | **async** `request_pid_gains()` / `send_pid_gains()` |
| `GET/POST/DELETE /api/pid/configs[/<name>]` | `pid_configs.json` CRUD, name matches `^[\w\s\-\.]+$` |

**PID start sanity gate — reproduce exactly.** `_imu_attitude_sanity()` classifies three outcomes:
not `usable` → hard fail; `usable` but not `ok` → recoverable, UI must offer a **Force** action
(the HTTP 409 + `force_supported` path); `ok` → proceed. Losing the force path means losing the
ability to hold attitude with a slightly stale IMU, which is a field workflow.

---

## 4. Shared logic — already ported, do not reimplement

**Done in Phase 0.** Every helper below already exists. A shard that rewrites one of these has
introduced a parity bug by definition.

In `desktop/logic.py` (leading underscores dropped): `clamp`, `normalize_angle_deg`,
`neutral_axis_values`, `zero_pid_gains`, `attitude_pid_gains`, `mcu_pid_gains`, `clean_pid_rates`,
`clean_controller_gains`, `coerce_attitude_setpoints`, `debug_override_axes`, `imu_attitude_sanity`,
`git_info`, `live_from_age`, `load_pid_rates`, `load_controller_gains`, `load_imu_axes`,
`load_accel_axes`, `load_imu_offset`, `load_pid_configs`, `save_pid_configs`, `camera_url_for_ip`,
`coerce_ipv4`, `ip_from_url`, `get_ip_camera_config`, `save_ip_camera_config`,
`upsert_ip_camera_preset`, `delete_ip_camera_preset`, `valid_name`.

On `ServiceHub`: `send_full_axis_config`, `save_pid_rates`, `save_controller_gains`,
`neutralize_thruster_command`, `send_active_pid_setpoints`, `manipulator_payload`,
`connection_proof`, `camera_status`, `reassign_ip_camera`.

Constants in `logic.py`: `ATTITUDE_LIMITS_DEG`, `CONTROL_AXES`, `TRANSLATIONAL_AXES`,
`ATTITUDE_AXES`, `DEFAULT_PID_SETPOINT_RATES`, `DEFAULT_CONTROLLER_GAINS`, `DEFAULT_IP_CAMERA_IP`,
`DEFAULT_RESOURCES`, `VALID_IMU_AXES`, `VALID_ACCEL_AXES`, `_DEFAULT_IMU_AXES`,
`_DEFAULT_ACCEL_AXES`, `_DEFAULT_OFFSET`.

---

## 5. Deliberately dropped

| Dropped | Why |
|---|---|
| `/docs` + `docs/swagger.yml` | Documents an HTTP API that no longer exists. `docs/swagger.yml` stays in the repo as a record of the retired API. |
| `POST /api/rov/command` | Only consumer was `curl`/external scripts. No JS calls it. If external tooling depends on it, say so — it is the one route worth keeping a socket for. |
| MJPEG multipart framing | Superseded by direct buffer reads. |
| Bootstrap / Chart.js / hammer.js / swagger-ui CDN | Replaced by Qt styles and pyqtgraph. **Removes the app's last internet dependency** — relevant given this runs on an isolated `10.77.0.1/24` link. |

---

## 6. Invariants — restated, still binding

From `CLAUDE.md`. These outlive the UI and every shard prompt must carry them.

1. **Only `Controller` writes thruster commands.** Screens never call `BitmaskClient.set_from_axes()`.
   Go through `apply_manual_axes_once()` or `_neutralize_thruster_command()`, or the kill switch and
   gain scaling are bypassed.
2. **Input priority is killed → debug override → joystick.** Never reorder.
3. **Under PID hold, manual sticks are setpoint rate inputs.**
4. **Never hardcode `data/` or `logs/`.** Use `data_path()` / `log_path()`.
5. **All JSON writes go through `JSONDataHandler`.** Note `_load_pid_configs`/`_save_pid_configs`
   currently bypass this with bare `open()` — pre-existing, ported as-is, not fixed here.
6. **Endianness differs per UDP port.** No packet code changes in this port at all.

---

## 7. Shard contract

Every shard prompt carries this verbatim.

1. **Read first, in order:** `CLAUDE.md` §Invariants, this file (§1, §3 rows for your screens, §6),
   then `desktop/screens/debug.py` — the reference screen. Copy its patterns.
2. **Write only new files** under `desktop/screens/` and `desktop/widgets/`. Do **not** edit
   `registry.py`, `main.py`, `services.py`, `logic.py`, `base.py`, or anything under `lib/`,
   `routes.py`, `app.py`, `static/`, or `tests/`. Need something added to the foundation? Report it;
   don't add it. This is what keeps parallel shards from colliding.
3. **Reuse §4.** If a helper exists in `logic.py` or on `ServiceHub`, call it.
4. **Blocking calls go through `hub.call_async`.** See the §1 table.
5. **No `QMessageBox`.** Use `self.notify(...)`.
6. Poll via `self.watch(key, fn, interval_ms, slot)`, never a bare `QTimer`, unless it is a local
   write loop (see `debug.py::_send_timer`).
7. Match the source page's poll intervals — they are in the JS files and some are load-bearing.
8. Run before reporting: `uv run --group lint ruff format desktop/`,
   `uv run --group lint ruff check desktop/`, `uv run --group dev pytest -q` (48 must still pass).
9. **Report:** your screen class name(s) and any foundation gap you hit.

## 8. Verification

Parity is verified per screen, against the running Flask app.

1. `uv run python app.py` and `uv run python -m desktop` side by side (no hardware needed — both
   degrade to disabled controls).
2. For each screen: every control present in the Jinja template exists in the Qt screen, and every
   endpoint in the §3 table for that screen is exercised.
3. `uv run --frozen --group dev pytest` — the six `lib/` test files must stay green untouched.
4. Replay traffic with `tools/send_control_telemetry.py` and confirm graphs/telemetry update.
5. UI responsiveness: trigger every async-marked call above and confirm the window never blocks.
