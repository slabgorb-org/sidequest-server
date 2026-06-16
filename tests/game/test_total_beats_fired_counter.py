"""Story 45-9: ``GameSnapshot.total_beats_fired`` increments on every beat
fire + emits an OTEL ``beat_fired`` watcher event.

Bug context — Playtest 3 (2026-04-27): the counter was defined on
``GameSnapshot`` (session.py:380) but never bumped anywhere — the
codebase audit found zero ``+=`` matches against the field. Three saves
showed ``total_beats_fired == 0`` despite real beats firing (Orin
resolved an ``extraction_panic`` trope after 5 beats). Any beat-gated
unlock — campaign maturity tiers in
``sidequest.game.world_materialization.derive_maturity`` is the live
consumer — silently never opened.

This module locks the fix in place:

1. ``test_record_beat_fired_increments_counter`` — pure unit: calling
   ``record_beat_fired`` N times leaves the counter at N.
2. ``test_record_beat_fired_emits_watcher_event`` — OTEL lie-detector:
   the watcher event fires with the new counter value, the beat id,
   and the source label so the GM panel can verify each fire.
3. ``test_legacy_narrator_beat_path_increments_counter`` — wire test
   for the most common production path: a narrator-driven encounter
   beat through ``_apply_narration_result_to_snapshot`` bumps the
   counter once per non-skipped ``apply_beat`` call.
4. ``test_dispatch_dice_throw_increments_counter`` — wire test for the
   explicit-PC-consent path: a successful ``DICE_THROW`` round bumps
   the counter once.

Together (3) and (4) cover the two production fire paths called out
in the codebase audit; (1) and (2) lock the helper itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    RulesConfig,
)
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test",
        world_slug="test",
        turn_manager=TurnManager(),
    )


def _make_pack_with_combat() -> GenrePack:
    """Minimal pack with one ``combat`` confrontation that has a strike beat
    (always advances the player dial on Success — keeps the wire test
    deterministic)."""
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "swing",
                    "label": "Swing",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STRENGTH",
                }
            ),
        ],
    )
    rules = MagicMock(spec=RulesConfig)
    rules.confrontations = [cdef]
    pack = MagicMock()
    pack.rules = rules
    return pack


def _make_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        actors=[
            EncounterActor(name="Bob", role="combatant", side="player"),
            EncounterActor(name="Hostile", role="hostile", side="opponent"),
        ],
    )


# ---------------------------------------------------------------------------
# 1. Pure unit: counter increments
# ---------------------------------------------------------------------------


class TestRecordBeatFiredCounter:
    def test_starts_at_zero(self) -> None:
        snap = _make_snapshot()
        assert snap.total_beats_fired == 0

    def test_record_beat_fired_increments_counter(self) -> None:
        snap = _make_snapshot()
        for i in range(5):
            new_value = snap.record_beat_fired(
                beat_id=f"beat_{i}",
                encounter_type="combat",
                turn=i,
                source="test",
            )
            assert new_value == i + 1
        assert snap.total_beats_fired == 5

    def test_n_fires_yields_counter_n(self) -> None:
        """AC #3: fire N beats, assert counter equals N."""
        snap = _make_snapshot()
        n = 7
        for i in range(n):
            snap.record_beat_fired(
                beat_id="b",
                encounter_type="combat",
                turn=i,
                source="test",
            )
        assert snap.total_beats_fired == n


# ---------------------------------------------------------------------------
# 2. OTEL watcher event emission
# ---------------------------------------------------------------------------


class TestRecordBeatFiredOtel:
    def test_record_beat_fired_emits_watcher_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict] = []

        def fake_publish(
            event_type: str,
            fields: dict,
            *,
            component: str = "x",
            severity: str = "info",
        ) -> None:
            captured.append(
                {
                    "event_type": event_type,
                    "fields": fields,
                    "component": component,
                    "severity": severity,
                }
            )

        # Patch the symbol that ``record_beat_fired`` lazy-imports.
        from sidequest.telemetry import watcher_hub

        monkeypatch.setattr(watcher_hub, "publish_event", fake_publish)

        snap = _make_snapshot()
        snap.record_beat_fired(
            beat_id="swing",
            encounter_type="combat",
            turn=3,
            source="narrator_beat",
        )

        assert len(captured) == 1
        evt = captured[0]
        assert evt["event_type"] == "state_transition"
        assert evt["component"] == "encounter"
        f = evt["fields"]
        assert f["op"] == "beat_fired"
        assert f["beat_id"] == "swing"
        assert f["encounter_type"] == "combat"
        assert f["turn"] == 3
        assert f["source"] == "narrator_beat"
        # AC #2: event carries the new counter value.
        assert f["total_beats_fired"] == 1


# ---------------------------------------------------------------------------
# 3. Wire test — legacy narrator beat path (_apply_narration_result_to_snapshot)
# ---------------------------------------------------------------------------


class TestLegacyNarratorBeatPathWiring:
    """AC #4: confirms the increment is reachable from the production
    fire path used when the narrator emits ``beat_selections`` for an
    active encounter (the most common path — Playtest 3's Orin trope
    fires went through here)."""

    def test_legacy_narrator_beat_path_increments_counter(self) -> None:
        from sidequest.agents.orchestrator import (
            BeatSelection,
            NarrationTurnResult,
        )
        from sidequest.protocol.dice import RollOutcome
        from sidequest.server.narration_apply import (
            _apply_narration_result_to_snapshot,
        )

        snap = _make_snapshot()
        snap.encounter = _make_encounter()
        pack = _make_pack_with_combat()

        # Narrator commits a Success swing for Bob this turn.
        result = NarrationTurnResult(
            narration="Bob swings.",
            beat_selections=[
                BeatSelection(
                    actor="Bob",
                    beat_id="swing",
                    outcome=RollOutcome.Success,
                ),
            ],
        )

        assert snap.total_beats_fired == 0
        _apply_narration_result_to_snapshot(
            snap,
            result,
            "Bob",
            pack=pack,
            from_explicit_action=True,  # bypass the SOUL gate; this is a
            # mechanical wire test, not a SOUL-gate test.,
            room=room_for(snap),
        )
        # Single beat fired in the narrator path → counter ticks once.
        assert snap.total_beats_fired == 1


# ---------------------------------------------------------------------------
# 4. Wire test — dispatch_dice_throw path
# ---------------------------------------------------------------------------


class TestDispatchDiceThrowWiring:
    """AC #4: confirms the increment is reachable from the explicit-PC
    DICE_THROW path."""

    def test_dispatch_dice_throw_increments_counter(self) -> None:
        from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
        from sidequest.server.dispatch.dice import dispatch_dice_throw

        pack = _make_pack_with_combat()
        enc = _make_encounter()
        snap = _make_snapshot()
        snap.encounter = enc

        payload = DiceThrowPayload(
            request_id="req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[15],  # 15 + 0 = 15 >= DC=14 → Success
            beat_id="swing",
        )

        assert snap.total_beats_fired == 0
        outcome = dispatch_dice_throw(
            payload=payload,
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 10},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s1",
            round_number=1,
            room_broadcast=None,
            snapshot=snap,
        )
        assert outcome.outcome.value == "Success"
        assert snap.total_beats_fired == 1
