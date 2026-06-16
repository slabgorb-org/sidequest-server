"""Course computation — selection, cost, validation.

Pure module: no I/O, no global state. Deterministic given its inputs.
The renderer (course_render.py) and handler (handlers/course_intent.py)
import from here; nothing imports those upward.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from sidequest.orbital.course_geometry import chord_angular_distance_deg
from sidequest.orbital.models import BodyDef, OrbitsConfig

if TYPE_CHECKING:
    from sidequest.orbital.render import Scope

# Calibration constants — tuned so Far Landing → Tethys Watch ≈ 12h,
# Far Landing → The Gate ≈ 90h. See cost-model section of the design.
TRAVEL_HOURS_PER_AU = 30.0
DELTA_V_BASE = 0.7  # km/s per AU of total chord distance
DELTA_V_RADIAL_FACTOR = 0.4  # extra Δv per AU of radial (semi-major-axis) diff


def compute_eta_and_dv(
    party_at: BodyDef,
    dest: BodyDef,
    orbits: OrbitsConfig,
) -> tuple[float, float]:
    """Hohmann-flavored cost. NOT real orbital mechanics.

    Returns ``(eta_hours, delta_v_km_per_s)``. Both 0.0 when the two
    bodies are identical references (same physical body, same phase).

    Inputs:
    - ``party_at``, ``dest``: ``BodyDef`` instances from the world's
      orbits.yaml. Either may be a moon (parent != system root); we
      treat ``semi_major_au`` as a flat distance proxy regardless.
    - ``orbits``: needed for ``travel.travel_speed_factor``.
    """
    if party_at is dest:
        return 0.0, 0.0
    a1 = party_at.semi_major_au or 0.0
    a2 = dest.semi_major_au or 0.0
    radial_au = abs(a1 - a2)
    phase_a = party_at.epoch_phase_deg or 0.0
    phase_b = dest.epoch_phase_deg or 0.0
    angular_au = 0.05 * (chord_angular_distance_deg(phase_a, phase_b) / 360.0)
    chord_au = radial_au + angular_au
    eta_hours = (chord_au * TRAVEL_HOURS_PER_AU) / orbits.travel.travel_speed_factor
    delta_v = chord_au * DELTA_V_BASE + radial_au * DELTA_V_RADIAL_FACTOR
    return eta_hours, delta_v


class CourseSource(StrEnum):
    """Why a course was offered. Drives the 12-cap priority ordering."""

    IN_SCOPE = "in_scope"
    RECENT_MENTION = "recent_mention"
    QUEST_OBJECTIVE = "quest_objective"

    @property
    def priority(self) -> int:
        """Higher = keep when capping. Quest > recent > in-scope."""
        return _SOURCE_PRIORITY[self]


_SOURCE_PRIORITY: dict[CourseSource, int] = {
    CourseSource.IN_SCOPE: 1,
    CourseSource.RECENT_MENTION: 2,
    CourseSource.QUEST_OBJECTIVE: 3,
}


class CourseRow(BaseModel):
    """One precomputed course exposed to narrator + GM panel.

    Labelled "row" because the prompt block renders these as one bullet
    each. Distinct from PlottedCourse, which is the snapshot field
    representing the *committed* (well, plotted) course.
    """

    model_config = ConfigDict(extra="forbid")

    to_body_id: str
    eta_hours: float
    delta_v: float
    source: CourseSource
    label_hint: str | None = None  # quest objective name when source=QUEST_OBJECTIVE


class PlottedCourse(BaseModel):
    """The snapshot's persistent course state — drawn on the chart.

    Cleared by replace, cancel, or arrival (party_body_id == to_body_id).
    Survives save/load and WebSocket disconnect by virtue of being a
    snapshot field.
    """

    model_config = ConfigDict(extra="forbid")

    to_body_id: str
    label: str | None = None
    eta_hours: float
    delta_v: float
    plotted_at_t_hours: float
    source: CourseSource


COURSES_HARD_CAP = 12
"""Token-budget guardrail. ~12 entries × ~20 tokens each = ~250 tokens
of <courses> block, well under what we can afford in the Recency zone.
If selection exceeds 12, drop in priority order keeping the highest."""


def compute_courses(
    *,
    orbits: OrbitsConfig,
    party_at: str | None,
    in_scope_body_ids: set[str],
    recent_body_mentions: list[str],
    quest_anchors: list[str],
) -> dict[str, CourseRow]:
    """Build the <courses> selection for one prompt assembly.

    Selection rule: a body is included if it appears in any of
    ``in_scope_body_ids``, ``recent_body_mentions``, or
    ``quest_anchors``. Source priority resolves multi-membership:
    quest > recent > in_scope.

    Hard cap: 12 entries. Drops are applied in *reverse* priority
    order, so quest objectives and recent mentions are preserved at
    the expense of in-scope-only bodies.

    Determinism: dict iteration order is sorted by
    (priority desc, eta_hours asc, body_id asc).

    Returns ``{}`` if ``party_at`` is None or unknown — there's no
    place to plot from.
    """
    if party_at is None or party_at not in orbits.bodies:
        return {}

    party_body = orbits.bodies[party_at]

    candidates: dict[str, CourseSource] = {}
    # Lowest priority first; later writes override.
    for bid in in_scope_body_ids:
        if bid != party_at and bid in orbits.bodies:
            candidates[bid] = CourseSource.IN_SCOPE
    for bid in recent_body_mentions:
        if bid != party_at and bid in orbits.bodies:
            candidates[bid] = CourseSource.RECENT_MENTION
    for bid in quest_anchors:
        if bid != party_at and bid in orbits.bodies:
            candidates[bid] = CourseSource.QUEST_OBJECTIVE

    rows: list[tuple[str, CourseRow]] = []
    for bid, source in candidates.items():
        eta, dv = compute_eta_and_dv(party_body, orbits.bodies[bid], orbits)
        rows.append(
            (
                bid,
                CourseRow(
                    to_body_id=bid,
                    eta_hours=eta,
                    delta_v=dv,
                    source=source,
                    label_hint=None,
                ),
            )
        )

    # Sort: priority desc, then eta asc, then body_id asc for stability.
    rows.sort(key=lambda kv: (-kv[1].source.priority, kv[1].eta_hours, kv[0]))

    if len(rows) > COURSES_HARD_CAP:
        rows = rows[:COURSES_HARD_CAP]

    return dict(rows)


def format_courses_block(rows: dict[str, CourseRow]) -> str:
    """Render the <courses> prompt block from a compute_courses output.

    Empty input → empty string (caller skips registering the section).

    The block contains the narrator instruction and one bullet per
    course. Format is engineered for one-shot Claude parsing: the
    instruction is unambiguous, each bullet is one line, body_ids are
    snake_case so a body_id token cannot collide with prose.
    """
    if not rows:
        return ""

    lines: list[str] = ["<courses>"]
    lines.append(
        "You can plot a course to any of these. When the player asks to plot "
        'a course ("plot a course to X", "Kestrel, lay in a course for X", '
        '"take us to X"), include the matching course_id in your '
        "game_patch sidecar:"
    )
    lines.append('  {"intent":"plot_course","course_id":"<id>"}')
    lines.append("")
    lines.append(
        "If the player asks for a destination not in this list, say so "
        "in-fiction (\"Kestrel can't lock that, captain — say a body within "
        'scanner range or a known objective"). Do NOT invent course_ids.'
    )
    lines.append("")
    for body_id, row in rows.items():
        suffix = ""
        if row.source == CourseSource.QUEST_OBJECTIVE:
            label = row.label_hint or body_id
            suffix = f" — quest: {label}"
        elif row.source == CourseSource.RECENT_MENTION:
            suffix = " — recently mentioned"
        # IN_SCOPE: no suffix.
        lines.append(f"- {body_id} (ETA {row.eta_hours:.0f}h, Δv {row.delta_v:.1f}){suffix}")
    lines.append("</courses>")
    return "\n".join(lines)


def _bodies_in_scope(orbits: OrbitsConfig, scope: Scope) -> set[str]:
    """Body ids visible in the current OrbitalIntent scope.

    System-root scope: the system primary body PLUS all of its direct
    children (bodies whose parent is the primary). Drilled-in scope:
    the center body PLUS its direct children (parent == center).

    Mirrors the existing render_chart scope semantics: _viewport_for_scope
    draws direct children of the center body; _draw_body includes the
    center itself. Consistent with Scope.system_root() → center_body_id
    == "<root>" sentinel.
    """
    if scope.center_body_id == "<root>":
        # Find the single parent-less body (the system primary).
        primaries = [bid for bid, b in orbits.bodies.items() if b.parent is None]
        if not primaries:
            return set()
        primary_id = primaries[0]
        # System root: include the primary and all of its direct children.
        return {primary_id} | {bid for bid, b in orbits.bodies.items() if b.parent == primary_id}
    # Drilled-in: center body plus its direct children.
    return {scope.center_body_id} | {
        bid for bid, b in orbits.bodies.items() if b.parent == scope.center_body_id
    }
