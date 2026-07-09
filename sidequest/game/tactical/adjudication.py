"""Pure tactical-grid adjudication over an ASCII mask (ADR-096 v2, Track C1).

The mask is truth: '#'=wall, '.'=floor, rows newline-separated (the
``TacticalGridPayload.mask`` shape). Every function is pure — no IO, no clock,
no random — and works in CELL units. The ruleset binding (Track C2/C3) supplies
the cell scale (metres per cell) and per-actor movement budgets; this library
never knows a ruleset. Coordinate convention matches ``dungeon/tactical.py`` and
the UI ``cellMath.ts``: cell = (x, y), x is column, y is row, origin top-left.
"""

from __future__ import annotations

from dataclasses import dataclass

Cell = tuple[int, int]

FLOOR_CHAR = "."
WALL_CHAR = "#"

_STEPS: tuple[Cell, ...] = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)


def parse_mask(mask: str) -> list[str]:
    """Split a newline mask into rows — the canonical grid form for this module."""
    return mask.split("\n")


def in_bounds(rows: list[str], cell: Cell) -> bool:
    x, y = cell
    return 0 <= y < len(rows) and 0 <= x < len(rows[y])


def is_floor(rows: list[str], cell: Cell) -> bool:
    """True iff ``cell`` is in-bounds and a floor ('.') cell."""
    if not in_bounds(rows, cell):
        return False
    x, y = cell
    return rows[y][x] == FLOOR_CHAR


def chebyshev_distance(a: Cell, b: Cell) -> int:
    """King-move distance: ``max(|dx|, |dy|)``. Diagonals cost as orthogonals."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def neighbors(rows: list[str], cell: Cell) -> list[Cell]:
    """The 8 king-move floor neighbours of ``cell`` (walls/off-map excluded)."""
    x, y = cell
    out: list[Cell] = []
    for dx, dy in _STEPS:
        n = (x + dx, y + dy)
        if is_floor(rows, n):
            out.append(n)
    return out


@dataclass(frozen=True)
class ReachResult:
    """Cells an actor can stop on within a movement budget, plus min cost each."""

    reachable: frozenset[Cell]
    cost: dict[Cell, int]


def cells_reachable(
    origin: Cell,
    budget: int,
    mask: str,
    *,
    difficult: frozenset[Cell] = frozenset(),
) -> ReachResult:
    """Dijkstra flood over floor cells from ``origin`` honouring walls and a
    difficult-terrain set (entering a difficult cell costs 2, else 1). ``budget``
    is movement in cells. Zero/negative budget or a non-floor origin -> empty."""
    rows = parse_mask(mask)
    if budget <= 0 or not is_floor(rows, origin):
        return ReachResult(frozenset(), {})
    cost: dict[Cell, int] = {origin: 0}
    frontier: list[Cell] = [origin]
    while frontier:
        # Grids are room-scale; a linear min-scan is simplest and deterministic.
        frontier.sort(key=lambda c: cost[c])
        cur = frontier.pop(0)
        cur_cost = cost[cur]
        for n in neighbors(rows, cur):
            step = 2 if n in difficult else 1
            nc = cur_cost + step
            if n not in cost or nc < cost[n]:
                # Record the true min cost for every relaxed cell — including the
                # immediate over-budget frontier — but only expand cells within
                # budget (an over-budget cell is a boundary, not a stepping stone).
                cost[n] = nc
                if nc <= budget:
                    frontier.append(n)
    reachable = frozenset(c for c in cost if c != origin and cost[c] <= budget)
    return ReachResult(reachable, cost)


def movement_cost(
    path: list[Cell],
    mask: str,
    *,
    difficult: frozenset[Cell] = frozenset(),
) -> int | None:
    """Cost in cells of walking ``path`` (adjacent floor cells, origin first).
    ``None`` if empty, or any step is non-adjacent / off-floor. A single floor
    cell is a zero-cost no-move. Entering a difficult cell costs 2, else 1."""
    rows = parse_mask(mask)
    if not path:
        return None
    if len(path) == 1:
        return 0 if is_floor(rows, path[0]) else None
    total = 0
    for a, b in zip(path, path[1:], strict=False):
        if not is_floor(rows, a) or not is_floor(rows, b):
            return None
        if chebyshev_distance(a, b) != 1:
            return None
        total += 2 if b in difficult else 1
    return total


@dataclass(frozen=True)
class MoveAdjudication:
    """A ruleset-neutral verdict on one move. ``reason`` is '' when valid."""

    valid: bool
    cells_spent: int
    cells_budget: int
    reason: str


def adjudicate_move(
    *,
    origin: Cell,
    path: list[Cell],
    budget_cells: int,
    mask: str,
    difficult: frozenset[Cell] = frozenset(),
) -> MoveAdjudication:
    """Adjudicate ``path`` against ``budget_cells``. A malformed path is INVALID
    with cells_spent=0 and a legible reason; an over-budget path is INVALID and
    reports the real cost so the denial can read 'that is N cells, you can move
    M'. Never silently corrects."""
    if not path or path[0] != origin:
        return MoveAdjudication(False, 0, budget_cells, "move path must start at the actor's cell")
    cost = movement_cost(path, mask, difficult=difficult)
    if cost is None:
        return MoveAdjudication(
            False, 0, budget_cells, "illegal move path (crosses a wall or skips a cell)"
        )
    if cost > budget_cells:
        return MoveAdjudication(
            False,
            cost,
            budget_cells,
            f"that move is {cost} cells; you can move {budget_cells} "
            f"cell{'s' if budget_cells != 1 else ''} this turn",
        )
    return MoveAdjudication(True, cost, budget_cells, "")


def reach_cells(origin: Cell, reach: int, mask: str) -> frozenset[Cell]:
    """Floor cells within Chebyshev ``reach`` of ``origin`` (origin excluded).
    Reach is a radius, not a path — walls are excluded from the set but do not
    block (that is what LOS is for). Melee reach = 1."""
    if reach <= 0:
        return frozenset()
    rows = parse_mask(mask)
    ox, oy = origin
    out: set[Cell] = set()
    for y in range(oy - reach, oy + reach + 1):
        for x in range(ox - reach, ox + reach + 1):
            c = (x, y)
            if c != origin and is_floor(rows, c) and chebyshev_distance(origin, c) <= reach:
                out.add(c)
    return frozenset(out)


def _ray_cells(a: Cell, b: Cell) -> list[Cell]:
    """Bresenham cells from ``a`` to ``b`` inclusive (integer supercover-lite)."""
    x0, y0 = a
    x1, y1 = b
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    cells = [(x, y)]
    while (x, y) != (x1, y1):
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy
        cells.append((x, y))
    return cells


def line_of_sight(a: Cell, b: Cell, mask: str) -> bool:
    """True iff no wall lies strictly between ``a`` and ``b`` on the ray.
    Endpoints are not tested (a token may stand at cover's edge); off-map is
    blocking."""
    rows = parse_mask(mask)
    ray = _ray_cells(a, b)
    return all(is_floor(rows, c) for c in ray[1:-1])


@dataclass(frozen=True)
class RangeAdjudication:
    """A ruleset-neutral verdict on whether ``target`` is attackable from
    ``origin``. ``mode`` is 'melee' | 'ranged'; ``reason`` is '' when in range."""

    in_range: bool
    distance_cells: int
    max_cells: int
    mode: str
    has_los: bool
    reason: str


def adjudicate_reach(
    *,
    origin: Cell,
    target: Cell,
    max_cells: int,
    mask: str,
    mode: str,
    require_los: bool,
) -> RangeAdjudication:
    """Adjudicate whether ``target`` is within ``max_cells`` (Chebyshev) and,
    when ``require_los``, has clear line of sight. Reports the real distance so
    a denial reads 'target is N cells away; your reach is M'. Never silently
    corrects."""
    if mode not in ("melee", "ranged"):
        raise ValueError(
            f"mode must be 'melee' or 'ranged', got {mode!r} "
            "(unknown modes used to fall through to the 'range' noun silently — "
            "No Silent Fallbacks, 165-1 carryover)"
        )
    dist = chebyshev_distance(origin, target)
    has_los = line_of_sight(origin, target, mask) if require_los else True
    if dist > max_cells:
        noun = "reach" if mode == "melee" else "range"
        return RangeAdjudication(
            False,
            dist,
            max_cells,
            mode,
            has_los,
            f"target is {dist} cells away; your {noun} is {max_cells}",
        )
    if require_los and not has_los:
        return RangeAdjudication(
            False, dist, max_cells, mode, has_los, "no line of sight to the target"
        )
    return RangeAdjudication(True, dist, max_cells, mode, has_los, "")


def aoe_burst(center: Cell, radius: int, mask: str, *, require_los: bool = True) -> frozenset[Cell]:
    """Floor cells within Chebyshev ``radius`` of ``center``, LOS-gated from the
    centre (walls shadow cells behind them). ``center`` included when floor."""
    rows = parse_mask(mask)
    cx, cy = center
    out: set[Cell] = set()
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            c = (x, y)
            if not is_floor(rows, c) or chebyshev_distance(center, c) > radius:
                continue
            if require_los and c != center and not line_of_sight(center, c, mask):
                continue
            out.add(c)
    return frozenset(out)


def aoe_line(origin: Cell, target: Cell, mask: str) -> frozenset[Cell]:
    """Floor cells along the ray origin->target until a wall stops it (the wall
    cell is excluded). A beam/line template."""
    rows = parse_mask(mask)
    out: set[Cell] = set()
    for c in _ray_cells(origin, target):
        if not is_floor(rows, c):
            break
        out.add(c)
    return frozenset(out)
