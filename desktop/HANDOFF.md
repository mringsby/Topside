# Desktop port — session handoff

**Read this first, then `desktop/PARITY.md`.** This file says where the work stands and what to do
next. `PARITY.md` is the spec and does not change unless the spec changes.

Repo state: commit `c89da76`, nothing committed since — see §5.

---

## 0. STATUS — start here

**All 10 screens are ported, merged and verified. Every shard (A–E) is complete.**

Tabs, in order: Home, Pilot, Tooling, Debug, PID Tuning, Graphs, Config, Connection, IP Camera,
Camera 1. `registry.PENDING` is now empty.

Gates: `ruff format` clean, `ruff check` clean, `pytest -q` → 48 passed, and a headless smoke test
cycles all 10 tabs with pollers ticking and zero failures.

Invariant sweep (run it again after any change):
- no `set_from_axes` outside `ServiceHub.neutralize_thruster_command()`
- no `QMessageBox` in code (only in docstrings explaining the ban)
- no hardcoded `data/` or `logs/` paths
- every IMU axis/offset write followed by `hub.send_full_axis_config()`

### Phases 3 and 4 are also done

**Phase 3 — tests re-derived.** The 4 route test files are deleted; their 15 tests were re-derived
against the desktop layer as 25 tests in `tests/test_desktop_logic.py`,
`tests/test_desktop_services.py` and `tests/test_desktop_screens.py`. **58 tests pass.** The
mapping old→new is in this session's history; the split worth knowing is the old killswitch test,
which asserted two behaviours at different layers and is now two tests (gain-zeroing in services,
killed-refusal in screens).

Most desktop tests need no Qt at all — `logic.py` is pure, and screen "work" methods are tested via
`PidTuningScreen.__new__(PidTuningScreen)` with a `SimpleNamespace` hub, skipping `__init__` so no
widget tree is built. Only 3 tests need an offscreen `QApplication`. **Never construct a real
`ServiceHub` in a test** — it binds UDP sockets, opens cameras and starts threads.

**Phase 4 — Flask retired.** Deleted: `routes.py`, `app.py`, `static/`, `docs/swagger.yml`, the 4
route test files, the 3 dead MJPEG generators in `lib/camera.py`, and the `flask` dependency (plus
5 transitive deps). `run.sh`, `run.ps1` and `release.yml` now build `desktop/__main__.py`.
Also removed `lib/comms.py` and `lib/eventlogger.py`, dead before this port began.

**Both CI gates now pass** (`ruff format --check .` and `ruff check .`) — they had been failing on
pre-existing issues since before the port.

### What is left

1. **The release build is untested.** PyInstaller has never been run against a PySide6 entry point
   here. Qt plugin bundling is the usual failure mode. Tag a throwaway `v*` and watch the workflow
   before trusting a real release.
2. **The two open questions in §4.**

### Integrator fixes applied on top of the shards — do not regress these

There were exactly three `window.confirm()` sites in the whole Flask app. Shards handled them
inconsistently, so all three are now settled:

| Site | Outcome |
|---|---|
| `connection.js:95` restart MCU | Shard D **dropped the guard entirely**. Restored as a two-step arm (`connection.py::_on_reset_clicked`). |
| `pid_tuning.js:477` PID force-start | Shard A correctly turned it into a **Force Start** button — matches the 409 + `force_supported` design. Kept. |
| `pid_tuning.js:732` delete saved tune | Shard A **dropped the guard**, claiming no non-blocking equivalent exists. Restored as a two-step arm (`pid_tuning.py::_delete_config`). |

**The rule shards keep misreading:** the port bans *blocking modals*, not confirmation itself. A
destructive action that had a confirm keeps one, as a two-step arm — first click re-labels the
button, second click within 5 s commits, the arm lapses on its own, and leaving the screen disarms.
Both existing implementations are verified by smoke test. Any shard that "drops the confirmation
because dialogs are banned" is wrong; point it at `connection.py::_on_reset_clicked`.

`QFileDialog` in the graphs CSV export is allowed: it is a user-initiated file picker, and the 20 Hz
thruster re-transmit and 60 Hz controller loop run on their own daemon threads in `lib/`, not the Qt
loop, so it stalls only graph polling.

---

## 1. What this project is doing

Porting Topside from a Flask web dashboard to a **PySide6 desktop app**, at full feature parity.
Flask is dropped entirely. `lib/` (3,937 lines, 19 modules, the hardware layer) never imported Flask
and is **not touched by this port at all**.

What dies at the end: `routes.py` (1,348 lines, 85 routes), `static/templates/`, `static/js/`, and
4 of 10 test files. None of it is deleted yet — the desktop app is being built alongside the working
Flask app so the two can be diffed screen by screen.

Decisions already made by the user, do not relitigate:

- Framework: **PySide6 / Qt** (with `pyqtgraph` for the graphs screen).
- **Drop Flask entirely** — not a hybrid, not a wrapper.
- **Model tiering**: Opus does foundation + integration only; Sonnet does the screens; Haiku does
  mechanical inventory. The point is to keep Opus off the repetitive 80%.

---

## 2. Phase 0 is complete and verified

`desktop/` — 1,445 lines across 9 files, ruff-clean, and the existing 48 tests still pass.

| File | Lines | What it is |
|---|---|---|
| `desktop/logic.py` | 347 | Pure functions, no Qt. Validation, clamping, coercion, persisted-settings IO — lifted **verbatim** from `routes.py` so semantics cannot drift. |
| `desktop/services.py` | 394 | `ServiceHub` (builds/tears down all 12 services), `Poller` (reference-counted, shared by key), `call_async` (QThreadPool), and the hub-bound operations. |
| `desktop/screens/base.py` | 92 | `ScreenBase` — `watch()` poller lifecycle tied to widget visibility, `notify()`, `on_activate`/`on_deactivate`. |
| `desktop/screens/debug.py` | 241 | **The reference screen.** Every shard copies its patterns. |
| `desktop/registry.py` | 27 | `SCREENS` + `PENDING`. The one shared file. Integrator-owned. |
| `desktop/main.py` | 56 | Composition root. Qt tabs, `PENDING` rendered as disabled tabs. |
| `desktop/PARITY.md` | 283 | The spec: all 85 routes → replacement, invariants, shard contract. |

**Verified by running it, not by reading it:** all 12 services construct and shut down cleanly; the
poller subscribes on show and unsubscribes on hide; both `call_async` paths fire (success and
exception); the debug override reaches the controller (`control_path: Override Controls`); the yaw
sign flip works (slider +50 → −0.5); stop returns control to PS4 with sliders zeroed; a killed
controller refuses the override. `uv run --group dev pytest -q` → 48 passed. Ruff clean.

### Three findings from the parity audit that changed the plan

1. **~615 lines of JS are dead** — `battery.js`, `depth.js`, `thrusters.js`, `sensors.js`,
   `resource_monitor.js`, `controller.js`, plus templates `camera2.html`, `_controller.html`,
   `_config_modal.html`. Confirmed by grepping every `<script src>` and `{% extends %}`. Real scope
   removed. `/Camera2` renders `ip_camera.html`, not `camera2.html`.
2. **Three endpoints serve frozen data.** `/api/depth`, `/api/battery`, `/api/thrusters` read
   `data.json` sections that nothing ever writes. Only `imu`, `resources` and `control_telemetry`
   are written. `pilot.js` has always been polling a seed value. Reproduced as placeholders —
   parity is the spec — and flagged in PARITY.md §3.
3. **Blocking calls were the real reason to build a foundation.** Flask gave each request its own
   thread, so routes blocked freely on MCU round-trips. Qt has one event loop;
   `request_pid_gains(timeout=2.0)` and friends would freeze the window. Solved once in
   `ServiceHub.call_async`; the full offender list is PARITY.md §1.

### One course correction worth knowing

The first reference screen used `QMessageBox` and the headless smoke test **hung**. A modal blocks
the Qt event loop — stalling every poller and the 20 Hz command loop behind it — and makes screens
untestable headlessly. Replaced with a non-blocking `notify()` on `ScreenBase`, and banned in the
shard contract (PARITY.md §7 rule 5). Do not reintroduce it, and do not let a shard reintroduce it.

---

## 3. Shard recipe (all shards complete — kept for reference)

All five shards are merged. The table and prompt below are retained because the same recipe applies
to any future screen work.

**Integrator consolidation applied after the shards landed** — worth knowing, since it is the class
of thing shards structurally cannot do:

- `widgets/camera.py` (shard E) is now the single video widget. Both `screens/ip_camera.py`
  (shard D) and `screens/pilot.py` (shard C) had written their own inline frame pollers, because
  each shard ran before E existed. Both were refactored onto the shared widget, which deleted two
  duplicate pollers and their dead `QPixmap`/`Qt`/`FRAME_INTERVAL_MS` imports.
- `CameraWidget.set_aspect_mode()` was added to absorb pilot's Fit/Fill toggle (the template's
  `object-fit: contain | cover`), so the shared widget covers all three camera surfaces.
- `ip_camera.py` gained the `on_deactivate` it was missing — it had been decoding JPEGs in the
  background for a hidden tab.

| Shard | Model | Builds | Source |
|---|---|---|---|
| **A** | Sonnet | `screens/pid_tuning.py` | `pid_tuning.js` (816 lines) + PID routes (`routes.py` 1104–1348). Largest single unit — runs alone. Includes the force-start sanity path. |
| **B** | Sonnet | `screens/graphs.py` | `graphs.js` (400). pyqtgraph rebuild: ring buffers, crosshair, zoom. |
| **D** | Sonnet | `screens/config.py`, `screens/connection.py`, `screens/ip_camera.py` | `configuration.js` (317) + `connection.js` (126) + `ip_camera.js` (181). |
| **C** | Sonnet | `screens/pilot.py`, `screens/tooling.py`, `widgets/manipulator.py` | **TODO.** `pilot.js` (269) + `manipulator.js` (107) + `lights.js` (60). One agent because they share `Controller` and `manipulator.js` is loaded by both pages. Needs to create the `desktop/widgets/` package. This is the shard where the command-path invariants actually bite. |
| **E** | Sonnet | `widgets/camera.py`, `screens/camera1.py` | **TODO.** MJPEG generators → `QLabel`/`QPixmap` fed directly from `lib/camera` buffers. Point it at `screens/ip_camera.py`, which already contains an inline frame poller (100 ms, sequence-compare, placeholder fallback, re-reads `hub.ip_camera` each tick because reassign swaps the object) — E should generalise that, and the integrator refactors `ip_camera.py` onto the shared widget once E lands. |

### The prompt to give each shard

Substitute the shard letter, screens and source files. The rest is verbatim.

> You are shard **{X}** of the Topside PySide6 port. Build **{screens}**, porting **{source JS files}**.
>
> Read first, in this order: `CLAUDE.md` §Invariants; `desktop/PARITY.md` §1, §6, §7, and the §3
> rows for your screens; then `desktop/screens/debug.py` — the reference screen. Copy its patterns.
>
> Never use `QMessageBox` or any modal for status, errors, or confirmations. But do NOT drop a
> confirmation the JS had — destructive actions keep their guard as a **two-step non-blocking arm**;
> copy `desktop/screens/connection.py::_on_reset_clicked`.
>
> Then follow the shard contract in PARITY.md §7 exactly. It is binding. In particular:
> **write only new files** under `desktop/screens/` and `desktop/widgets/` — do not edit
> `registry.py`, `main.py`, `services.py`, `logic.py`, `base.py`, or anything under `lib/`,
> `routes.py`, `app.py`, `static/` or `tests/`. If you need something added to the foundation,
> report it; do not add it yourself.
>
> Before reporting, run: `uv run --group lint ruff format desktop/`,
> `uv run --group lint ruff check desktop/`, `uv run --group dev pytest -q` (48 must still pass).
> Report your screen class name(s) and any foundation gap you hit.

**Why "write only new files" matters:** the only shared file is `registry.py`, and the integrator
adds each screen's import + `SCREENS` entry at merge time. That makes conflicts structurally
impossible rather than merely unlikely, which is why worktree isolation is optional here.

### After the shards

- **Merge step (integrator, per shard):** add the import and `SCREENS` entry in `registry.py`,
  remove the matching `PENDING` row, run the app, verify against the Flask screen side by side.
- **Phase 3 — one Sonnet agent.** Re-derive the 4 dying route test files against `lib/` directly.
  The `Fake*` fixtures are salvageable; the `response.get_json()` assertions are not. The other
  6 test files need zero changes.
- **Phase 4 — Opus.** Cross-screen integration review, then the release path nobody will remember:
  `installer/Topside.iss`, `.github/workflows/release.yml` `--add-data` flags, `run.ps1` / `run.sh`.
  All three still point at Flask.

---

## 4. Two open questions for the user

Neither blocks shard A, B or D. Both were raised at the end of Phase 0 and never answered.

1. **`POST /api/rov/command`** is slated for removal (PARITY.md §5). No JS calls it — the only
   consumers were `curl` and external scripts. If any external tooling depends on it, that is the
   one route worth keeping a socket open for. Ask before Phase 4.
2. **The release path** (§ Phase 4 above) still builds the Flask app. Fine until the port lands,
   but it means a `v*` tag today ships the old app.

---

## 5. Repo state — nothing is committed

```
 M pyproject.toml     # adds pyside6>=6.8,<7 and pyqtgraph>=0.13,<0.14
 M uv.lock
?? .claude/           # graphify hooks + project-scoped skill
?? CLAUDE.md          # written this session, graphify-aware
?? backup.txt         # terminal scrollback, not part of the project
?? desktop/           # the entire Phase 0 foundation
?? graphify-out/      # ~1.6 MB of generated graph artifacts
```

Recommended before starting the shards, so each shard's diff is legible:

- Commit `desktop/`, `CLAUDE.md`, `.claude/`, `pyproject.toml`, `uv.lock`.
- **gitignore `graphify-out/`** — 1.6 MB of generated artifacts that churn on every rebuild.
- `backup.txt` is scrollback; delete or ignore it.
- Branch first. `main` is the default branch and this is a large port.

Also note: `graphify-out/graph.json` predates `desktop/`, so it is stale. Run `graphify update .`
after committing (AST-only, no API cost).

---

## 6. The rules that outlive the UI

From `CLAUDE.md`, restated in PARITY.md §6. These are the things that "look correct and pass tests
but misbehave on hardware", so every shard prompt carries them:

1. **Only `Controller` writes thruster commands.** The GUI must not call
   `BitmaskClient.set_from_axes()` any more than a route could — go through
   `apply_manual_axes_once()` or `hub.neutralize_thruster_command()`, or the kill switch and gain
   scaling are silently bypassed.
2. **Input priority is killed → debug override → joystick.** Never reorder.
3. **Under PID hold, manual sticks are setpoint *rate* inputs, not torque inputs.**
4. **Never hardcode `data/` or `logs/`** — use `data_path()` / `log_path()`.
5. **Endianness differs per UDP port.** No packet code changes in this port at all.
6. The **yaw negation** on the debug override is real and load-bearing. Preserve it.
7. The **PID force-start path** (HTTP 409 + `force_supported`) is a field workflow, not an error
   case. Losing it means losing the ability to hold attitude with a slightly stale IMU.
