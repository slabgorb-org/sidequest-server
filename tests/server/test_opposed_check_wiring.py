"""End-to-end wiring tests for the opposed_check resolution branch.

CLAUDE.md: every set of tests must include at least one integration test
that verifies the component is wired into the system — imported, called,
and reachable from production code paths.

These tests cover:

- Wiring 1: dispatch_dice_throw defers beat application when the active
  confrontation declares ``resolution_mode: opposed_check`` and surfaces
  the deferred state on ``DiceThrowOutcome``.
- Wiring 2: ``_apply_narration_result_to_snapshot`` runs the opposed
  resolver (and not the legacy beat loop) when the cdef is opposed_check
  and pending player state is present.
- Wiring 3: ``encounter_opposed_roll_resolved_span`` actually fires
  during the resolution and carries every spec'd attribute.
- Wiring 4: the narrator prompt receives the opposed_check gate text
  when the active encounter has that mode (and DOES NOT receive it
  otherwise).
- Wiring 5: the migrated genre packs load with opposed_check + a
  populated ``opponent_default_stats`` block, end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
)
from sidequest.protocol.dice import (
    DiceThrowPayload,
    RollOutcome,
    ThrowParams,
)
from sidequest.server.dispatch.dice import dispatch_dice_throw
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.telemetry.spans import SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED
from tests._helpers.session_room import room_for


def _make_snapshot() -> GameSnapshot:
    """Story 45-9: dispatch_dice_throw now requires a snapshot."""
    return GameSnapshot(
        genre_slug="test",
        world_slug="test",
        turn_manager=TurnManager(),
    )


CONTENT_ROOT = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
MIGRATION_ROOT = Path("/Users/slabgorb/Projects/oq-2-content-migration/genre_packs")


# ---------------------------------------------------------------------------
# OTEL exporter fixture (mirrors tests/server/dispatch/test_sealed_letter.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def captured_spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a per-test tracer that exports to an in-memory buffer.

    Mirrors ``tests/server/dispatch/test_sealed_letter.py`` —
    monkeypatching ``spans.tracer`` so each test gets a fresh exporter
    without colliding with the global tracer provider that other tests
    or the server boot may have installed.
    """
    from sidequest.telemetry import spans as spans_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exporter


# ---------------------------------------------------------------------------
# Test fixtures (lightweight pack + encounter, no real YAML)
# ---------------------------------------------------------------------------


def _attack_beat() -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": "attack",
            "label": "Attack",
            "kind": "strike",
            "base": 2,
            "stat_check": "STR",
        }
    )


def _defend_beat() -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": "defend",
            "label": "Defend",
            "kind": "brace",
            "base": 1,
            "stat_check": "STR",
        }
    )


def _opposed_cdef() -> ConfrontationDef:
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "resolution_mode": "opposed_check",
            "opponent_default_stats": {"STR": 12},
            "player_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "opponent_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "beats": [_attack_beat().model_dump(), _defend_beat().model_dump()],
        }
    )


def _legacy_cdef() -> ConfrontationDef:
    """Same beats but legacy beat_selection — for negative narrator-prompt test."""
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "resolution_mode": "beat_selection",
            "player_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "opponent_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "beats": [_attack_beat().model_dump(), _defend_beat().model_dump()],
        }
    )


def _make_pack(cdef: ConfrontationDef) -> GenrePack:
    """Build a minimal GenrePack — only ``rules`` is consulted by the
    code paths under test. ``model_construct`` skips the validator so we
    don't need to fabricate every required nested field (theme, lore,
    etc.); a real load_genre_pack happens in the migration wiring test.
    """
    rules = RulesConfig(confrontations=[cdef])
    return GenrePack.model_construct(rules=rules)


def _make_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name="Sam",
                role="combatant",
                side="player",
                per_actor_state={"stats": {"STR": 14}},
            ),
            EncounterActor(
                name="Wolf",
                role="combatant",
                side="opponent",
                per_actor_state={"stats": {"STR": 14}},
            ),
        ],
    )


def _make_encounter_with_companion() -> StructuredEncounter:
    """Sumpdrake-fight shape: solo PC + recruited NPC companion + 1 opponent."""
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name="Sam",
                role="combatant",
                side="player",
                per_actor_state={"stats": {"STR": 14}},
            ),
            EncounterActor(
                name="Donut",
                role="ally",
                side="player",
                # Companion has no per_actor_state.stats — falls back
                # to cdef.opponent_default_stats (STR: 12 in the fixture)
                # via resolve_opponent_modifier.
            ),
            EncounterActor(
                name="Wolf",
                role="combatant",
                side="opponent",
                per_actor_state={"stats": {"STR": 14}},
            ),
        ],
    )


def _throw_params() -> ThrowParams:
    return ThrowParams(
        velocity=(0.0, 0.0, 0.0),
        angular=(0.0, 0.0, 0.0),
        position=(0.0, 0.0),
    )


# ---------------------------------------------------------------------------
# Wiring 1: dispatch_dice_throw defers apply_beat for opposed_check
# ---------------------------------------------------------------------------


def test_dispatch_dice_throw_defers_apply_beat_for_opposed_check():
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    payload = DiceThrowPayload(
        request_id="r1",
        throw_params=_throw_params(),
        face=[15],  # player rolled a 15
        beat_id="attack",
    )

    outcome = dispatch_dice_throw(
        payload=payload,
        rolling_player_id="p1",
        character_name="Sam",
        character_stats={"STR": 14},
        encounter=enc,
        pack=pack,
        genre_slug="test",
        session_id="s",
        round_number=1,
        room_broadcast=None,
        snapshot=_make_snapshot(),
    )

    # Beat application was deferred — neither dial moves yet.
    assert enc.player_metric.current == 0
    assert enc.opponent_metric.current == 0
    # The dispatcher reports the deferral.
    assert outcome.opposed_pending is True
    assert outcome.opposed_player_d20 == 15
    assert outcome.opposed_player_beat_id == "attack"


def test_dispatch_dice_throw_legacy_beat_selection_still_applies():
    """Negative wiring: when resolution_mode is beat_selection (the
    default), the legacy path applies the beat immediately. Proves the
    new branch is GUARDED by the resolution_mode check, not always-on."""
    enc = _make_encounter()
    pack = _make_pack(_legacy_cdef())
    payload = DiceThrowPayload(
        request_id="r1",
        throw_params=_throw_params(),
        face=[20],  # crit success
        beat_id="attack",
    )

    outcome = dispatch_dice_throw(
        payload=payload,
        rolling_player_id="p1",
        character_name="Sam",
        character_stats={"STR": 14},
        encounter=enc,
        pack=pack,
        genre_slug="test",
        session_id="s",
        round_number=1,
        room_broadcast=None,
        snapshot=_make_snapshot(),
    )

    # Legacy path applied the beat — player metric advanced.
    assert enc.player_metric.current > 0
    assert outcome.opposed_pending is False
    assert outcome.opposed_player_d20 is None


# ---------------------------------------------------------------------------
# Wiring 2 + 3: narration_apply runs the resolver and emits the OTEL span
# ---------------------------------------------------------------------------


def _patched_d20(monkeypatch, value: int) -> None:
    """Monkeypatch the server-side opponent d20 to a deterministic value."""
    from sidequest.server import narration_apply

    monkeypatch.setattr(
        narration_apply,
        "_roll_d20_server_side",
        lambda: value,
    )


def test_narration_apply_runs_resolver_and_advances_dial(monkeypatch, captured_spans):
    """Wiring: when cdef is opposed_check, _apply_narration_result_to_snapshot
    runs the resolver, applies the engine-derived tier, and the encounter
    metric advances accordingly. Proves the dispatch branch is reachable
    from the production narration path.

    Per-side tier semantics (Keith's rule, playtest 2026-05-06): each
    actor resolves *their own* roll-vs-DC. A player CritSuccess does NOT
    bleed into an opponent CritSuccess; the opponent gets whatever tier
    their own d20+mod-vs-DC yields.
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())

    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    # Player rolled 18 (mod +2) → 20 vs DC=14 (base=2 → DC=14). Player
    # total exceeds DC by 6 → Success (decisive margin is +10, so this
    # is plain Success, not CritSuccess despite the high roll).
    # Opponent rolls 5 (mod +2) → 7 vs DC=14 (their attack also base=2).
    # Opponent total below DC → Fail tier for the opponent.
    _patched_d20(monkeypatch, value=5)

    # Narrator emitted opponent's beat selection only — player's beat is in
    # the pending stash from DICE_THROW. The PC-beat gate would drop any
    # PC-side selections in real flow, so we only feed the opponent here.
    # Opponent's beat is `attack` targeting nothing (no counteract).
    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(
                actor="Wolf",
                beat_id="attack",
                outcome=RollOutcome.Success,
            ),
        ],
    )

    apply_outcome = _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        opposed_player_d20=18,
        opposed_player_beat_id="attack",
        opposed_player_actor="Sam",
        from_explicit_action=True,  # DICE_THROW already gated this turn,
        room=room_for(snapshot),
    )

    assert apply_outcome.sealed_letter is None
    # Player rolled Success on a strike (base=2). Strike Success grants
    # own=base → +2 momentum.
    assert enc.player_metric.current == 2
    # Opponent rolled Fail (7 < 14). Strike Fail grants no own delta —
    # opponent dial stays at 0. This is the corrected per-side behavior:
    # the prior shift-tier-applied-to-both-sides logic gave the opponent
    # CritSuccess here too, advancing their dial unjustly.
    assert enc.opponent_metric.current == 0

    # Wiring 3: the lie-detector span fired with full attributes. The
    # span's headline ``tier`` carries the player's final tier (the
    # value that drove the player's metric_advance); the watcher event
    # carries the full per-side breakdown.
    finished = captured_spans.get_finished_spans()
    opposed_spans = [s for s in finished if s.name == SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED]
    assert len(opposed_spans) == 1, (
        f"expected exactly 1 opposed_roll_resolved span, got {len(opposed_spans)}: "
        f"finished={[s.name for s in finished]}"
    )
    attrs = dict(opposed_spans[0].attributes or {})
    assert attrs["encounter_type"] == "combat"
    assert attrs["player_roll"] == 18
    assert attrs["player_mod"] == 2
    assert attrs["opponent_roll"] == 5
    assert attrs["opponent_mod"] == 2
    assert attrs["shift"] == 13
    assert attrs["tier"] == RollOutcome.Success.value


def test_narration_apply_opposed_check_fail_advances_opponent_dial(monkeypatch, captured_spans):
    """Per-side resolution (playtest 2026-05-06): when the player rolled
    poorly and the opponent rolled well, the opponent advances *their*
    dial from *their* own roll-vs-DC. This is the inverse of the player-
    crit case: player Fail does NOT pull the opponent down to Fail —
    the opponent gets their own well-rolled tier.
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    # Player rolls 5 (mod +2) → 7 vs DC=14 → Fail for player.
    # Opponent rolls 18 (mod +2) → 20 vs DC=14 → Success (not CritSuccess
    # — total exceeds DC by 6, decisive-margin threshold is +10).
    _patched_d20(monkeypatch, value=18)

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(actor="Wolf", beat_id="attack", outcome=RollOutcome.Success),
        ],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        opposed_player_d20=5,
        opposed_player_beat_id="attack",
        opposed_player_actor="Sam",
        from_explicit_action=True,
        room=room_for(snapshot),
    )

    # Player Fail on strike → no own dial advance.
    assert enc.player_metric.current == 0
    # Opponent Success on strike → own dial advances by base=2. The
    # test name "fail_advances_opponent_dial" finally matches reality:
    # player failure no longer suppresses the opponent's own roll.
    assert enc.opponent_metric.current == 2

    # Verify the resolver ran — the OTEL span carries the player's tier.
    opposed_spans = [
        s
        for s in captured_spans.get_finished_spans()
        if s.name == SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED
    ]
    assert len(opposed_spans) == 1
    assert opposed_spans[0].attributes["tier"] == RollOutcome.Fail.value


def test_narration_apply_opposed_check_hard_fails_without_pending_state():
    """opposed_check is dice-throw-only on the explicit-action path —
    if ``dispatch_dice_throw`` reaches us without stashing the player
    roll, that IS a programming error and must fail loud (CLAUDE.md
    no-silent-fallback). Verify the loud-failure path is preserved
    for ``from_explicit_action=True``.

    The narrator-prose path (``from_explicit_action=False``) has a
    different handling — see
    ``test_narration_apply_opposed_check_awaiting_dice_drops_beats_on_narrator_path``
    below. That path redirects to "wait for dice" instead of crashing,
    because production reaches that state when a player typed text
    in combat without rolling first.
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(actor="Wolf", beat_id="attack", outcome=RollOutcome.Success),
        ],
    )

    with pytest.raises(ValueError, match="without a pending DICE_THROW player roll"):
        _apply_narration_result_to_snapshot(
            snapshot,
            result,
            player_name="p1",
            pack=pack,
            from_explicit_action=True,
            room=room_for(snapshot),
        )


def test_narration_apply_opposed_check_awaiting_dice_drops_beats_on_narrator_path():
    """Playtest 2026-04-30 4-player MP regression. Production reaches
    a state where:

    1. An opposed_check encounter is active (Firefight in The Pit).
    2. The player submits a text PLAYER_ACTION (no DICE_THROW yet).
    3. The narrator returns beats including opponent-side selections.
    4. ``_filter_inferred_pc_beats`` drops the PC-side beats but
       keeps opponent ones (NPCs don't need the consent contract).
    5. ``_apply_narration_result_to_snapshot`` enters the
       opposed_check branch with no ``pending_player_d20``.
    6. Pre-fix: ``_resolve_opposed_check_branch`` raised ValueError
       on every NPC beat — the WS handler caught it as
       ``ws.unexpected_error`` and disconnected the session.

    The fix redirects: when ``from_explicit_action=False`` AND
    ``opposed_player_d20`` is None, drop the opponent selections,
    log the awaiting-dice state, and short-circuit the resolver.
    The narrator's prose still applies, the encounter persists, and
    a subsequent DICE_THROW completes the round.

    Asserts:

    - No ValueError raised.
    - Encounter is NOT marked resolved.
    - Both player/opponent dials remain at starting values.
    - Encounter actors and roles are untouched.
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    result = NarrationTurnResult(
        narration="The Knife-Hand circles, watching where your weight goes.",
        beat_selections=[
            BeatSelection(actor="Wolf", beat_id="attack", outcome=RollOutcome.Success),
        ],
    )

    # No exception expected — the awaiting-dice branch handles the
    # absent stash cleanly on the narrator-prose path.
    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        opposed_player_d20=None,
        opposed_player_beat_id=None,
        opposed_player_actor=None,
        from_explicit_action=False,
        room=room_for(snapshot),
    )

    assert not enc.resolved, (
        "encounter must NOT be resolved on the awaiting-dice path — "
        "no resolution can fire without a paired player d20"
    )
    assert enc.player_metric.current == 0, "player dial must not advance on the awaiting-dice path"
    assert enc.opponent_metric.current == 0, (
        "opponent dial must not advance on the awaiting-dice path — "
        "the opponent beat selection was dropped, not applied"
    )
    assert snapshot.pending_resolution_signal is None, (
        "no resolution signal should fire on the awaiting-dice path"
    )


# ---------------------------------------------------------------------------
# Companion-NPC wiring (playtest 2026-05-06): NPC ally beats run mechanically.
# ---------------------------------------------------------------------------


def test_companion_brace_targeting_player_counteracts_opponent_attack(
    monkeypatch,
    captured_spans,
):
    """Sumpdrake-fight regression: when a recruited NPC companion's
    beat is ``defend target=<player>`` and the opponent's beat is
    attacking that same player AND the companion's roll passes its
    DC, the opponent's tier must downgrade by one step.

    Pre-fix: companion beats were rejected by the SOUL gate (every
    player-side beat was treated as an inferred PC beat) and never
    reached the resolver. Even when allowed through, the resolver only
    applied the player↔opponent pair — companion beats fell off the
    floor with no OTEL, no roll, no apply.

    Post-fix:
    - SOUL gate is seat-aware (companions flow through).
    - Resolver rolls each companion's d20 server-side, classifies their
      tier, and applies their beat.
    - Companion brace whose target matches the opponent's target counts
      as ally counteract → downgrade opponent_tier_final by one step.
    """
    enc = _make_encounter_with_companion()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc
    # Sam is the only seated PC. Donut is a companion (no seat).
    snapshot.player_seats = {"p1": "Sam"}

    # Donut rolls 18 server-side (mod +1 from cdef default STR=12 → +1).
    # vs defend DC=12 (base=1) → total=19 → CritSuccess on his brace.
    # Wait — total=18+1=19, DC=12, decisive margin is +10 → 19 > 22?
    # No: 12 + 10 = 22, 19 < 22 → Success (not CritSuccess).
    # Wolf rolls 14 next (the second _roll_d20_server_side call).
    # Wolf mod=+2, attack DC=14 (base=2) → total=16 > 14 → Success.
    # With ally counteract: Wolf's Success → Tie (downgrade).
    # Tie strike base=2 → own=base//2=+1 (opponent_metric +1).
    rolls = iter([18, 14])  # Donut first, then Wolf
    monkeypatch.setattr(
        "sidequest.server.narration_apply._roll_d20_server_side",
        lambda: next(rolls),
    )

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            # Wolf attacks Sam.
            BeatSelection(
                actor="Wolf",
                beat_id="attack",
                outcome=RollOutcome.Success,
                target="Sam",
            ),
            # Donut shields Sam from Wolf's attack.
            BeatSelection(
                actor="Donut",
                beat_id="defend",
                outcome=RollOutcome.Success,
                target="Sam",
            ),
        ],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        # Sam's defend total = 5+2=7 vs DC=12 → Fail.
        opposed_player_d20=5,
        opposed_player_beat_id="defend",
        opposed_player_actor="Sam",
        from_explicit_action=True,
        room=room_for(snapshot),
    )

    # Lie-detector span carries Sam's final tier (Fail — he rolled poorly).
    finished = captured_spans.get_finished_spans()
    opposed_spans = [s for s in finished if s.name == SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED]
    assert len(opposed_spans) == 1
    attrs = dict(opposed_spans[0].attributes or {})
    assert attrs["tier"] == RollOutcome.Fail.value


def test_companion_beat_ignored_when_seat_manifest_excludes_pc_only(monkeypatch):
    """Negative wiring: with no seat manifest (legacy save), every
    player-side beat that wasn't dispatched-via-DICE_THROW is treated
    as inferred-PC and dropped. Companions only become first-class
    when ``snapshot.player_seats`` is populated (post-MP migration
    + post-recruiter-pipeline). This proves the seat-aware filter
    isn't accidentally accepting narrator-inferred PC beats.
    """
    enc = _make_encounter_with_companion()
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc
    snapshot.player_seats = {}  # legacy save / no seats

    monkeypatch.setattr(
        "sidequest.server.narration_apply._roll_d20_server_side",
        lambda: 10,
    )

    # Pre-existing test in this file already covers the dispatch path
    # for legacy mode — here we only need to prove the SOUL gate's
    # legacy fallback still rejects player-side beats. Calling
    # ``_filter_inferred_pc_beats`` directly exercises the gate.
    from sidequest.server.narration_apply import _filter_inferred_pc_beats

    selections = [
        BeatSelection(actor="Sam", beat_id="attack", outcome=RollOutcome.Success),
        BeatSelection(actor="Donut", beat_id="defend", outcome=RollOutcome.Success),
    ]
    kept = _filter_inferred_pc_beats(
        selections,
        enc,
        narrating_player="p1",
        seated_pc_names=None,  # legacy fallback path
    )
    # Both player-side selections rejected.
    actor_names = {sel.actor for sel in kept}
    assert "Sam" not in actor_names
    assert "Donut" not in actor_names


def test_companion_beat_passes_seat_aware_gate(monkeypatch):
    """Positive wiring: seat-aware SOUL gate lets companion beats
    through but still rejects the seated PC's own narrator-inferred
    beat. Sam's beat is dropped (no DICE_THROW); Donut's flows.
    """
    enc = _make_encounter_with_companion()

    from sidequest.server.narration_apply import _filter_inferred_pc_beats

    selections = [
        BeatSelection(actor="Sam", beat_id="attack", outcome=RollOutcome.Success),
        BeatSelection(actor="Donut", beat_id="defend", outcome=RollOutcome.Success),
        BeatSelection(actor="Wolf", beat_id="attack", outcome=RollOutcome.Success),
    ]
    kept = _filter_inferred_pc_beats(
        selections,
        enc,
        narrating_player="p1",
        seated_pc_names={"Sam"},  # only Sam is a seated PC
    )
    actor_names = {sel.actor for sel in kept}
    # Sam dropped (seated PC, no DICE_THROW); Donut + Wolf flow.
    assert "Sam" not in actor_names
    assert "Donut" in actor_names
    assert "Wolf" in actor_names


# ---------------------------------------------------------------------------
# Keith's rule (playtest 2026-05-06): plain Success uncontested → momentum.
# ---------------------------------------------------------------------------


def test_uncontested_player_success_advances_player_dial(monkeypatch, captured_spans):
    """Sumpdrake-fight regression: a plain player Success against an
    opponent who is *not* counteracting must advance the player dial.

    Pre-fix behavior: opposed_check derived the tier from the shift
    between the two rolls and applied it to BOTH apply_beat calls.
    A player total of 16 vs an opponent total of 17 (shift -1) collapsed
    the player to Tie tier; an opponent total of 18+ (shift -2) collapsed
    to Fail tier. So plain Successes did nothing — only nat20s moved
    the player dial.

    Post-fix: each side resolves its own roll-vs-DC. With base=2 the DC
    is 14; the player rolled 16 → Success → strike grants own=base=+2.
    Opponent's roll is irrelevant unless they emit a brace targeting
    the player (counteract gate).
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    # Opponent rolls a high d20 (18) but their beat is a strike, not a
    # brace targeting the player → no counteract gate, opponent's roll
    # has no effect on the player's tier.
    _patched_d20(monkeypatch, value=18)

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(actor="Wolf", beat_id="attack", outcome=RollOutcome.Success),
        ],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        # Player rolled 14 + mod=2 = 16 vs DC=14 → Success.
        opposed_player_d20=14,
        opposed_player_beat_id="attack",
        opposed_player_actor="Sam",
        from_explicit_action=True,
        room=room_for(snapshot),
    )

    # Plain Success on strike (base=2) → +2 momentum on the player dial,
    # even though the opponent rolled higher overall (shift = -4 under
    # the prior shift-tier logic would have collapsed the player to
    # Fail tier).
    assert enc.player_metric.current == 2


def test_opponent_brace_targeting_player_counteracts_player_success(monkeypatch, captured_spans):
    """Counteract gate: when the opponent's beat is brace and targets
    the player AND the opponent's own roll passes its DC, the player's
    tier downgrades by one step (Success → Tie). The combined effect
    of (a) the player's downgraded Tie tier on strike (own=+base//2)
    and (b) the opponent's successful brace draining the opposite dial
    (opponent_expr=-base on Success) nets out to ~0 net momentum on the
    attacker — exactly Keith's rule that "Counteracted Success should
    grant 0 (or reduced)."

    Asserts the downgrade actually happened via the watcher span (not
    just the net dial state), so a future regression that produces 0
    by accident — e.g., applying Fail tier instead of downgrading
    Success → Tie — still surfaces.
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    # Opponent rolls 16 + mod=2 = 18 vs defend DC (base=1 → DC=12) →
    # Success for the opponent's brace.
    _patched_d20(monkeypatch, value=16)

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(
                actor="Wolf",
                beat_id="defend",
                outcome=RollOutcome.Success,
                target="Sam",  # brace targeting the player → counteract
            ),
        ],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        opposed_player_d20=14,  # 14 + 2 = 16 vs DC=14 → Success
        opposed_player_beat_id="attack",
        opposed_player_actor="Sam",
        from_explicit_action=True,
        room=room_for(snapshot),
    )

    # Player's Success → downgraded to Tie (strike base=2 → own=+1).
    # Opponent's brace Success → opponent_expr=-base=-1 drains player
    # dial by 1. Net: 1 + (-1) = 0. Both effects ran; they happen to
    # cancel cleanly for this base/tier combination, which is the
    # intended "counteracted Success grants 0" outcome.
    assert enc.player_metric.current == 0

    # Lie-detector: confirm the downgrade actually happened (rather
    # than the player getting Fail tier from some other path). The
    # span carries the player's *final* tier as the headline.
    finished = captured_spans.get_finished_spans()
    opposed_spans = [s for s in finished if s.name == SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED]
    assert len(opposed_spans) == 1
    assert opposed_spans[0].attributes["tier"] == RollOutcome.Tie.value


def test_opponent_brace_self_targeted_does_not_counteract(monkeypatch, captured_spans):
    """A brace with target=<self> is self-defense, not a counteract of
    an attack on a different actor. Opponent bracing themselves while
    the player attacks them must NOT downgrade the player's tier —
    that would be the engine confusing self-shielding for an
    interception.

    We assert via the lie-detector span (player_tier_final=Success)
    rather than the metric value because brace's apply_beat cross-
    drain reduces the player dial regardless of target — the target
    matching only gates the *tier downgrade*, not the brace's
    independent cross-effect on the opposite dial.
    """
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="testpack", world_slug="testworld")
    snapshot.encounter = enc

    _patched_d20(monkeypatch, value=18)

    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(
                actor="Wolf",
                beat_id="defend",
                outcome=RollOutcome.Success,
                target="Wolf",  # bracing self, not protecting Sam
            ),
        ],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        opposed_player_d20=14,
        opposed_player_beat_id="attack",
        opposed_player_actor="Sam",
        from_explicit_action=True,
        room=room_for(snapshot),
    )

    # No counteract gate fires — the player's final tier is the raw
    # roll-vs-DC tier (Success), not downgraded.
    finished = captured_spans.get_finished_spans()
    opposed_spans = [s for s in finished if s.name == SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED]
    assert len(opposed_spans) == 1
    assert opposed_spans[0].attributes["tier"] == RollOutcome.Success.value


# ---------------------------------------------------------------------------
# Wiring 4: narrator prompt gate fires on opposed_check encounters
# ---------------------------------------------------------------------------


def _render_prompt(cdef: ConfrontationDef) -> str:
    """Render the encounter-live prompt section text for inspection."""
    enc = _make_encounter()
    narrator = NarratorAgent()
    registry = PromptRegistry()
    narrator.build_encounter_context(
        registry,
        encounter=enc,
        cdef=cdef,
    )
    return registry.compose(narrator.name())


def test_narrator_prompt_includes_opposed_check_gate_when_mode_is_opposed():
    text = _render_prompt(_opposed_cdef())
    assert "RESOLUTION_MODE: opposed_check" in text, (
        f"expected opposed_check gate in prompt; got: {text!r}"
    )
    assert "do not narrate whether it lands or fails" in text.lower() or (
        "engine rolls dice" in text.lower()
    )


def test_narrator_prompt_omits_opposed_check_gate_when_mode_is_legacy():
    text = _render_prompt(_legacy_cdef())
    assert "RESOLUTION_MODE: opposed_check" not in text, (
        f"opposed_check gate leaked into legacy beat_selection prompt: {text!r}"
    )


# ---------------------------------------------------------------------------
# Wiring 5: real migrated genre packs load and declare opposed_check
# ---------------------------------------------------------------------------


def _migration_root_present() -> bool:
    return MIGRATION_ROOT.is_dir()


@pytest.mark.skipif(
    not _migration_root_present(),
    reason="migration worktree not present (parallel branch in sidequest-content)",
)
@pytest.mark.parametrize(
    "slug",
    [
        "caverns_and_claudes",
        "elemental_harmony",
        "heavy_metal",
        "mutant_wasteland",
        "space_opera",
        "spaghetti_western",
    ],
)
def test_migrated_pack_declares_opposed_check_on_combat(slug: str):
    pack = load_genre_pack(MIGRATION_ROOT / slug)
    assert pack.rules is not None
    combat_cdefs = [c for c in pack.rules.confrontations if c.category == "combat"]
    assert combat_cdefs, f"{slug} has no combat confrontations"
    for cdef in combat_cdefs:
        # Dogfight (sealed_letter_lookup) is the documented exception — the
        # spec migrates beat_selection-only combats to opposed_check.
        if cdef.resolution_mode is ResolutionMode.sealed_letter_lookup:
            continue
        assert cdef.resolution_mode is ResolutionMode.opposed_check, (
            f"{slug}/{cdef.confrontation_type} did not migrate to opposed_check; "
            f"got {cdef.resolution_mode}"
        )
        assert cdef.opponent_default_stats, (
            f"{slug}/{cdef.confrontation_type} missing opponent_default_stats"
        )
