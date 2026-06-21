"""RED wiring tests — Story 153-5 — the ``course`` (course/clock) subsystem.

Playtest finding ``[SWN-ORBITAL-COURSE-INERT]``: ADR-130's orbital story-time
*Clock* and *Course* model already exist in ``sidequest/orbital/`` (``course.py``
``compute_eta_and_dv`` / ``PlottedCourse``; ``beats.py`` ``advance_clock_via_beat``
/ ``StoryBeatKind.TRAVEL``; ``clock.py`` ``Clock.advance``) — but the only way to
engage them in play is the narrator's *post-hoc* ``plot_course`` game_patch
sidecar (``narration_apply.py::_apply_course_sidecar``) plus a ``<courses>``
prompt block. There is **no pre-narrator IntentRouter dispatch-bank route**, so a
player who says "we burn for the Red Prospect" never deterministically engages
the course/clock engine: the narrator improvises, the story clock stays stale.

This story wires ADR-130 to the ADR-113/123 mechanical-engagement spine. As with
Story 117-3 (``quest_offer``) and Story 105-2 (the movement seam), we add ONE
subsystem, ``course``, registered in the dispatch ``_REGISTRY`` and reachable
through the REAL ``run_dispatch_bank``. On a high-confidence travel/course intent
it engages BOTH halves of ADR-130 in a single dispatch (the SOUL "Cut the Dull
Bits" fast-travel): it computes the course (course model) AND advances the
story-time clock by the travel beat (clock model).

**Router is STUBBED.** A real intent-router LLM pass is flaky (project lore,
``feedback_no_content_coupled_tests``); the router's classification *is* the input
to this layer, so we inject it deterministically by constructing the
``SubsystemDispatch`` (``subsystem="course"``, ``params={"destination": <body>}``,
``confidence``) and driving the REAL bank / pre-pass. No LLM runs.

Contract under test (TEA-defined for Dev), from ADR-130 + ADR-113/123:

* ``sidequest/agents/subsystems/course.py`` exports ``run_course_dispatch`` and it
  is registered in ``_REGISTRY`` under ``"course"`` (``_register_defaults``).
* THE WIRING TEST (mandatory): a course dispatch through the REAL bank →
  ``compute_eta_and_dv`` fires (``course.plot`` span, course committed) AND the
  story clock advances by a TRAVEL beat of ``duration_hours == eta_hours``
  (``clock.advance`` span, ``snapshot.clock_t_hours`` += eta). Proves the
  subsystem is connected end-to-end, not that the helpers work in isolation.
* The per-subsystem confidence gate (default 0.6) protects the engine: a
  below-threshold course intent degrades to a narrator hint and engages nothing.
* No Silent Fallbacks: a destination naming a body NOT in ``orbits.bodies`` is
  rejected LOUD (``course.plot.rejected`` span); no phantom ``PlottedCourse``.
* The engagement watcher has a ``course`` witness (added to ``_WITNESSES`` AND
  ``_DISPATCHED_TYPE_KEY``) that flags ``dispatch_engagement.course.mismatch``
  when the router dispatched ``course`` but neither a plotted course nor an
  arrival landed (router-claimed-but-engine-idle).
* The PRODUCTION pre-pass (``execute_intent_router_pre_narrator_pass``) threads
  the world's ``orbital_content`` into the bank context so the handler is
  reachable in real play — ``orbital_content`` lives on the ``Session``, NOT the
  ``GameSnapshot``, and today's bank context (``intent_router_pass.py``) does NOT
  include it. Asserting only via a hand-built bank context would mask this gap
  (the "opposed-check wiring trap", project memory
  ``project_opposed_check_wiring_trap``).

These import / look up names that do not exist yet (the handler, the registry
entry, the witness, the ``orbital_content`` pre-pass param). Collection passes
but the assertions fail until Dev (153-5 GREEN) lands every wiring connection.
That is the intended RED. Wiring is proven by behavior + OTEL spans + the
registry — never a source-text grep (CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.orbital.course import compute_eta_and_dv
from sidequest.orbital.loader import load_orbital_content
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# world_minimal orbital fixture: coyote (star) → red_prospect (companion,
# semi_major_au 1.2) → turning_hub (habitat moon of red_prospect, 0.04).
# Reused from tests/orbital/test_quest_anchors_course_wiring.py.
_WORLD_MINIMAL = Path(__file__).resolve().parents[2] / "orbital" / "fixtures" / "world_minimal"

_PARTY_BODY = "turning_hub"
_DEST_BODY = "red_prospect"
_INITIAL_CLOCK_HOURS = 100.0

SPAN_COURSE_PLOT = "course.plot"
SPAN_COURSE_PLOT_REJECTED = "course.plot.rejected"
SPAN_CLOCK_ADVANCE = "clock.advance"


# ---------------------------------------------------------------------------
# Builders — synthetic only (the STUBBED router output)
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _course_dispatch(
    *,
    destination: str = _DEST_BODY,
    confidence: float = 0.9,
    idempotency_key: str = "k-course-1",
) -> SubsystemDispatch:
    """The STUBBED router output: a deterministic ``course`` classification.

    Stands in for the IntentRouter's Haiku pass. ``params["destination"]`` names
    the body the player wants to reach (a body id surfaced in the world's
    bodies / the ``<courses>`` block); the engine resolves it against
    ``orbits.bodies`` and computes ETA/Δv. No LLM runs.
    """
    return SubsystemDispatch(
        subsystem="course",
        params={"destination": destination},
        idempotency_key=idempotency_key,
        confidence=confidence,
        visibility=_open_viz(),
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Rux",
                raw_action="We burn for the Red Prospect.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _orbital_content():
    return load_orbital_content(_WORLD_MINIMAL)


def _snap() -> GameSnapshot:
    """A snapshot anchored at turning_hub with a non-zero story clock.

    ``red_prospect`` is added as a quest anchor so the course resolves whether
    Dev resolves the destination directly against ``orbits.bodies`` or via
    ``compute_courses`` (where a quest anchor is surfaced regardless of scope).
    """
    return GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        party_body_id=_PARTY_BODY,
        clock_t_hours=_INITIAL_CLOCK_HOURS,
        quest_anchors=[_DEST_BODY],
    )


def _bank_context(snap: GameSnapshot, content) -> dict:
    """The context the PRODUCTION caller must thread for the course subsystem.

    ``snapshot`` + ``pack`` are already threaded today; ``orbital_content`` is
    the NET-NEW key this story adds (the orbits config the course engine needs).
    """
    return {
        "snapshot": snap,
        "pack": MagicMock(),
        "player_name": "Rux",
        "orbital_content": content,
    }


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _course_committed(snap: GameSnapshot) -> bool:
    """The course engaged — robust to the plot-persists vs immediate-arrival fork.

    Either ``plotted_course`` carries the destination (course committed, not yet
    arrived) OR the party has arrived (``party_body_id`` == destination).
    """
    plotted = snap.plotted_course is not None and snap.plotted_course.to_body_id == _DEST_BODY
    arrived = snap.party_body_id == _DEST_BODY
    return plotted or arrived


# ---------------------------------------------------------------------------
# Registration — the handler is reachable from the dispatch bank
# ---------------------------------------------------------------------------


def test_course_handler_is_registered() -> None:
    """The bank's _REGISTRY must carry course → run_course_dispatch.

    Without registration, ``_REGISTRY.get('course')`` is None: the pre-pass
    unregistered-gate (``gate_unregistered_subsystems``) drops the dispatch and
    the bank would log ``unknown_subsystem`` — the course/clock engine never
    fires and the narrator silently improvises the burn.
    """
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "course" in registry, (
        f"course handler not registered; bank has {sorted(registry)}. "
        "Story 153-5 must add the registration in "
        "sidequest/agents/subsystems/__init__.py:_register_defaults()."
    )
    fn = registry["course"]
    assert callable(fn) and getattr(fn, "__name__", "") == "run_course_dispatch", (
        f"registered course handler should be run_course_dispatch; got {fn!r}"
    )


# ---------------------------------------------------------------------------
# THE WIRING TEST (mandatory) — course intent → plot + clock advance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_intent_through_bank_plots_and_advances_clock(otel_capture) -> None:
    """End-to-end through the REAL dispatch bank: a high-confidence course intent
    engages BOTH halves of ADR-130 — the course model AND the story clock.

    This is the load-bearing wiring proof. It asserts:
      * the bank ENGAGED the subsystem (decision == "engaged", not
        "unknown_subsystem");
      * the COURSE model fired — a ``course.plot`` span AND a committed course
        (plotted, or arrived) to the destination;
      * the CLOCK model fired — a ``clock.advance`` span AND
        ``snapshot.clock_t_hours`` advanced by the computed ETA (a TRAVEL beat
        of ``duration_hours == eta_hours``).
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    eta_hours, _dv = compute_eta_and_dv(
        content.orbits.bodies[_PARTY_BODY],
        content.orbits.bodies[_DEST_BODY],
        content.orbits,
    )
    assert eta_hours > 0.0, "fixture sanity: turning_hub→red_prospect must cost > 0h"

    snap = _snap()
    package = _package_with(_course_dispatch(confidence=0.9))

    result = await run_dispatch_bank(package, context=_bank_context(snap, content))

    # The bank engaged the subsystem (not gated, not unknown).
    course_decisions = [d for d in result.decisions if d["subsystem"] == "course"]
    assert course_decisions, "bank recorded no decision for the course dispatch"
    assert course_decisions[-1]["decision"] == "engaged", (
        "the course dispatch did not engage the engine "
        f"(decision={course_decisions[-1]['decision']!r}) — handler unregistered, "
        "signature mismatch, or no-op'd silently"
    )

    # Course model engaged: the engine plotted to the destination.
    assert _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "course.plot span did not fire — the course model (compute_eta_and_dv / "
        "PlottedCourse) never engaged through the bank"
    )
    assert _course_committed(snap), (
        "the course was not committed: neither plotted_course nor party arrival "
        f"reflects {_DEST_BODY!r} (plotted_course={snap.plotted_course!r}, "
        f"party_body_id={snap.party_body_id!r})"
    )

    # Clock model engaged: the story clock advanced by the travel ETA.
    assert _spans_named(otel_capture, SPAN_CLOCK_ADVANCE), (
        "clock.advance span did not fire — the clock model (advance_clock_via_beat "
        "/ a TRAVEL StoryBeat) never engaged; the travel consumed no story-time"
    )
    assert snap.clock_t_hours == pytest.approx(_INITIAL_CLOCK_HOURS + eta_hours), (
        "the story clock did not advance by the computed travel ETA "
        f"(expected {_INITIAL_CLOCK_HOURS + eta_hours}, got {snap.clock_t_hours})"
    )


# ---------------------------------------------------------------------------
# Confidence gate — a below-threshold course intent engages nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_course_degrades_and_does_not_engage(otel_capture) -> None:
    """The bank's per-subsystem confidence gate (default 0.6) protects the
    course/clock engine: an ambiguous turn below threshold degrades to a narrator
    hint and fires NO engine — no course.plot, no clock.advance, clock unmoved
    (ADR-113 confidence gate).
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    snap = _snap()
    package = _package_with(_course_dispatch(confidence=0.2))

    result = await run_dispatch_bank(package, context=_bank_context(snap, content))

    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "a below-threshold course intent must NOT plot a course"
    )
    assert not _spans_named(otel_capture, SPAN_CLOCK_ADVANCE), (
        "a below-threshold course intent must NOT advance the clock"
    )
    assert snap.clock_t_hours == _INITIAL_CLOCK_HOURS, "clock advanced on a gated course"
    assert snap.plotted_course is None and snap.party_body_id == _PARTY_BODY
    # The degrade produced a narrator hint directive (the player's intent still
    # reaches the narrator), and the decision is recorded as degraded_to_hint.
    assert any(d.kind == "must_narrate" for d in result.directives)
    course_decisions = [d for d in result.decisions if d["subsystem"] == "course"]
    assert course_decisions and course_decisions[-1]["decision"] == "degraded_to_hint"


# ---------------------------------------------------------------------------
# No Silent Fallbacks — an unknown destination is rejected LOUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_unknown_destination_rejects_loud(otel_capture) -> None:
    """A course dispatch naming a body NOT in ``orbits.bodies`` is a resolution
    failure — it must fail LOUD (a ``course.plot.rejected`` span), never plot a
    phantom course or silently no-op (No Silent Fallbacks; CLAUDE.md).
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    snap = _snap()
    package = _package_with(
        _course_dispatch(destination="atlantis_station_that_does_not_exist", confidence=0.9)
    )

    await run_dispatch_bank(package, context=_bank_context(snap, content))

    assert _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED), (
        "an unresolvable destination must emit a course.plot.rejected span "
        "(loud failure), not a silent no-op"
    )
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "a rejected course must NOT also fire course.plot"
    )
    assert snap.plotted_course is None, "a rejected destination must not plot a phantom course"
    assert snap.clock_t_hours == _INITIAL_CLOCK_HOURS, (
        "a rejected course must not advance the clock"
    )
    assert snap.party_body_id == _PARTY_BODY, "a rejected course must not move the party"


# ---------------------------------------------------------------------------
# Engagement watcher — course witness (lie-detector)
# ---------------------------------------------------------------------------


def test_course_witness_registered() -> None:
    """The dispatch engagement watcher must have a ``course`` witness in
    ``_WITNESSES`` (and a ``_DISPATCHED_TYPE_KEY['course']`` entry) so the GM
    panel can detect a router-claimed-but-engine-idle burn.
    """
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES

    assert "course" in _WITNESSES, (
        f"course has no engagement witness; _WITNESSES has {sorted(_WITNESSES)}. "
        "Story 153-5 must add it (dispatch_engagement_watcher.py:_WITNESSES "
        "+ _DISPATCHED_TYPE_KEY)."
    )


def test_watcher_flags_course_dispatch_that_did_not_plot() -> None:
    """Router dispatched ``course`` but neither a plotted course nor an arrival
    landed → a real mismatch (``dispatch_engagement.course.mismatch``). Drives the
    pure detection function against a post-turn snapshot with no course engaged.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    # course was dispatched, but the snapshot shows no engagement: still at the
    # origin, no plotted course (the exact router-claimed-but-engine-idle case).
    snap = _snap()
    assert snap.plotted_course is None and snap.party_body_id == _PARTY_BODY
    package = _package_with(_course_dispatch(confidence=0.9))

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert any(m.subsystem == "course" for m in mismatches), (
        "watcher must flag a course dispatch that left the party un-coursed "
        "(router-claimed-but-engine-idle), got: "
        f"{[(m.subsystem, m.evidence) for m in mismatches]}"
    )


def test_watcher_silent_when_course_committed() -> None:
    """The inverse: when the course actually committed (plotted_course set), the
    witness sees it and reports NO mismatch (honest engagement)."""
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )
    from sidequest.orbital.course import CourseSource, PlottedCourse

    snap = _snap()
    snap.plotted_course = PlottedCourse(
        to_body_id=_DEST_BODY,
        label="RED PROSPECT",
        eta_hours=35.0,
        delta_v=0.9,
        plotted_at_t_hours=snap.clock_t_hours,
        source=CourseSource.QUEST_OBJECTIVE,
    )
    package = _package_with(_course_dispatch(confidence=0.9))

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert not any(m.subsystem == "course" for m in mismatches), (
        "watcher false-flagged a course that actually committed"
    )


# ---------------------------------------------------------------------------
# Production wiring — the pre-pass threads orbital_content to the handler
# ---------------------------------------------------------------------------


def test_pre_pass_accepts_orbital_content_param() -> None:
    """The course engine needs the world's ``orbital_content`` (orbits config),
    which lives on the ``Session`` — NOT the ``GameSnapshot``. The production
    pre-pass must accept it so it can thread it into the bank context. This is a
    runtime-signature (reflection) tripwire, not a source grep — the blessed
    pattern for a call-site contract (CLAUDE.md "No Source-Text Wiring Tests").
    """
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    params = inspect.signature(execute_intent_router_pre_narrator_pass).parameters
    assert "orbital_content" in params, (
        "execute_intent_router_pre_narrator_pass must accept an orbital_content "
        "parameter so the course subsystem is reachable in real play; "
        f"signature params: {sorted(params)}"
    )


@pytest.mark.asyncio
async def test_pre_pass_threads_orbital_content_to_course_handler(otel_capture) -> None:
    """Anti-trap wiring proof: drive the REAL pre-narrator pass with a stubbed
    router that dispatches ``course``, and prove the handler ENGAGES — i.e. the
    pass forwarded ``orbital_content`` into the bank context. Driving only a
    hand-built bank context (the tests above) would mask a pre-pass that never
    threads the orbits config (project memory ``project_opposed_check_wiring_trap``).
    """
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    content = _orbital_content()
    snap = _snap()

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()

    router = MagicMock()
    router.decompose = AsyncMock(return_value=_package_with(_course_dispatch(confidence=0.9)))

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="We burn for the Red Prospect.",
        player_name="Rux",
        orbital_content=content,
    )

    # The handler engaged through the REAL pass: the clock advanced + the span
    # fired. (If orbital_content is not threaded, the handler cannot resolve the
    # course and engages nothing.)
    assert _spans_named(otel_capture, SPAN_CLOCK_ADVANCE), (
        "clock.advance did not fire from the real pre-pass — orbital_content was "
        "not threaded into the bank context, so the course handler stayed inert"
    )
    assert _course_committed(snap), (
        "the course did not commit through the real pre-pass "
        f"(plotted_course={snap.plotted_course!r}, party_body_id={snap.party_body_id!r})"
    )
