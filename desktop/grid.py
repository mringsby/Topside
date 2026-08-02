"""The snap-to-grid workspace canvas — what components are laid out on.

A workspace is a fixed `COLUMNS`-column grid with a fixed row height. Every component occupies a
whole number of cells (`Placement(col, row, cols, rows)`), is dragged by its header bar, and is
resized from its right/bottom edges. Both gestures snap to cells, so two components dropped beside
each other line up exactly and stay lined up when the window is resized — column width is a
fraction of the canvas, row height is constant.

**Why not QDockWidget.** The dock shell placed every new component in one dock area, which meant a
full-width strip stacked under the last one, and it honoured each panel's content minimum as a hard
floor: with the ten screens loaded the window could not go below 1308x1008, so the splitters had
nothing left to give and resizing did nothing. Here placement is explicit cell arithmetic and every
panel sits in a `QScrollArea`, so a tile can be made smaller than its content and the content
scrolls. The cost of dropping docks is that there is no tab grouping and no tearing a component out
onto a second monitor — open a second workspace window for that instead.

Two rules the layout follows, both deliberate:

**Dropping a component pushes what it lands on downward; it never swaps or reflows sideways.**
`resolve()` is the whole algorithm. Sideways reflow makes a drop depend on the order tiles happen
to be stored in, which is not something an operator can predict mid-drag.

**Nothing is ever compacted upward.** A tile stays exactly where it was dropped even if the space
above it is empty. Auto-compaction would yank a component out from under the cursor the moment
whatever was above it is closed.

The geometry half of this module is pure — no Qt, no widgets, directly testable (`tests/test_grid.py`).
"""

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

#: Columns in a workspace. 12 divides into halves, thirds, quarters and sixths, so the arrangements
#: an operator actually wants (two-up, three-up, sidebar + main) all land on exact cell boundaries.
COLUMNS = 12

#: Row height in pixels. Constant, unlike column width: rows scroll, columns fit the window.
ROW_HEIGHT = 44

#: Pixels between neighbouring tiles, and between a tile and the canvas edge.
GAP = 6

#: Smallest tile, in cells. Below this the header bar and its close button stop being clickable.
MIN_COLS = 2
MIN_ROWS = 2

#: Grab strip along a tile's right/bottom edge that starts a resize instead of hitting the panel.
RESIZE_MARGIN = 7

#: Spare rows kept below the lowest tile, so there is always somewhere to drag a component to.
TRAILING_ROWS = 3

#: How close to the viewport edge a drag has to get before the canvas scrolls itself.
AUTOSCROLL_MARGIN = 28
AUTOSCROLL_STEP = 18
AUTOSCROLL_INTERVAL_MS = 30


# --- geometry: pure, no Qt ------------------------------------------------------------------


@dataclass
class Placement:
    """Where a component sits, in cells. Mutable — `resolve()` pushes copies of these around."""

    col: int
    row: int
    cols: int
    rows: int

    def copy(self):
        return Placement(self.col, self.row, self.cols, self.rows)

    @property
    def bottom(self):
        """First row *below* this placement, i.e. exclusive."""
        return self.row + self.rows

    @property
    def right(self):
        """First column to the *right* of this placement, i.e. exclusive."""
        return self.col + self.cols


def clamp(placement, columns=COLUMNS):
    """Force a placement inside the grid: at least MIN_COLS x MIN_ROWS, never off either side."""
    cols = max(MIN_COLS, min(placement.cols, columns))
    rows = max(MIN_ROWS, placement.rows)
    col = max(0, min(placement.col, columns - cols))
    row = max(0, placement.row)
    return Placement(col, row, cols, rows)


def intersects(a, b):
    return a.col < b.right and b.col < a.right and a.row < b.bottom and b.row < a.bottom


def resolve(placements, pinned):
    """Return placements with no overlaps, `pinned` (an index) left exactly where it is.

    Everything else is settled in reading order and pushed straight down off whatever it hits.
    Rows only ever increase, and each tile is compared against a finite settled set, so the inner
    loop terminates; the guard is there to make that a fact rather than an argument.
    """
    order = sorted(
        range(len(placements)),
        key=lambda i: (i != pinned, placements[i].row, placements[i].col),
    )
    settled = []
    out = [None] * len(placements)
    for index in order:
        candidate = placements[index].copy()
        if index != pinned:
            for _pass in range(len(settled) + 1):
                collision = next((s for s in settled if intersects(candidate, s)), None)
                if collision is None:
                    break
                candidate.row = collision.bottom
        settled.append(candidate)
        out[index] = candidate
    return out


def first_free_slot(placements, cols, rows, columns=COLUMNS):
    """The top-left-most gap a `cols` x `rows` component fits in without displacing anything."""
    cols = max(MIN_COLS, min(cols, columns))
    rows = max(MIN_ROWS, rows)
    limit = max((p.bottom for p in placements), default=0) + 1
    for row in range(limit):
        for col in range(columns - cols + 1):
            candidate = Placement(col, row, cols, rows)
            if not any(intersects(candidate, p) for p in placements):
                return candidate
    return Placement(0, limit, cols, rows)


def _track(width, gap=GAP):
    """The span the columns divide up: the canvas less the margin down its right-hand side."""
    return max(1, width - gap)


def pixel_rect(placement, width, columns=COLUMNS, row_height=ROW_HEIGHT, gap=GAP):
    """Cell placement -> (x, y, w, h) on a canvas `width` px wide.

    Column edges are computed from the placement's own boundaries rather than from a rounded
    per-column width, so rounding error cannot accumulate and two tiles that share a boundary
    always share the same pixel. Every tile is inset by `gap`, including the last column — the
    canvas keeps a margin on both sides, not just the left.
    """
    track = _track(width, gap)
    left = round(placement.col * track / columns) + gap
    right = round(placement.right * track / columns) + gap
    return (
        left,
        placement.row * row_height + gap,
        max(1, right - left - gap),
        max(1, placement.rows * row_height - gap),
    )


def snap_col(x, width, columns=COLUMNS, gap=GAP):
    """Nearest column boundary to pixel `x`. Used for both a drag's left edge and a resize's right.

    The inverse of `pixel_rect`'s column mapping, so a tile dragged one column over lands exactly
    where the guide line was drawn.
    """
    if width <= 0:
        return 0
    return max(0, min(columns, round((x - gap) * columns / _track(width, gap))))


def snap_row(y, row_height=ROW_HEIGHT):
    return max(0, round(y / row_height))


def preferred_cells(size_hint, width, columns=COLUMNS, row_height=ROW_HEIGHT):
    """How many cells a panel wants, from its own size hint. Keeps a chart bigger than a toggle."""
    column_width = max(1.0, width / columns)
    cols = max(MIN_COLS, min(columns, -(-size_hint.width() // int(column_width))))
    rows = max(MIN_ROWS, -(-size_hint.height() // row_height))
    return int(cols), int(rows)


# --- widgets --------------------------------------------------------------------------------


class _TileHeader(QWidget):
    """The drag handle. Owns the title and the close button; forwards its drags to the tile."""

    def __init__(self, tile):
        super().__init__(tile)
        self.tile = tile
        self.setObjectName("gridTileHeader")
        self.setCursor(Qt.OpenHandCursor)
        self.setFixedHeight(26)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 4, 0)
        row.setSpacing(4)
        self.label = QLabel("", self)
        self.label.setObjectName("gridTileTitle")
        row.addWidget(self.label, 1)
        close = QToolButton(self)
        close.setText("✕")
        close.setAutoRaise(True)
        close.setCursor(Qt.ArrowCursor)
        close.setToolTip("Close this component")
        close.clicked.connect(tile.request_close)
        row.addWidget(close, 0)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.tile.begin_gesture("move", event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.tile.gesture_active():
            self.tile.update_gesture(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.tile.gesture_active():
            self.tile.end_gesture()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class GridTile(QFrame):
    """One component on the canvas: header bar, close button, and the panel in a scroll area.

    Exposes `closed` and `visibilityChanged` because that is exactly the contract `PanelBase.
    bind_host()` was written against for `QDockWidget` — a tile substitutes for a dock without
    `PanelBase` needing to know which one it is hosted by.

    The panel is wrapped in a `QScrollArea`. That wrapper is the whole reason a tile can be shrunk:
    docked bare, a panel's content minimum became the tile's hard floor, and several screens have
    minimums wider than a 1080p display.
    """

    #: The tile's X button, or its window closing. Carries the tile so the shell can find it.
    closed = Signal(object)
    #: Whether the operator can actually see this tile — mirrors QDockWidget.visibilityChanged.
    visibilityChanged = Signal(bool)

    def __init__(self, title, canvas):
        super().__init__(canvas.sheet)
        self.canvas = canvas
        self.setObjectName("gridTile")
        self.setFrameShape(QFrame.NoFrame)
        self.setMouseTracking(True)
        self.placement = Placement(0, 0, MIN_COLS, MIN_ROWS)
        self._panel = None
        self._gesture = None
        self._grab = QPoint(0, 0)
        self._host_visible = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, RESIZE_MARGIN, RESIZE_MARGIN)
        root.setSpacing(0)
        self.header = _TileHeader(self)
        self.header.label.setText(title)
        root.addWidget(self.header)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setObjectName("gridTileBody")
        root.addWidget(self._scroll, 1)

    # --- content -----------------------------------------------------------------------

    def set_panel(self, panel):
        self._panel = panel
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._scroll.setWidget(panel)

    def widget(self):
        """The panel, not the scroll area — callers want the thing with `teardown()` on it."""
        return self._panel

    def title(self):
        return self.header.label.text()

    def setTitle(self, text):
        self.header.label.setText(text)

    # --- visibility --------------------------------------------------------------------

    def set_host_visible(self, visible):
        """Called by the canvas. Idempotent, so scrolling costs nothing until the state flips."""
        visible = bool(visible)
        if visible == self._host_visible:
            return
        self._host_visible = visible
        self.visibilityChanged.emit(visible)

    def request_close(self):
        """Ask to be removed. Deliberately not an override of `QWidget.close()`, which only hides."""
        self.closed.emit(self)

    # --- gestures ----------------------------------------------------------------------

    def gesture_active(self):
        return self._gesture is not None

    def _edge_at(self, pos):
        """Which resize edges `pos` is on, as a (right, bottom) pair of bools."""
        return (
            pos.x() >= self.width() - RESIZE_MARGIN,
            pos.y() >= self.height() - RESIZE_MARGIN,
        )

    def mouseMoveEvent(self, event):
        if self._gesture is not None:
            self.update_gesture(event.globalPosition().toPoint())
            event.accept()
            return
        right, bottom = self._edge_at(event.position().toPoint())
        if right and bottom:
            self.setCursor(Qt.SizeFDiagCursor)
        elif right:
            self.setCursor(Qt.SizeHorCursor)
        elif bottom:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        right, bottom = self._edge_at(event.position().toPoint())
        if event.button() == Qt.LeftButton and (right or bottom):
            self.begin_gesture("resize", event.globalPosition().toPoint(), right=right, bottom=bottom)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._gesture is not None:
            self.end_gesture()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if self._gesture is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def begin_gesture(self, mode, global_pos, right=False, bottom=False):
        self._gesture = mode
        origin = self.mapFromGlobal(global_pos)
        self._grab = origin
        self.canvas.begin_gesture(self, mode, global_pos, grab=origin, right=right, bottom=bottom)

    def update_gesture(self, global_pos):
        self.canvas.update_gesture(global_pos)

    def end_gesture(self):
        self._gesture = None
        self.unsetCursor()
        self.canvas.end_gesture()


class _Sheet(QWidget):
    """The scrolled surface tiles are children of. Paints the grid guides and the drop preview."""

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setObjectName("gridSheet")

    def paintEvent(self, event):
        self.canvas.paint_sheet(self, event)


class GridCanvas(QScrollArea):
    """A workspace's grid. Owns its tiles, their placements, and the drag/resize gestures.

    It is a `QScrollArea` rather than something a window has to wrap, because it needs the viewport
    anyway: a tile scrolled out of sight is reported as not visible, which is what keeps a panel
    you cannot see from polling. That property came free with tabbed docks and has to be
    reconstructed here.
    """

    #: A tile's X button was pressed. The shell removes it; the canvas does not self-remove.
    tile_closed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = _Sheet(self)
        self.setWidget(self.sheet)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(_TILE_QSS)

        self.tiles = []
        self._gesture = None
        self._preview = None
        self._empty_hint = QLabel(
            "Empty workspace — add components from the Components menu.\n"
            "Drag a component by its title bar to move it; drag its right or bottom edge to resize.",
            self.sheet,
        )
        self._empty_hint.setObjectName("gridEmptyHint")
        self._empty_hint.setAlignment(Qt.AlignCenter)

        self._autoscroll_by = 0
        self._autoscroll = QTimer(self)
        self._autoscroll.setInterval(AUTOSCROLL_INTERVAL_MS)
        self._autoscroll.timeout.connect(self._autoscroll_tick)

        self.verticalScrollBar().valueChanged.connect(self._sync_visibility)

    # --- tiles -------------------------------------------------------------------------

    def add_tile(self, title, panel, placement=None):
        """Place a panel on the grid. Without a placement it lands in the first gap it fits."""
        tile = GridTile(title, self)
        tile.set_panel(panel)
        tile.placement = clamp(placement) if placement is not None else self._auto_placement(panel)
        tile.closed.connect(self.tile_closed.emit)
        self.tiles.append(tile)
        # Bind BEFORE showing. A PanelBase that is shown while still unbound falls back to its own
        # showEvent and activates itself — and nothing would ever deactivate it again, so a panel
        # placed off the bottom of the canvas would poll forever.
        if hasattr(panel, "bind_host"):
            panel.bind_host(tile)
        tile.show()
        self.relayout()
        return tile

    def _auto_placement(self, panel):
        cols, rows = preferred_cells(panel.sizeHint(), max(1, self._content_width()))
        return first_free_slot([t.placement for t in self.tiles], cols, rows)

    def remove_tile(self, tile):
        if tile not in self.tiles:
            return
        self.tiles.remove(tile)
        tile.setParent(None)
        tile.deleteLater()
        self.relayout()

    def placements(self):
        """Cell placements in tile order — what the shell serializes into a preset."""
        return [tile.placement for tile in self.tiles]

    def reveal(self, tile):
        """Scroll a tile into view. The grid's answer to raising a dock that is already open."""
        if tile in self.tiles:
            self.ensureWidgetVisible(tile, 0, 0)

    # --- layout ------------------------------------------------------------------------

    def _content_width(self):
        return self.viewport().width()

    def relayout(self):
        width = self._content_width()
        bottom = 0
        for tile in self.tiles:
            x, y, w, h = pixel_rect(tile.placement, width)
            tile.setGeometry(x, y, w, h)
            bottom = max(bottom, tile.placement.bottom)
        self.sheet.setMinimumHeight((bottom + TRAILING_ROWS) * ROW_HEIGHT)
        self._empty_hint.setVisible(not self.tiles)
        if not self.tiles:
            self._empty_hint.setGeometry(0, 0, max(1, width), max(1, self.viewport().height()))
        self.sheet.update()
        self._sync_visibility()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_visibility()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_visibility()

    def _viewport_rect(self):
        return QRect(
            self.horizontalScrollBar().value(),
            self.verticalScrollBar().value(),
            self.viewport().width(),
            self.viewport().height(),
        )

    def _sync_visibility(self):
        """A tile is 'visible' only if the canvas is shown and the tile is inside the viewport.

        This is what `PanelBase` subscribes its pollers on. Scrolling a chart off the bottom of a
        workspace releases its poller exactly like tabbing away from it used to.
        """
        shown = self.isVisible()
        rect = self._viewport_rect()
        for tile in self.tiles:
            tile.set_host_visible(shown and rect.intersects(tile.geometry()))

    # --- gestures ----------------------------------------------------------------------

    def begin_gesture(self, tile, mode, global_pos, grab, right=False, bottom=False):
        self._gesture = {
            "tile": tile,
            "mode": mode,
            "grab": grab,
            "right": right,
            "bottom": bottom,
            "pos": global_pos,
        }
        self._preview = tile.placement.copy()
        self.sheet.update()

    def update_gesture(self, global_pos):
        if self._gesture is None:
            return
        self._gesture["pos"] = global_pos
        self._preview = self._preview_for(global_pos)
        self._drive_autoscroll(global_pos)
        self.sheet.update()

    def _preview_for(self, global_pos):
        gesture = self._gesture
        tile = gesture["tile"]
        width = max(1, self._content_width())
        point = self.sheet.mapFromGlobal(global_pos)
        current = tile.placement
        if gesture["mode"] == "move":
            col = snap_col(point.x() - gesture["grab"].x(), width)
            row = snap_row(point.y() - gesture["grab"].y())
            return clamp(Placement(col, row, current.cols, current.rows))
        cols = current.cols
        rows = current.rows
        if gesture["right"]:
            cols = snap_col(point.x(), width) - current.col
        if gesture["bottom"]:
            rows = snap_row(point.y()) - current.row
        return clamp(Placement(current.col, current.row, cols, rows))

    def end_gesture(self):
        if self._gesture is None:
            return
        tile = self._gesture["tile"]
        preview = self._preview
        self._gesture = None
        self._preview = None
        self._autoscroll.stop()
        if preview is not None:
            self.commit(tile, preview)
        else:
            self.sheet.update()

    def commit(self, tile, placement):
        """Apply a placement to `tile` and push whatever it now overlaps downward."""
        index = self.tiles.index(tile)
        proposed = [t.placement for t in self.tiles]
        proposed[index] = clamp(placement)
        for resolved, target in zip(resolve(proposed, index), self.tiles):
            target.placement = resolved
        self.relayout()

    # --- autoscroll --------------------------------------------------------------------

    def _drive_autoscroll(self, global_pos):
        point = self.viewport().mapFromGlobal(global_pos)
        height = self.viewport().height()
        if point.y() < AUTOSCROLL_MARGIN:
            self._autoscroll_by = -AUTOSCROLL_STEP
        elif point.y() > height - AUTOSCROLL_MARGIN:
            self._autoscroll_by = AUTOSCROLL_STEP
        else:
            self._autoscroll.stop()
            return
        if not self._autoscroll.isActive():
            self._autoscroll.start()

    def _autoscroll_tick(self):
        if self._gesture is None:
            self._autoscroll.stop()
            return
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + self._autoscroll_by)
        self.update_gesture(self._gesture["pos"])

    # --- painting ----------------------------------------------------------------------

    def paint_sheet(self, sheet, event):
        """Column guides while a gesture is running, plus the drop preview. Idle canvas: nothing."""
        if self._gesture is None or self._preview is None:
            return
        painter = QPainter(sheet)
        width = max(1, self._content_width())
        guide = QColor(self.palette().highlight().color())
        guide.setAlpha(40)
        painter.setPen(QPen(guide, 1, Qt.DashLine))
        for column in range(1, COLUMNS):
            # Same mapping as pixel_rect, so a guide line is exactly where a tile edge will land.
            x = pixel_rect(Placement(column, 0, 1, 1), width)[0]
            painter.drawLine(x, 0, x, sheet.height())

        x, y, w, h = pixel_rect(self._preview, width)
        fill = QColor(self.palette().highlight().color())
        fill.setAlpha(60)
        painter.setPen(QPen(self.palette().highlight().color(), 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRect(x, y, w, h), 4, 4)
        painter.end()


#: Palette roles, not hex — the tile chrome then follows a theme switch with no work from
#: `desktop/theme.py`, which owns colours that QSS cannot express as a role.
_TILE_QSS = """
#gridTile {
    background: palette(window);
    border: 1px solid palette(mid);
    border-radius: 5px;
}
#gridTileHeader {
    background: palette(alternate-base);
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    border-bottom: 1px solid palette(mid);
}
#gridTileTitle {
    font-weight: 600;
}
#gridEmptyHint {
    color: palette(mid);
}
"""
