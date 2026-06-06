"""Story 77-7 — lull-escalation turn-pipeline wiring (ADR-024/025/128).

RED premise (Keith playtest, wry_whimsy/gulliver): the engine never PUSHES on a
lull. 81-2 put a live ``TensionTracker`` on ``_SessionData`` and 81-3 bridged its
``pacing_hint`` into ``TurnContext``; 22-x built the seed deck. This story wires
the missing SELECTOR into the turn so a lull fires a seed as a concrete
escalation directive — and proves it with OTEL + a behavioral bridge test.

Two wiring seams, both proved behaviorally (per CLAUDE.md "No Source-Text Wiring
Tests" — drive real code, observe spans / TurnContext, never grep source):

  PRODUCER — the step runs inside ``_execute_narration_turn`` (post
  ``record_interaction``, peer to ``tick_seeds``). Proof: a real turn during a
  lull emits ``SPAN_LULL_ESCALATION``; a non-lull turn does not.

  CONSUMER — the fired seed's ``narrative_hint`` reaches the NEXT turn's narrator
  context, REPLACING the generic "environment shifts" ``escalation_beat``. Proof:
  the real selector stores a directive, then the real ``_build_turn_context``
  surfaces it on ``TurnContext.pacing_hint.escalation_beat``.

Both fail on current ``develop`` (no selector, no override) and pass after the
fix.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot, SeedState
from sidequest.game.tension_tracker import RoundResult, TensionTracker
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry import init_tracer
from tests.server.conftest import _make_minimal_narration_turn_result

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def lull_otel_capture() -> Iterator[InMemorySpanExporter]:
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


@pytest.fixture(scope="module")
def _loader() -> GenreLoader:
    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS)


def _seedtrope(seed_id: str, *, narrative_hint: str) -> SeedTrope:
    return SeedTrope(
        id=seed_id,
        name=f"Seed {seed_id}",
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
        narrative_hint=narrative_hint,
    )


def _active(seed_id: str) -> SeedState:
    return SeedState(
        id=seed_id,
        name=f"Seed {seed_id}",
        activated_at_turn=1,
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
    )


class _Pack:
    def __init__(self, seed_tropes: list[SeedTrope]) -> None:
        self.seed_tropes = seed_tropes
        self.tropes: list = []


def _observe_boring(tracker: TensionTracker, n: int) -> None:
    boring = RoundResult(round=1, damage_events=[], effects_applied=[], effects_expired=[])
    for _ in range(n):
        tracker.observe(boring, killed=None, lowest_hp_ratio=None)


def _lull_spans(exporter: InMemorySpanExporter) -> list:
    from sidequest.telemetry.spans import SPAN_LULL_ESCALATION

    return [s for s in exporter.get_finished_spans() if s.name == SPAN_LULL_ESCALATION]


# ---------------------------------------------------------------------------
# PRODUCER — the selector runs inside the real turn pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_turn_during_lull_emits_lull_escalation_span(
    session_fixture, lull_otel_capture
) -> None:
    """Driving one real ``_execute_narration_turn`` while the session is in a
    lull must emit ``SPAN_LULL_ESCALATION`` from the production path — the
    behavioral proof that the selector is wired into the turn (not just unit-
    tested in isolation). Fails on develop: no such step exists, no span fires.

    The lull seam runs *before* the per-turn tension ``observe()`` (handler line
    order), so we pre-seat the tracker to the escalation threshold; this turn's
    own observation is not needed to cross it.
    """
    sd, handler = session_fixture
    handler._validator = None
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_make_minimal_narration_turn_result("The corridor stays still.")
    )
    # The selector reads genre thresholds + the seed deck off the pack (same
    # source as the 81-3 pacing bridge: ``pack.drama_thresholds or defaults``).
    sd.genre_pack.drama_thresholds = DramaThresholds(escalation_streak=3)
    sd.genre_pack.seed_tropes = [_seedtrope("alpha", narrative_hint="A spy tails the party.")]
    sd.genre_pack.tropes = []
    # Pre-seat the lull (3 prior boring turns) and seat an active seed to fire.
    _observe_boring(sd.tension_tracker, 3)
    sd.snapshot.active_seeds = [_active("alpha")]

    from tests.server.conftest import _build_turn_context_for_test

    await handler._execute_narration_turn(
        sd, "I wait and listen.", _build_turn_context_for_test(sd)
    )

    spans = _lull_spans(lull_otel_capture)
    assert len(spans) >= 1, (
        "a real lull turn must emit SPAN_LULL_ESCALATION from the production turn "
        "path (selector wired peer to tick_seeds); none fired — the step is not "
        f"wired into _execute_narration_turn. Spans seen: "
        f"{sorted({s.name for s in lull_otel_capture.get_finished_spans()})}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("fired") is True, f"the lull should have fired a seat seed; got {attrs}"
    assert attrs.get("selected_seed_id") == "alpha", (
        f"the fired span must carry the selected seed id; got {attrs}"
    )


@pytest.mark.asyncio
async def test_non_lull_turn_does_not_emit_lull_escalation_span(
    session_fixture, lull_otel_capture
) -> None:
    """A normal (non-lull) turn must NOT emit ``SPAN_LULL_ESCALATION`` — the
    selector is gated on the live lull signal, not fired every turn. Guards
    against an always-on step that would spam the panel and pile up Bangs in
    violation of the ADR-128 governor.
    """
    sd, handler = session_fixture
    handler._validator = None
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_make_minimal_narration_turn_result("You step into the hall.")
    )
    sd.genre_pack.drama_thresholds = DramaThresholds(escalation_streak=3)
    sd.genre_pack.seed_tropes = [_seedtrope("alpha", narrative_hint="A spy tails the party.")]
    sd.genre_pack.tropes = []
    # Fresh tracker (boring_streak 0) → no lull this turn.
    sd.snapshot.active_seeds = [_active("alpha")]

    from tests.server.conftest import _build_turn_context_for_test

    await handler._execute_narration_turn(
        sd, "I march forward boldly.", _build_turn_context_for_test(sd)
    )

    assert _lull_spans(lull_otel_capture) == [], (
        "a non-lull turn must not emit SPAN_LULL_ESCALATION — the step is gated "
        "on boring_streak >= escalation_streak, not fired unconditionally"
    )


# ---------------------------------------------------------------------------
# CONSUMER — the fired directive reaches the next turn's narrator context
# ---------------------------------------------------------------------------


def _make_real_sd(loader: GenreLoader, genre: str, world: str):
    """A real ``_SessionData`` with a loaded pack — mirrors the 81-3 pacing
    bridge fixture so the real ``_build_turn_context`` runs cleanly."""
    from sidequest.server.session_handler import _SessionData

    pack = loader.load(genre)
    snap = GameSnapshot(
        genre_slug=genre,
        world_slug=world,
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Rux"] = "Main Hall"
    snap.player_seats["player:Rux"] = "Rux"
    repo = MagicMock()
    repo.recent_narrative.return_value = []
    sd = _SessionData(
        genre_slug=genre,
        world_slug=world,
        player_name="Rux",
        player_id="player:Rux",
        snapshot=snap,
        repository=repo,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.game_slug = f"2026-06-05-{genre}_{world}-1"
    return sd


def test_fired_directive_replaces_generic_escalation_beat_next_turn(_loader) -> None:
    """End-to-end bridge (AC5 consumer half): after the real selector fires a
    seed (storing its ``narrative_hint`` as the pending escalation directive),
    the real ``_build_turn_context`` must surface THAT directive on
    ``TurnContext.pacing_hint.escalation_beat`` — REPLACING the generic
    "environment shifts" text the tension track would otherwise produce.

    The tracker is primed above the escalation threshold so the *generic* beat
    would fire absent the override; asserting the seed directive (not the
    generic string) proves the replacement. Storage-agnostic: it drives the real
    producer then the real consumer, never naming the snapshot field.

    Fails on develop two ways: ``apply_lull_escalation`` does not exist, and
    ``_build_turn_context`` has no directive override.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_real_sd(_loader, "caverns_and_claudes", "sunken_keep")
    # caverns ships no pacing.yaml → DramaThresholds() defaults (escalation_streak 5).
    assert sd.genre_pack.drama_thresholds is None, "fixture sanity: no authored pacing.yaml"
    _observe_boring(sd.tension_tracker, 5)  # generic escalation_beat would now fire

    directive = "A Thern in a red sash slips a forged map into your pack."
    sd.snapshot.active_seeds = [_active("alpha")]
    selector_pack = _Pack([_seedtrope("alpha", narrative_hint=directive)])

    # PRODUCER (real): fire the seed; stores the directive for the next turn.
    result = apply_lull_escalation(
        sd.snapshot,
        selector_pack,
        tracker=sd.tension_tracker,
        thresholds=DramaThresholds(),
        session_id=sd.game_slug,
        now_turn=sd.snapshot.turn_manager.interaction,
    )
    assert result.fired is True and result.directive == directive, (
        f"selector precondition: must fire and carry the directive; got {result}"
    )

    # CONSUMER (real): the next turn's context carries the seed directive.
    ctx = _build_turn_context(sd)
    assert ctx.pacing_hint is not None, "the pacing bridge must populate the hint (81-3)"
    assert ctx.pacing_hint.escalation_beat == directive, (
        "the fired seed's narrative_hint must REPLACE the generic escalation_beat "
        "in the next turn's narrator context; got "
        f"{ctx.pacing_hint.escalation_beat!r} (expected the seed directive, not the "
        "generic 'environment shifts' text)"
    )
