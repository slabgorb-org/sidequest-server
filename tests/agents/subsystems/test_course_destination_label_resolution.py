"""RED tests — Story 158-27 ``[SWN-ORBITAL-COURSE-INERT]`` — course destination
label resolution.

Playtest (pingpong 2026-06-25, coyote_star SWN): a clear course intent — *"plot a
course to the Broken Drift, lay in the burn"* — left ``plotted_course`` null and
engaged no Hohmann transit, even though Story 153-5 wired the ``course`` subsystem
onto the pre-narrator dispatch bank. The wiring is all present and verified by
``test_course_clock_dispatch_wiring.py``: the handler IS registered, the bank
threads ``orbital_content``, and ``party_body_id`` IS bound on connect from
``cartography.starting_region`` (``far_landing`` is a real body). So the engine is
reachable — yet inert on a real, naturally-phrased burn.

Root cause (TEA, 158-27 RED): ``run_course_dispatch``
(``sidequest/agents/subsystems/course.py``) resolves the dispatched ``destination``
against ``orbits.bodies`` with a bare exact-key lookup — ``bodies.get(destination)``.
But the IntentRouter is an LLM pass, and a player who says "the Broken Drift" yields a
``destination`` carrying the human label / proper-noun phrasing ("Broken Drift" /
"BROKEN DRIFT") — NEVER the internal snake_case body id ``broken_drift``
(``orbits.yaml``: ``broken_drift:`` with ``label: "BROKEN DRIFT"``). The exact lookup
misses, the handler rejects the course as ``unknown_destination``
(``course.plot.rejected``), and ``plotted_course`` stays null. The story clock then
advances only by the generic per-turn beat, never by the travel ETA — exactly the
playtest symptom.

The 153-5 wiring suite hid this because every ``SubsystemDispatch`` it builds names the
canonical id directly (``destination="red_prospect"``). These tests drive the same
``world_minimal`` fixture but pass the destination as a human would name it — the
body's LABEL, with natural casing and a leading article — and assert the burn lands.

Contract under test (TEA-defined for Dev, 158-27 GREEN): ``run_course_dispatch`` must
resolve a destination naming a body's LABEL (case- and leading-article tolerant) to
that body's canonical id before plotting — so the natural-language burn commits a real
``PlottedCourse`` (``to_body_id`` == the *canonical id*, not the echoed label) and a
TRAVEL clock advance. A destination matching NEITHER a body id NOR any label still
fails LOUD (``course.plot.rejected``); resolution becomes tolerant, never a silent
fuzzy guess that always matches something (No Silent Fallbacks, CLAUDE.md).

Drives the REAL ``run_dispatch_bank`` — wiring-honest behavior + OTEL spans, never a
source-text grep. RED until Dev lands label resolution; collection passes today.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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

# world_minimal orbital fixture: coyote (star) → red_prospect (companion, label
# "RED PROSPECT") → turning_hub (habitat moon). Shared with the 153-5 wiring test.
_WORLD_MINIMAL = Path(__file__).resolve().parents[2] / "orbital" / "fixtures" / "world_minimal"

_PARTY_BODY = "turning_hub"
_DEST_BODY = "red_prospect"  # canonical snake_case id
_DEST_LABEL = "RED PROSPECT"  # the human label authored in orbits.yaml
_INITIAL_CLOCK_HOURS = 100.0

SPAN_COURSE_PLOT = "course.plot"
SPAN_COURSE_PLOT_REJECTED = "course.plot.rejected"
SPAN_CLOCK_ADVANCE = "clock.advance"


# ---------------------------------------------------------------------------
# Builders — synthetic only (the STUBBED router output)
# ---------------------------------------------------------------------------


def _course_dispatch(*, destination: str, confidence: float = 0.9) -> SubsystemDispatch:
    """The STUBBED router output: the ``course`` classification the IntentRouter
    would emit. ``destination`` is whatever the LLM extracted from the player's
    natural-language burn — here, the human label / phrasing, not the body id."""
    return SubsystemDispatch(
        subsystem="course",
        params={"destination": destination},
        idempotency_key="k-course-158-27",
        confidence=confidence,
        visibility=VisibilityTag(visible_to="all"),
    )


def _package(dispatch: SubsystemDispatch) -> DispatchPackage:
    return DispatchPackage(
        turn_id="turn-158-27",
        per_player=[
            PlayerDispatch(
                player_id="player:Rux",
                raw_action="We burn for the Red Prospect.",
                dispatch=[dispatch],
            )
        ],
        confidence_global=1.0,
    )


def _orbital_content():
    return load_orbital_content(_WORLD_MINIMAL)


def _snap() -> GameSnapshot:
    """Party anchored at turning_hub with a non-zero story clock — no quest
    anchors, so resolution can only succeed by matching the body itself (id or
    label), never by riding the quest-anchor provenance path."""
    return GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        party_body_id=_PARTY_BODY,
        clock_t_hours=_INITIAL_CLOCK_HOURS,
    )


def _bank_context(snap: GameSnapshot, content) -> dict:
    return {
        "snapshot": snap,
        "pack": MagicMock(),
        "player_name": "Rux",
        "orbital_content": content,
    }


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _expected_eta(content) -> float:
    eta_hours, _dv = compute_eta_and_dv(
        content.orbits.bodies[_PARTY_BODY],
        content.orbits.bodies[_DEST_BODY],
        content.orbits,
    )
    assert eta_hours > 0.0, "fixture sanity: turning_hub→red_prospect must cost > 0h"
    return eta_hours


# ---------------------------------------------------------------------------
# Core reproduction — a label destination resolves and plots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_resolves_label_destination_and_plots(otel_capture) -> None:
    """The playtest reproduction. The router emits the body's LABEL ("RED PROSPECT")
    — what the player said — not the snake_case id. The handler must resolve it to
    ``red_prospect`` and plot a real course: ``course.plot`` fires, NO
    ``course.plot.rejected``, ``plotted_course`` carries the CANONICAL id, and the
    clock advances by the travel ETA.

    Today ``bodies.get("RED PROSPECT")`` is ``None`` → ``unknown_destination`` →
    rejected, ``plotted_course`` stays null. That is the bug.
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    eta_hours = _expected_eta(content)
    snap = _snap()

    await run_dispatch_bank(
        _package(_course_dispatch(destination=_DEST_LABEL)),
        context=_bank_context(snap, content),
    )

    assert _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "course.plot did not fire — the handler failed to resolve the label "
        f"{_DEST_LABEL!r} to a body and never plotted (the inert-burn bug)"
    )
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED), (
        "a resolvable label must NOT be rejected as unknown_destination"
    )
    assert snap.plotted_course is not None, (
        f"no course committed for label {_DEST_LABEL!r}; plotted_course is still None"
    )
    assert snap.plotted_course.to_body_id == _DEST_BODY, (
        "a resolved label must commit the CANONICAL body id, not echo the label "
        f"(expected {_DEST_BODY!r}, got {snap.plotted_course.to_body_id!r}) — the "
        "id is what joins with party_body_id on arrival and feeds the watcher"
    )
    assert snap.clock_t_hours == pytest.approx(_INITIAL_CLOCK_HOURS + eta_hours), (
        "the story clock did not advance by the computed travel ETA after a label "
        f"burn (expected {_INITIAL_CLOCK_HOURS + eta_hours}, got {snap.clock_t_hours})"
    )


# ---------------------------------------------------------------------------
# Case + article tolerance — the LLM does not echo the canonical casing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    [
        "RED PROSPECT",  # label verbatim (copied from a courses block)
        "Red Prospect",  # natural title-case proper noun
        "red prospect",  # lowercased
        "the Red Prospect",  # the literal playtest phrasing: "the Broken Drift"
    ],
)
async def test_course_label_resolution_is_case_and_article_tolerant(
    otel_capture, destination: str
) -> None:
    """An LLM names a destination however the player phrased it. Every natural form
    of the label — verbatim, title-case, lowercase, and with a leading article —
    must resolve to the same canonical body and plot, because the player who says
    "the Broken Drift" must not be silently stranded on casing or an article."""
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    snap = _snap()

    await run_dispatch_bank(
        _package(_course_dispatch(destination=destination)),
        context=_bank_context(snap, content),
    )

    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED), (
        f"destination {destination!r} was rejected — label resolution must tolerate "
        "casing and a leading article"
    )
    assert snap.plotted_course is not None and snap.plotted_course.to_body_id == _DEST_BODY, (
        f"destination {destination!r} did not resolve to {_DEST_BODY!r} "
        f"(plotted_course={snap.plotted_course!r})"
    )


# ---------------------------------------------------------------------------
# Regression — the canonical body id still resolves (don't break 153-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_canonical_body_id_still_resolves(otel_capture) -> None:
    """Tolerant resolution must not regress the exact-id path the 153-5 wiring suite
    proves: a dispatch naming the canonical ``red_prospect`` still plots and advances
    the clock."""
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    eta_hours = _expected_eta(content)
    snap = _snap()

    await run_dispatch_bank(
        _package(_course_dispatch(destination=_DEST_BODY)),
        context=_bank_context(snap, content),
    )

    assert _spans_named(otel_capture, SPAN_COURSE_PLOT), "canonical id stopped plotting"
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED)
    assert snap.plotted_course is not None and snap.plotted_course.to_body_id == _DEST_BODY
    assert snap.clock_t_hours == pytest.approx(_INITIAL_CLOCK_HOURS + eta_hours)


# ---------------------------------------------------------------------------
# No Silent Fallbacks — a truly unknown destination still rejects LOUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_unknown_destination_still_rejects_loud(otel_capture) -> None:
    """Tolerance must not become a fuzzy guess that always matches *something*. A
    destination matching NEITHER any body id NOR any label is a real resolution
    failure: it still emits ``course.plot.rejected`` (loud), plots no phantom course,
    and moves neither the clock nor the party (No Silent Fallbacks, CLAUDE.md)."""
    from sidequest.agents.subsystems import run_dispatch_bank

    content = _orbital_content()
    snap = _snap()

    await run_dispatch_bank(
        _package(_course_dispatch(destination="atlantis_station_that_does_not_exist")),
        context=_bank_context(snap, content),
    )

    assert _spans_named(otel_capture, SPAN_COURSE_PLOT_REJECTED), (
        "an unresolvable destination must still fail loud (course.plot.rejected)"
    )
    assert not _spans_named(otel_capture, SPAN_COURSE_PLOT), (
        "a rejected course must not also fire course.plot"
    )
    assert snap.plotted_course is None, "a rejected destination must not plot a phantom course"
    assert snap.clock_t_hours == _INITIAL_CLOCK_HOURS, "a rejected course must not advance the clock"
    assert snap.party_body_id == _PARTY_BODY, "a rejected course must not move the party"
