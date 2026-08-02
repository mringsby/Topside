# Topside — session handoff

**Read this first, then `desktop/PARITY.md`.** This file says where the work stands and what to do
next. `PARITY.md` is the Flask→Qt spec and does not change unless the spec changes.

The previous handoff covered the Flask→PySide6 port (finished; see `git log` and `PARITY.md`), then
the tabbed shell → dock shell port. **Both the tab bar and the dock shell are gone.** A workspace is
now a snap-to-grid canvas: `desktop/grid.py`.

---

## 0. STATUS — start here

**Done: dark mode, local-webcam removal, all ten screens decomposed into panels, whole-workspace
presets, and the grid canvas that replaced the dock shell.**

Gates, all currently passing:

```bash
uv run --frozen --group dev pytest -q                                    # 132 passed
uv run --frozen --no-default-groups --group lint ruff format --check .   # CI gate 1
uv run --frozen --no-default-groups --group lint ruff check .            # CI gate 2
```

48 components in 12 categories: 10 whole screens + 38 panels lifted out of them. Every one
constructs standalone against a real `ServiceHub`.

Invariant sweep — run it again after any change:
- no `set_from_axes` outside `ServiceHub.neutralize_thruster_command()`
- no `QMessageBox` in code (only in docstrings explaining the ban)
- no hardcoded `data/` or `logs/` paths
- every IMU axis/offset write followed by `hub.send_full_axis_config()`
- no `zlib.crc32` — checksums go through `lib/crc.py::crc32_ieee`
- nothing may open `cv2.VideoCapture(0)`:
  `grep -rn "default_camera\|DefaultCameraReceiver\|init_camera\|VideoCapture(0)"` must be empty

---

## 1. What changed

| Area | Where |
|---|---|
| Component descriptor | `desktop/component.py` — `Component(id, title, factory, category, duplicable, owns, order)` |
| Registry | `desktop/registry.py` — `SCREEN_COMPONENTS`, `PANEL_COMPONENTS`, `COMPONENTS`, `BY_ID`, `BY_TITLE`, `categories()` |
| Grid canvas | `desktop/grid.py` — `GridCanvas`, `GridTile`, and the pure cell arithmetic |
| Shell | `desktop/shell.py` — `Shell`, `WorkspaceWindow` |
| Panel base | `desktop/screens/base.py` — `PanelBase`, and `ScreenBase(PanelBase)` |
| Theme | `desktop/theme.py` — `DARK`/`LIGHT` tokens, `apply()`, `badge()`, `token()`, `signals.changed` |
| Presets | `desktop/logic.py` (`load/save/delete_workspace_preset`, `CLASSIC_PRESET`) + `desktop/screens/workspaces.py` |
| Entry point | `desktop/main.py` — ~35 lines: `QApplication` → `theme.apply` → `ServiceHub` → `Shell` |

**Deleted outright:** `ComponentDock`, `Shell.build_tabbed`, `Shell.load_classic`, every
`QDockWidget` in the app, `desktop/screens/camera1.py`, `lib/camera.py::DefaultCameraReceiver`,
`init_camera`, `hub.default_camera` and its shutdown hook.

### Why the dock shell went

It was measured, not disliked. Two defects, both structural:

- `add_dock` hardcoded `Qt.LeftDockWidgetArea`, so **every** component became a full-window-width
  strip stacked under the last one. No code path ever placed two side by side.
- Docked bare, a panel's content minimum is a hard floor for its dock. `screen.pid_tuning` alone is
  1312x609; with Classic loaded the window could not go below **1308x806**, and adding one small
  chart panel took that to **1308x1008**. On a 1080p display there is no slack left, so dragging a
  splitter does nothing — which is exactly what "I can't resize them" meant.

Both are gone: placement is explicit cell arithmetic, and every panel sits in a `QScrollArea` inside
its tile, so a tile can be smaller than its content. Same four panels, same window: minimum size
hint went from 1308x1008 to **68x107**.

---

## 2. The things that will bite you

These are the non-obvious decisions. Each was measured, not assumed.

**1. Bind the panel to its tile BEFORE showing the tile.** `GridCanvas.add_tile` does this and it
is the whole reason it takes the panel rather than letting the caller add it. A `PanelBase` that is
shown while still unbound falls back to its own `showEvent` and activates itself — and then nothing
ever deactivates it, so a component placed below the fold polls forever. This cost two failing tests
to find; do not reorder those three lines.

**2. Activation runs off the host's `visibilityChanged`, not `showEvent`/`hideEvent`.** Under docks
that signal came from a dock tabified behind another. There are no tabs now, so `GridCanvas` drives
it from the **viewport**: a tile scrolled out of sight reports itself invisible and its panel drops
its poller subscriptions. That is what preserves "a panel you cannot see costs nothing", which
otherwise would have been lost with the tabs. `PanelBase._host_managed()` walks up the parent chain
so a child built *after* `bind_host` still defers to it; un-hosted panels (plain layouts, tests) keep
the old show/hide behaviour.

**3. `PanelBase.teardown()` is load-bearing.** Screens used to live for the whole process, so a
leaked `subscribe()` was impossible. A closeable tile makes it the default failure: close an active
panel without teardown and the poller's refcount never returns to 0, so its timer runs forever.
`_watched` holds `(poller, slot)` pairs so teardown disconnects *this* panel without severing other
panels sharing the same poller. `Shell.close_tile` is the only place that runs it for a live tile.

**4. `Component.owns` is what makes the single-instance rule safe.** The guard is keyed on component
id, and a screen and its own panel have *different* ids — so without `owns` you could open Pilot
**and** the standalone Manipulator panel and drive one actuator from two widgets. A screen declares
the panels it contains; opening it reserves them. `owns` is derived from the id prefix in
`registry.py`, so adding a panel to a screen module extends that screen's reservation automatically.

Two deliberate exceptions, both documented at the line in `registry.py`:
- **Tooling does not claim `panel.pilot.manipulator`.** The tabbed build already shipped a
  manipulator on both screens, so claiming it would stop Classic opening Pilot and Tooling together.
  Two manipulators is last-write-wins on a one-shot command, not a racing loop.
- **Composite screens were kept, not retired.** They are how an operator gets a whole screen in one
  tile instead of assembling ~30 panels by hand, and the `Classic` preset still needs them.

**5. Only `Shell` shuts the hub down.** The old `MainWindow.closeEvent` called `hub.shutdown()`
unconditionally. With N windows that kills every service when the *first* one closes, leaving the
rest driving dead sockets in silence. Windows report upward via `Shell.window_closed`; the hub goes
down when the last one does. `ServiceHub.shutdown()` is guarded by `_shut_down` because `main()`'s
`finally` still calls it.

**6. The layout algorithm has exactly two rules, and both are deliberate.** A drop pushes what it
lands on **downward** — never sideways, never a swap, because those depend on tile storage order,
which an operator cannot predict mid-drag. And nothing is ever **compacted upward** — a tile stays
where it was dropped even with empty space above it, because auto-compaction yanks a component out
from under the cursor the moment whatever was above it closes. `resolve()` is all of it; it is pure
and covered in `tests/test_grid.py`.

---

## 3. Adding a panel — the current recipe

`desktop/screens/graphs.py` is the worked reference. Copy its patterns.

1. Subclass `PanelBase` (from `desktop.screens.base`), ctor `(hub, ..., parent=None)`, set `self.title`.
2. Do your own `self.watch(key, fn, interval_ms, slot)` — a panel must work alone in a tile. Put the
   poll getter at module level taking `hub`, so panels sharing a key share one timer.
3. Do **not** have the parent screen watch on your behalf — `PanelBase.set_active` cascades to child
   panels via `findChildren(PanelBase)` (transitive, because a panel inside a `QGroupBox` is a
   grandchild).
4. Export `COMPONENTS = [Component(...)]` from your own module. Import `Component` from
   **`desktop.component`**, never from `desktop.registry` — that is a circular import.
5. `factory` takes only `hub`; use a closure for extra ctor args (see `_chart_factory`).
6. Set `duplicable=True` **only** for read-only displays. Anything that writes to the vehicle is
   single-instance. This is a hardware-safety decision, not a UI preference.
7. A panel's `sizeHint()` decides how many cells it gets when opened, via `grid.preferred_cells`. A
   panel with no meaningful hint opens at the 2x2 floor; give it a sensible one rather than fighting
   the grid.

**`registry.py` stays integrator-owned.** A shard exports `COMPONENTS`; the integrator adds the
import and the concatenation line. That is what keeps parallel shards from colliding.

Panels that live inside a parent screen take an optional `notify=` callback so their messages
surface in the screen's single notice widget; standalone they fall back to `PanelBase.notify()`.

---

## 4. What is left

1. **PID Tuning's write panels are inert standalone.** Measured usable controls when opened alone:
   `panel.pid_tuning.override` **0/9**, `action_bar` 1/4, `setpoints` 3/11, `gains` 10/15. Their
   logic stayed on `PidTuningScreen` to keep `_start_pid_work` / `_kill_work` / `_rearm_work` /
   `_send_setpoints_work` / `_clear_axis_work` / `_stop_pid_work` **Qt-free** — `test_desktop_services.py`
   drives those via `PidTuningScreen.__new__(...)` with a `SimpleNamespace` hub and no widget tree.
   Because the `owns` guard means the screen is never open at the same time, those controls are dead
   in practice. Fix = move the write logic into the panels and have the screen delegate, keeping the
   `_*_work` methods on the screen and Qt-free. Every other screen's panels are fully usable
   standalone. (`panel.connection.testpy_activation` is also inert, but it was already a dead
   placeholder before this work — see PARITY.md §3.)
2. **Three things cannot be verified without a real display**: whether light mode is genuinely
   readable; whether a second workspace window on a second monitor round-trips through a saved
   preset; and whether the drag/resize gestures *feel* right (the offscreen tests drive
   `GridCanvas.begin_gesture`/`update_gesture`/`end_gesture` directly, which proves the arithmetic
   and the commit but not the hit-testing of the 7px `RESIZE_MARGIN` under a real cursor).
   `RESIZE_MARGIN`, `ROW_HEIGHT` and `COLUMNS` in `grid.py` are the three knobs to turn if it does
   not; none of them is load-bearing.
3. **The release build is still untested**, and `run.ps1` / `installer/Topside.iss` /
   `release.yml` were already flagged in the previous handoff as pointing at the retired Flask app.
   Tag a throwaway `v*` and watch the workflow before trusting a real release.
4. **`POST /api/rov/command`** (PARITY.md §5) was dropped; the only consumers were `curl` and
   external scripts. Still unanswered from the previous session — confirm nothing external needs it.
5. **No keyboard path to move or resize a tile**, and no "tidy up" command. Both are additive; the
   grid arithmetic they would need is already pure and tested.

---

## 5. How to verify

`pytest` covers the geometry (`tests/test_grid.py`, no Qt) and the tile lifecycle
(`tests/test_desktop_screens.py`, offscreen Qt). The shell against a **real** `ServiceHub` still
needs a headless run. Build a short script (do not commit it) under
`QT_QPA_PLATFORM=offscreen PYTHONPATH=.` that constructs `Shell(ServiceHub())` and asserts:

- `load_empty()` → 1 window, 0 tiles
- open four components → **the acceptance assertion for the whole design**: at least two share a
  row, no two placements `grid.intersects`, and no tile is the full window width
- the window then resizes to 700x500 (under docks its minimum was 1308x1008)
- drive a real gesture: `canvas.begin_gesture(tile, "move", global_pos, grab=...)` →
  `update_gesture(target_global_pos)` → `end_gesture()`, and check the tile committed to the cell
  the target pixel snaps to. Same again with `"resize"`, `right=True, bottom=True`
- `canvas.commit(tile, placement_on_top_of_another)` → the dropped tile keeps its cell and the one
  it landed on has moved to `tile.placement.bottom` or lower
- move a chart to row 60 → `hub._pollers["graphs.sample"]._subscribers` drops to 0; scroll to the
  bottom → it returns to 2 (the viewport-visibility mechanism, §2.2)
- `tile.request_close()` returns its poller's `_subscribers` to 0 (the teardown leak test)
- a duplicable panel opens twice; a writer panel is refused with "already open" in
  `window.statusBar().currentMessage()`
- capture a preset, reload it, compare **cells** and not just component ids — a layout that comes
  back in different places is not restored. Point `logic.workspace_handler` at a throwaway
  `JSONDataHandler(file_path=Path(...))` first; assigning a `str` to `.file_path` fails the write
  silently and makes the whole check vacuous
- closing all windows runs `hub.shutdown()` exactly once

A real `ServiceHub` is fine in that script — every service degrades without hardware. **Never
construct one in a pytest test**; it binds UDP sockets, opens cameras and starts threads. Tests use
the local `FakeHub` in `tests/test_desktop_screens.py` (whose `poller()` is memoized by key, like
the real hub) or a `SimpleNamespace` in `tests/test_desktop_services.py`.

Run `graphify update .` after code changes (AST-only, no API cost).

---

## 6. The rules that outlive the UI

From `CLAUDE.md`, restated in `PARITY.md` §6. These are the things that "look correct and pass tests
but misbehave on hardware".

1. **Only `Controller` writes thruster commands** — via `apply_manual_axes_once()` or
   `hub.neutralize_thruster_command()`, never `BitmaskClient.set_from_axes()`.
2. **Input priority is killed → debug override → joystick.** Never reorder.
3. **Under PID hold, manual sticks are setpoint *rate* inputs, not torque inputs.**
4. **Any IMU axis or offset change re-sends the FULL axis config packet.**
5. **Port 5004 has no ACK** — the axis probe reports *liveness only*. Never reword it to say
   "verified"; confirming a remap needs physically moving the vehicle.
6. **`setpoint_override` refuses to transmit when UDP RX errors are rising.** Every call site must
   surface the refusal; the guarded action itself still completes.
7. **Never hardcode `data/` or `logs/`** — use `data_path()` / `log_path()`.
8. **Endianness differs per UDP port.** Match the existing struct format; keep
   `tests/test_protocols.py` in sync.
9. The **yaw negation** on the debug override is real and load-bearing.
10. **No modals — but do not drop confirmations.** A destructive action keeps its guard as a
    two-step arm: first click re-labels, second click within ~5 s commits, the arm lapses on a
    single-shot timer, and `on_deactivate` disarms. See `screens/connection.py::_on_reset_clicked`,
    `screens/pid_tuning.py::_delete_config`, and `screens/workspaces.py`. A `QFileDialog` is fine.
