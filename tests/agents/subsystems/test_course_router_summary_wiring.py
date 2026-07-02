"""RED wiring tests — Story 158-50 — the router-summary ``<courses>`` block.

Playtest finding ``[SWN-ORBITAL-COURSE-INERT]`` re-confirmed (2026-06-27 GM
``/sq-playtest`` space-scale run, solo ``space_opera/coyote_star``): plotting AND
executing a transit in natural language writes NOTHING mechanical —
``plotted_course=None``, ``clock_t_hours`` frozen at 0.0, no course/clock/time_skip
OTEL span fires. The narrator improvises the burn; the orrery is a read-only
decoration whose STARDATE never ticks.

ROOT CAUSE (the CLAUDE.md half-wired-trigger anti-pattern). Story 153-5 wired the
``course`` subsystem end-to-end BELOW the router: ``run_course_dispatch`` is
registered, the pre-pass threads ``orbital_content`` into the bank context, the
engagement watcher has a ``course`` witness. Those are GREEN
(``test_course_clock_dispatch_wiring.py``) — but every one of those tests **stubs
the router**, injecting a ``course`` ``SubsystemDispatch`` directly, because "the
router's classification *is* the input to this layer". 158-50 lives in exactly the
seam 153-5 stubbed past: the REAL router never emits a ``course`` dispatch, because
its own state summary never contains a ``<courses>`` block.

Concretely: the IntentRouter prompt gates course emission —
``intent_router.py:292`` says *"Emit course ONLY when the world has an orbital tier
(a ``<courses>`` block is present in game_state)"*. But ``compute_courses`` /
``format_courses_block`` (``sidequest/orbital/course.py``) are called ONLY when
assembling the NARRATOR prompt (``orchestrator.py`` / ``narration_apply.py``),
NEVER in ``intent_router_pass.py::_build_state_summary`` (which builds the router's
summary). So the router follows its own "ONLY when present" rule, sees no block,
and never classifies travel → ``run_course_dispatch`` is never invoked.

THE FIX (Dev's call, 158-50 GREEN): assemble a ``<courses>`` block into the
router's state summary when the world has an orbital tier
(``session.orbital_content`` present), reusing ``compute_courses`` +
``format_courses_block`` (Don't Reinvent). Then the real router can classify
travel, emit ``course``, and the already-green engine plots the course + ticks the
clock.

These tests drive the PRODUCTION pre-pass
(``execute_intent_router_pre_narrator_pass``) — not a hand-built bank context —
so they exercise the exact seam the fix touches (the summary the real router would
see), and they are refactor-stable to how Dev threads ``orbital_content`` into the
summary builder. Wiring is proven by behavior + OTEL spans, never a source-text
grep (CLAUDE.md "No Source-Text Wiring Tests").

**On the router doubles.** A live intent-router LLM pass is flaky and
content-coupled (project lore ``feedback_no_content_coupled_tests``), so — as in
153-5 — no LLM runs. Two doubles stand in:

* ``_recording_router`` captures the ``state_summary`` the pass hands the router
  and engages nothing. Test 1 asserts on that captured summary directly.
* ``_courses_gated_router`` is a *contract* double, not a content stub: it encodes
  the EXACT rule ``intent_router.py:292`` encodes — emit ``course`` iff a
  ``<courses>`` block is present in the summary it is handed. Its emission
  therefore tracks the one thing 158-50 changes: whether the summary carries the
  block. Today it emits nothing (no block) → the spans never fire → RED. After the
  fix (block present) → it emits ``course`` → the green engine fires the spans →
  GREEN.

Collection passes (every imported name already exists); the assertions fail until
Dev lands the summary block. That is the intended RED.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import RulesConfig
from sidequest.orbital.course import compute_eta_and_dv
from sidequest.orbital.loader import load_orbital_content
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

# world_minimal orbital fixture: coyote (star) → red_prospect (companion,
# semi_major_au 1.2) → turning_hub (habitat moon of red_prospect, 0.04).
# Shared with tests/agents/subsystems/test_course_clock_dispatch_wiring.py.
_WORLD_MINIMAL = Path(__file__).resolve().parents[2] / "orbital" / "fixtures" / "world_minimal"

_PARTY_BODY = "turning_hub"
_DEST_BODY = "red_prospect"
_INITIAL_CLOCK_HOURS = 100.0

SPAN_COURSE_PLOT = "course.plot"
SPAN_COURSE_PLOT_REJECTED = "course.plot.rejected"
SPAN_CLOCK_ADVANCE = "clock.advance"

# The literal marker the IntentRouter prompt gate keys on (intent_router.py:292)
# and that format_courses_block emits (orbital/course.py:204). Its presence in the
# serialized router summary is the leak-proof RED discriminator: a quest_anchor
# serializes as a plain ``["red_prospect"]`` list — it never mints this tag.
COURSES_MARKER = "<courses>"


# ---------------------------------------------------------------------------
# Builders — the stubbed router output + orbital fixtures
# ---------------------------------------------------------------------------


def _course_dispatch(
    *, destination: str = _DEST_BODY, confidence: float = 0.9
) -> SubsystemDispatch:
    """A deterministic ``course`` classification (stands in for the Haiku pass)."""
    return SubsystemDispatch(
        subsystem="course",
        params={"destination": destination},
        idempotency_key="k-course-158-50",
        confidence=confidence,
        visibility=VisibilityTag(visible_to="all"),
    )


def _package_with(*dispatches: SubsystemDispatch) -> DispatchPackage:
    return DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id="player:Rux",
                raw_action="We burn for the Red Prospect.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _empty_package() -> DispatchPackage:
    """The router classified no mechanical dispatch (e.g. it saw no <courses>
    block, so — per its own gate — it did not classify the turn as travel)."""
    return _package_with()


def _orbital_content():
    return load_orbital_content(_WORLD_MINIMAL)


def _snap() -> GameSnapshot:
    """A snapshot anchored at ``turning_hub`` with a non-zero story clock.

    ``red_prospect`` is a quest anchor so ``compute_courses`` surfaces it in the
    block regardless of scope (``compute_courses`` includes any quest-anchored
    body), letting the router name it as a travel destination.
    """
    return GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        party_body_id=_PARTY_BODY,
        clock_t_hours=_INITIAL_CLOCK_HOURS,
        quest_anchors=[_DEST_BODY],
    )


def _pack() -> GenrePack:
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()
    return pack


def _recording_router(sink: dict):
    """A router double that records the ``state_summary`` it is handed and engages
    nothing — for asserting on the summary the real router would see."""
    router = MagicMock()

    async def _decompose(*, action: str, state_summary: dict):
        sink["state_summary"] = state_summary
        return _empty_package()

    router.decompose = _decompose
    return router


def _courses_gated_router(sink: dict | None = None):
    """A *contract* double faithful to ``intent_router.py:292``'s own prompt gate:
    emit a ``course`` dispatch ONLY when a ``<courses>`` block is present in the
    state summary it is handed. Not a content-coupled LLM stub — it encodes the
    exact rule the production Haiku prompt encodes, so the emission tracks the one
    thing 158-50 changes (whether the summary carries the block)."""
    router = MagicMock()

    async def _decompose(*, action: str, state_summary: dict):
        if sink is not None:
            sink["state_summary"] = state_summary
        serialized = json.dumps(state_summary).lower()
        if COURSES_MARKER in serialized:
            return _package_with(_course_dispatch(confidence=0.9))
        return _empty_package()

    router.decompose = _decompose
    return router


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# Test 1 (RED, the seam) — the router summary carries a <courses> block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_state_summary_carries_courses_block_for_orbital_world() -> None:
    """The state summary the pre-pass hands the IntentRouter must contain a
    ``<courses>`` block in an orbital world — otherwise the router's own gate
    (``intent_router.py:292``) can never classify travel and the course subsystem
    is unreachable. This is the 158-50 root cause, asserted on the REAL production
    summary the router consumes (not a source grep).
    """
    sink: dict = {}
    router = _recording_router(sink)

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snap(),
        pack=_pack(),
        action="Kestrel, lay in a burn for the Red Prospect.",
        player_name="Rux",
        orbital_content=_orbital_content(),
    )

    assert "state_summary" in sink, "the pre-pass never called router.decompose"
    serialized = json.dumps(sink["state_summary"]).lower()

    assert COURSES_MARKER in serialized, (
        "the IntentRouter's state summary carries NO <courses> block in an orbital "
        "world. compute_courses/format_courses_block are assembled only for the "
        "NARRATOR prompt (orchestrator.py), never in intent_router_pass.py, so the "
        "router obeys its own gate (intent_router.py:292 'emit course ONLY when a "
        "<courses> block is present') and never classifies travel. 158-50 must "
        "build the block into the router summary when session.orbital_content is "
        "present (reuse compute_courses + format_courses_block)."
    )
    assert f"{_DEST_BODY} (eta" in serialized, (
        "the reachable destination body is not listed in the <courses> block; "
        "compute_courses must surface the quest-anchored body so the router can "
        "name it as a travel destination"
    )


# ---------------------------------------------------------------------------
# Test 2 (RED, AC-1 span spine) — travel action → course.plot + clock tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orbital_travel_action_dispatches_course_and_ticks_clock(otel_capture) -> None:
    """End-to-end through the REAL pre-pass with a router double faithful to the
    production prompt gate: a natural-language travel-to-named-body action in an
    orbital world must fire ``course.plot`` (where today none does), advance the
    story clock, and commit a ``PlottedCourse``.

    RED today: the summary carries no ``<courses>`` block, so the gated router — per
    ``intent_router.py:292`` — emits no ``course`` dispatch, ``run_course_dispatch``
    never runs, and no span fires (the inert-in-play symptom). GREEN once 158-50
    builds the block.
    """
    content = _orbital_content()
    eta_hours, _dv = compute_eta_and_dv(
        content.orbits.bodies[_PARTY_BODY],
        content.orbits.bodies[_DEST_BODY],
        content.orbits,
    )
    assert eta_hours > 0.0, "fixture sanity: turning_hub→red_prospect must cost > 0h"

    snap = _snap()
    router = _courses_gated_router()

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_pack(),
        action="We burn for the Red Prospect.",
        player_name="Rux",
        orbital_content=content,
    )

    # AC-1: the OTEL span fires where today none does (span assertion, not grep).
    assert _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "course.plot span did NOT fire for a natural-language travel action in an "
        "orbital world — the router saw no <courses> block, emitted no course "
        "dispatch, and run_course_dispatch never ran (SWN-ORBITAL-COURSE-INERT)"
    )
    assert _spans_named(otel_capture, SPAN_CLOCK_ADVANCE), (
        "clock.advance span did NOT fire — the story clock never ticked; the orrery "
        "STARDATE stays frozen while the narrator improvises the transit"
    )

    # AC-3: plotted_course is non-None (a PlottedCourse to the resolved body) and
    # the story clock advanced by the computed transit time (verified in the
    # snapshot, not the narration).
    assert snap.plotted_course is not None and snap.plotted_course.to_body_id == _DEST_BODY, (
        f"plotted_course must commit to {_DEST_BODY!r}; got {snap.plotted_course!r}"
    )
    assert snap.clock_t_hours == pytest.approx(_INITIAL_CLOCK_HOURS + eta_hours), (
        "clock_t_hours must advance by the computed travel ETA "
        f"(expected {_INITIAL_CLOCK_HOURS + eta_hours}, got {snap.clock_t_hours})"
    )


# ---------------------------------------------------------------------------
# Test 3 (regression guard, AC-6) — non-orbital worlds are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_orbital_world_gets_no_courses_block_and_no_dispatch(otel_capture) -> None:
    """The ``<courses>`` block is gated on the world having an orbital tier. With no
    ``orbital_content`` threaded (a non-orbital world), the router summary must carry
    NO ``<courses>`` block and no course may plot — else a naive unconditional fix
    would hallucinate travel and spuriously tick the clock in a world with no bodies.

    Passes today (nothing builds the block yet) AND must keep passing after the fix
    (the block stays gated on ``session.orbital_content``).
    """
    sink: dict = {}
    router = _courses_gated_router(sink)
    snap = _snap()

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_pack(),
        action="We burn for the Red Prospect.",
        player_name="Rux",
        orbital_content=None,
    )

    serialized = json.dumps(sink["state_summary"]).lower()
    assert COURSES_MARKER not in serialized, (
        "a non-orbital world (orbital_content=None) must NOT carry a <courses> block "
        "in the router summary; the block must be gated on session.orbital_content"
    )
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "no course may plot in a world with no orbital tier"
    )
    assert not _spans_named(otel_capture, SPAN_CLOCK_ADVANCE), (
        "the clock must not advance in a world with no orbital tier"
    )
    assert snap.plotted_course is None, "no phantom course in a non-orbital world"
    assert snap.clock_t_hours == _INITIAL_CLOCK_HOURS, "the clock must stay put"


# ---------------------------------------------------------------------------
# AC-4 (No Silent Fallbacks) — an unresolvable course fails LOUD, never a
# phantom course + frozen clock. These characterize the run_course_dispatch
# rejection contract (built in 153-5) that 158-50 must not regress; the two
# reasons AC-4 names — no_orbital_tier / no_party_anchor — have no coverage
# elsewhere in the suite. Green today; guards against a silent-fallback
# regression as the fix newly makes the course subsystem reachable in play.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_rejects_loud_when_world_has_no_orbital_tier(otel_capture) -> None:
    """A course dispatch that reaches the handler with no orbital tier
    (``orbital_content=None``) must fail LOUD via ``course.plot.rejected``
    (reason ``no_orbital_tier``) — never a phantom ``PlottedCourse`` or a
    frozen-clock no-op (CLAUDE.md "No Silent Fallbacks")."""
    from sidequest.agents.subsystems.course import run_course_dispatch

    snap = _snap()
    out = await run_course_dispatch(_course_dispatch(), snapshot=snap, orbital_content=None)

    assert _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED), (
        "no orbital tier must emit course.plot.rejected, not a silent no-op"
    )
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT), "no phantom course may plot"
    assert out.data.get("error") == "no_orbital_tier", (
        f"rejection must be reason-coded no_orbital_tier; got {out.data!r}"
    )
    assert snap.plotted_course is None, "a rejected course must not plot a phantom course"
    assert snap.clock_t_hours == _INITIAL_CLOCK_HOURS, "a rejected course must not tick the clock"


@pytest.mark.asyncio
async def test_course_rejects_loud_when_party_has_no_anchor(otel_capture) -> None:
    """A course dispatch whose party is anchored at a body absent from the world's
    ``orbits.bodies`` has nowhere to plot from — it must fail LOUD via
    ``course.plot.rejected`` (reason ``no_party_anchor``), never a phantom course."""
    from sidequest.agents.subsystems.course import run_course_dispatch

    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        party_body_id="a_body_not_in_this_world",
        clock_t_hours=_INITIAL_CLOCK_HOURS,
        quest_anchors=[_DEST_BODY],
    )
    out = await run_course_dispatch(
        _course_dispatch(), snapshot=snap, orbital_content=_orbital_content()
    )

    assert _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED), (
        "no party anchor must emit course.plot.rejected, not a silent no-op"
    )
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT), "no phantom course may plot"
    assert out.data.get("error") == "no_party_anchor", (
        f"rejection must be reason-coded no_party_anchor; got {out.data!r}"
    )
    assert snap.plotted_course is None, "a rejected course must not plot a phantom course"
    assert snap.clock_t_hours == _INITIAL_CLOCK_HOURS, "a rejected course must not tick the clock"
