"""The workspace grid's geometry — pure cell arithmetic, no Qt, no QApplication.

Widget behaviour (a tile scrolled out of the viewport releasing its pollers, a tile's X button
removing its component) is asserted in `test_desktop_screens.py`, which has the offscreen Qt
fixture. What is here is the layout algorithm itself: where a component lands, what a drop does to
what was already there, and what a placement means in pixels.
"""

from desktop.grid import (
    COLUMNS,
    GAP,
    MIN_COLS,
    MIN_ROWS,
    ROW_HEIGHT,
    Placement,
    clamp,
    first_free_slot,
    intersects,
    pixel_rect,
    resolve,
    snap_col,
    snap_row,
)

# --- clamping -------------------------------------------------------------------------------


def test_clamp_pulls_a_placement_back_inside_the_right_edge():
    # Dragged past the right edge: it slides back, it does not get narrower.
    clamped = clamp(Placement(col=10, row=2, cols=4, rows=3))

    assert clamped.right == COLUMNS
    assert clamped.cols == 4
    assert clamped.row == 2


def test_clamp_enforces_a_floor_below_which_the_header_is_unusable():
    clamped = clamp(Placement(col=0, row=0, cols=0, rows=0))

    assert (clamped.cols, clamped.rows) == (MIN_COLS, MIN_ROWS)


def test_clamp_refuses_a_negative_row():
    assert clamp(Placement(col=0, row=-5, cols=3, rows=3)).row == 0


def test_clamp_caps_width_at_the_full_grid():
    clamped = clamp(Placement(col=0, row=0, cols=99, rows=2))

    assert clamped.cols == COLUMNS
    assert clamped.col == 0


# --- overlap --------------------------------------------------------------------------------


def test_neighbours_that_share_an_edge_do_not_intersect():
    left = Placement(0, 0, 6, 4)
    right = Placement(6, 0, 6, 4)
    below = Placement(0, 4, 6, 4)

    assert not intersects(left, right)
    assert not intersects(left, below)


def test_a_one_cell_bleed_counts_as_an_overlap():
    assert intersects(Placement(0, 0, 6, 4), Placement(5, 3, 6, 4))


# --- resolve: what a drop does to what was already there --------------------------------------


def test_a_drop_pushes_what_it_lands_on_downward_and_stays_put_itself():
    existing = Placement(0, 0, 12, 4)
    dropped = Placement(0, 0, 6, 3)  # dropped straight on top of `existing`

    resolved = resolve([existing, dropped], pinned=1)

    assert (resolved[1].col, resolved[1].row) == (0, 0)  # pinned, exactly where it was dropped
    assert resolved[0].row == 3  # pushed clear of the dropped tile's bottom edge
    assert not intersects(resolved[0], resolved[1])


def test_a_push_cascades_through_a_stack():
    """Displacing the top of a stack has to move everything under it, not just the first hit."""
    stack = [Placement(0, 0, 12, 2), Placement(0, 2, 12, 2), Placement(0, 4, 12, 2)]
    dropped = Placement(0, 0, 12, 3)

    resolved = resolve([*stack, dropped], pinned=3)

    assert resolved[3].row == 0  # the drop itself is pinned
    assert [p.row for p in resolved[:3]] == [3, 5, 7]  # pushed down, order preserved
    for i in range(len(resolved)):
        for j in range(i + 1, len(resolved)):
            assert not intersects(resolved[i], resolved[j])


def test_a_tile_beside_the_drop_is_left_alone():
    """Pushing is vertical only. A neighbour in other columns must not be disturbed."""
    beside = Placement(6, 0, 6, 4)
    dropped = Placement(0, 0, 6, 4)

    resolved = resolve([beside, dropped], pinned=1)

    assert resolved[0] == beside


def test_nothing_is_compacted_upward():
    """A tile stays where it was put even with empty space above it."""
    lonely = Placement(0, 20, 4, 4)
    dropped = Placement(8, 0, 4, 4)

    resolved = resolve([lonely, dropped], pinned=1)

    assert resolved[0].row == 20


def test_resolve_leaves_an_already_valid_layout_untouched():
    layout = [Placement(0, 0, 6, 4), Placement(6, 0, 6, 4), Placement(0, 4, 12, 3)]

    assert resolve(layout, pinned=2) == layout


# --- auto-placement -------------------------------------------------------------------------


def test_the_first_component_lands_in_the_top_left_corner():
    assert first_free_slot([], 6, 4) == Placement(0, 0, 6, 4)


def test_a_second_component_lands_beside_the_first_not_under_it():
    """The dock shell stacked everything full-width in one column. This is the fix, asserted."""
    placed = [Placement(0, 0, 6, 4)]

    slot = first_free_slot(placed, 6, 4)

    assert slot == Placement(6, 0, 6, 4)


def test_a_component_too_wide_for_the_remaining_gap_starts_a_new_row():
    placed = [Placement(0, 0, 8, 4)]

    slot = first_free_slot(placed, 8, 4)

    assert slot.col == 0
    assert slot.row >= 4


def test_auto_placement_fills_a_hole_left_by_a_closed_component():
    placed = [Placement(6, 0, 6, 4), Placement(0, 4, 12, 4)]

    assert first_free_slot(placed, 6, 4) == Placement(0, 0, 6, 4)


def test_auto_placement_never_returns_an_overlapping_slot():
    placed = []
    for _ in range(9):
        slot = first_free_slot(placed, 4, 3)
        assert all(not intersects(slot, p) for p in placed)
        placed.append(slot)


# --- pixels ---------------------------------------------------------------------------------


def test_tiles_sharing_a_column_boundary_share_a_pixel_edge():
    """Rounding a per-column width instead would let a 1px seam drift across the canvas."""
    width = 1279  # deliberately not divisible by 12
    left = pixel_rect(Placement(0, 0, 7, 4), width)
    right = pixel_rect(Placement(7, 0, 5, 4), width)

    assert left[0] + left[2] + GAP == right[0]


def test_a_full_width_tile_is_inset_on_both_sides_not_just_the_left():
    x, _y, w, _h = pixel_rect(Placement(0, 0, COLUMNS, 3), 1200)

    assert x == GAP
    assert x + w == 1200 - GAP


def test_row_height_is_constant_but_column_width_follows_the_canvas():
    narrow = pixel_rect(Placement(0, 0, 6, 4), 600)
    wide = pixel_rect(Placement(0, 0, 6, 4), 1200)

    assert narrow[3] == wide[3] == 4 * ROW_HEIGHT - GAP
    assert wide[2] > narrow[2]


def test_snapping_rounds_to_the_nearest_boundary_not_down_to_it():
    width = COLUMNS * 100 + GAP  # exactly 100px per column, boundary k at 100k + GAP
    assert snap_col(100 + GAP, width) == 1  # dead on a boundary
    assert snap_col(155, width) == 1  # just under the halfway point to the next one
    assert snap_col(157, width) == 2  # just over it
    assert snap_row(ROW_HEIGHT - 1) == 1


def test_snapping_is_the_inverse_of_the_pixel_mapping():
    """A tile dragged onto a guide line must land on that guide line, at every column."""
    width = 1279  # deliberately not divisible by 12
    for column in range(COLUMNS + 1):
        boundary = round(column * (width - GAP) / COLUMNS) + GAP
        assert snap_col(boundary, width) == column


def test_snapping_cannot_escape_the_grid():
    assert snap_col(-500, 1200) == 0
    assert snap_col(99999, 1200) == COLUMNS
    assert snap_row(-40) == 0


def test_snap_col_survives_a_zero_width_canvas():
    """A window restored before it is shown has no width yet; that must not divide by zero."""
    assert snap_col(120, 0) == 0
