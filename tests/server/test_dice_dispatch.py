"""Unit + wiring tests for DICE_THROW dispatch.

Story 34 / 2026-04-24 port: Rust dice_dispatch → Python. Covers the pure
dispatcher (dispatch_dice_throw) and the session-handler integration point
(WebSocketSessionHandler._handle_dice_throw). A wiring test asserts that an
inbound DICE_THROW fans out DiceRequest + DiceResult to the whole room so
multiplayer spectators can see rolls in real time.

Task 12 (2026-04-25): Rewritten for dual-dial encounters. The module-level
skip added in Task 8 (MetricDirection removed) is lifted here. Tests that
were narrowly coupled to the old single-dial / failure_metric_delta schema
are individually skipped with per-test markers; a Phase-3 cleanup story
will remove the skip annotations once the fixture pack migration lands.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    RulesConfig,
)
from sidequest.protocol.dice import (
    DiceThrowPayload,
    RollOutcome,
    ThrowParams,
)
from sidequest.protocol.messages import DiceRequestMessage, DiceResultMessage
from sidequest.server.dispatch.dice import (
    DiceDispatchError,
    dispatch_dice_throw,
)


def _make_snapshot() -> GameSnapshot:
    """Minimal GameSnapshot for dispatch_dice_throw tests.

    Story 45-9: dispatch_dice_throw now requires a snapshot so it can
    bump ``total_beats_fired`` on each successful beat fire.
    """
    return GameSnapshot(
        genre_slug="test",
        world_slug="test",
        turn_manager=TurnManager(),
    )


def _pack_with_combat() -> object:
    """Minimal GenrePack-shaped stub with dual-dial confrontation.

    Uses the new BeatDef schema (kind + base, no metric_delta).
    """
    cdef = ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "kick_door",
                    "label": "Kick Door",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STRENGTH",
                }
            ),
            BeatDef.model_validate(
                {
                    "id": "unknown_stat",
                    "label": "Unknown Stat Beat",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "   ",  # blank — used for validation test
                }
            ),
        ],
    )
    rules = MagicMock(spec=RulesConfig)
    rules.confrontations = [cdef]
    pack = MagicMock()
    pack.rules = rules
    return pack


def _make_encounter(*, player_current: int = 0) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=player_current,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[EncounterActor(name="Bob", role="combatant", side="player")],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _throw(face: int = 14, beat_id: str | None = "kick_door") -> DiceThrowPayload:
    return DiceThrowPayload(
        request_id="req-1",
        throw_params=ThrowParams(
            velocity=(0.0, 5.0, -2.0),
            angular=(1.0, 1.0, 1.0),
            position=(0.5, 0.5),
        ),
        face=[face],
        beat_id=beat_id,
    )


class TestDispatchDiceThrow:
    """Pure dispatcher — no session handler, no room."""

    def test_success_roll_resolves_and_applies_beat(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        outcome = dispatch_dice_throw(
            payload=_throw(face=13),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 16},  # +3 modifier
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="session-1",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        # 13 + 3 = 16 >= DC(10 + |2|*2 = 14) → Success (margin 2 < DECISIVE_MARGIN)
        assert outcome.outcome is RollOutcome.Success
        assert outcome.result.total == 16
        assert outcome.result.difficulty == 14
        assert outcome.request.modifier == 3
        assert outcome.request.rolling_player_id == "p1"
        # Beat applied: strike kind Success → own = base = 2 → player_metric: 0+2=2
        assert enc.player_metric.current == 2
        assert "[BEAT_RESOLVED] Kick Door" in outcome.replay_action_text
        assert "Roll: 16 (Success)" in outcome.replay_action_text

    def test_crit_success_bypasses_dc(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        # Face 20 with a huge negative modifier and a high DC — CritSuccess wins.
        outcome = dispatch_dice_throw(
            payload=_throw(face=20),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 1},  # -5 modifier
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        assert outcome.outcome is RollOutcome.CritSuccess

    def test_crit_fail_bypasses_modifier(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        outcome = dispatch_dice_throw(
            payload=_throw(face=1),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 30},  # +10 modifier
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        assert outcome.outcome is RollOutcome.CritFail

    def test_missing_beat_id_raises_dispatch_error(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        with pytest.raises(DiceDispatchError, match="missing beat_id"):
            dispatch_dice_throw(
                payload=_throw(beat_id=None),
                rolling_player_id="p1",
                character_name="Bob",
                character_stats={},
                encounter=enc,
                pack=pack,  # type: ignore[arg-type]
                genre_slug="test",
                session_id="s",
                round_number=1,
                room_broadcast=None,
                snapshot=_make_snapshot(),
            )

    def test_no_active_encounter_raises(self) -> None:
        pack = _pack_with_combat()
        with pytest.raises(DiceDispatchError, match="active encounter"):
            dispatch_dice_throw(
                payload=_throw(),
                rolling_player_id="p1",
                character_name="Bob",
                character_stats={"STRENGTH": 10},
                encounter=None,
                pack=pack,  # type: ignore[arg-type]
                genre_slug="test",
                session_id="s",
                round_number=1,
                room_broadcast=None,
                snapshot=_make_snapshot(),
            )

    def test_unknown_beat_id_raises(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        with pytest.raises(DiceDispatchError, match="unknown beat_id"):
            dispatch_dice_throw(
                payload=_throw(beat_id="does_not_exist"),
                rolling_player_id="p1",
                character_name="Bob",
                character_stats={"STRENGTH": 10},
                encounter=enc,
                pack=pack,  # type: ignore[arg-type]
                genre_slug="test",
                session_id="s",
                round_number=1,
                room_broadcast=None,
                snapshot=_make_snapshot(),
            )

    def test_invalid_stat_check_raises_without_mutating_encounter(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        prev_player = enc.player_metric.current
        prev_opp = enc.opponent_metric.current
        with pytest.raises(DiceDispatchError, match="invalid stat_check"):
            dispatch_dice_throw(
                payload=_throw(beat_id="unknown_stat"),
                rolling_player_id="p1",
                character_name="Bob",
                character_stats={},
                encounter=enc,
                pack=pack,  # type: ignore[arg-type]
                genre_slug="test",
                session_id="s",
                round_number=1,
                room_broadcast=None,
                snapshot=_make_snapshot(),
            )
        # Validate-then-mutate: encounter untouched on validation failure.
        assert enc.player_metric.current == prev_player
        assert enc.opponent_metric.current == prev_opp

    def test_case_insensitive_stat_lookup(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        # Character's stats dict uses TitleCase; stat_check is UPPERCASE.
        outcome = dispatch_dice_throw(
            payload=_throw(face=12),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"Strength": 14},  # +2 modifier via case-insensitive lookup
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        assert outcome.request.modifier == 2

    def test_broadcast_sends_dice_request_and_result_in_order(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        broadcasts: list[object] = []
        dispatch_dice_throw(
            payload=_throw(face=15),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 10},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s",
            round_number=1,
            room_broadcast=broadcasts.append,
            snapshot=_make_snapshot(),
        )
        # Spectators need the overlay to open before the result lands.
        # Story 45-3: a CONFRONTATION carrying post-apply momentum
        # follows DICE_RESULT on every non-deferred beat. Order is:
        #   DICE_REQUEST → DICE_RESULT → CONFRONTATION
        from sidequest.protocol.messages import ConfrontationMessage

        assert len(broadcasts) == 3
        assert isinstance(broadcasts[0], DiceRequestMessage)
        assert isinstance(broadcasts[1], DiceResultMessage)
        assert isinstance(broadcasts[2], ConfrontationMessage)
        assert broadcasts[0].payload.request_id == "req-1"
        assert broadcasts[1].payload.request_id == "req-1"
        # Mid-turn CONFRONTATION reflects post-apply momentum.
        assert broadcasts[2].payload.player_metric["current"] == enc.player_metric.current

    def test_player_action_prepended_to_replay_text(self) -> None:
        """D2 confrontation panel (2026-05-13): the freeform text the player
        typed into the InputBar before clicking a beat tile must reach the
        narrator. ``payload.player_action`` is the chandelier-swing — when
        present, dispatch prepends a ``PLAYER_ACTION:`` line above the
        synthetic ``[BEAT_RESOLVED]`` summary so the narrator runs with
        both the mechanical outcome AND the player's invention.
        """
        pack = _pack_with_combat()
        enc = _make_encounter()
        payload = _throw(face=13)
        payload = DiceThrowPayload(
            request_id=payload.request_id,
            throw_params=payload.throw_params,
            face=payload.face,
            beat_id=payload.beat_id,
            player_action="I swing from the chandelier into the Bruiser's chest",
        )
        outcome = dispatch_dice_throw(
            payload=payload,
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 16},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="session-1",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        text = outcome.replay_action_text
        assert text.startswith(
            "PLAYER_ACTION: I swing from the chandelier into the Bruiser's chest"
        )
        # And the mechanical beat summary still rides along, unchanged.
        assert "[BEAT_RESOLVED] Kick Door" in text
        assert "Roll: 16 (Success)" in text

    def test_player_action_empty_falls_back_to_beat_summary_only(self) -> None:
        """Whitespace-only or missing ``player_action`` MUST yield the
        legacy synthetic line unchanged — empty player text is not a cue
        the narrator should see."""
        pack = _pack_with_combat()
        for blank in (None, "", "   ", "\n\t"):
            payload_base = _throw(face=13)
            payload = DiceThrowPayload(
                request_id=payload_base.request_id,
                throw_params=payload_base.throw_params,
                face=payload_base.face,
                beat_id=payload_base.beat_id,
                player_action=blank,
            )
            enc_local = _make_encounter()
            outcome = dispatch_dice_throw(
                payload=payload,
                rolling_player_id="p1",
                character_name="Bob",
                character_stats={"STRENGTH": 16},
                encounter=enc_local,
                pack=pack,  # type: ignore[arg-type]
                genre_slug="test",
                session_id="session-1",
                round_number=1,
                room_broadcast=None,
                snapshot=_make_snapshot(),
            )
            assert outcome.replay_action_text.startswith("[BEAT_RESOLVED] Kick Door"), (
                f"player_action={blank!r} should fall through to synthetic only"
            )
            assert "PLAYER_ACTION" not in outcome.replay_action_text

    def test_encounter_resolves_when_beat_hits_threshold(self) -> None:
        pack = _pack_with_combat()
        # Threshold=10; push player_metric to 9 so a +2 strike crosses it.
        enc = _make_encounter(player_current=9)
        outcome = dispatch_dice_throw(
            payload=_throw(face=14),  # 14+0 = 14 >= DC=14 → Success (+2 own)
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 10},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        assert enc.resolved is True
        assert enc.structured_phase is EncounterPhase.Resolution
        assert outcome.encounter_resolved is True


class TestDiceThrowWireFormat:
    """Wire-format round-trip — the shape the UI consumes."""

    def test_result_serializes_with_expected_fields(self) -> None:
        pack = _pack_with_combat()
        enc = _make_encounter()
        outcome = dispatch_dice_throw(
            payload=_throw(face=15),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 10},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s",
            round_number=1,
            room_broadcast=None,
            snapshot=_make_snapshot(),
        )
        wire = DiceResultMessage(
            payload=outcome.result,
            player_id="server",
        ).model_dump_json()
        # Shape-check: every field the React UI reads from DiceResultPayload.
        # Matches sidequest-ui/src/types/payloads.ts::DiceResultPayload.
        for field_name in (
            '"request_id"',
            '"rolling_player_id"',
            '"character_name"',
            '"rolls"',
            '"modifier"',
            '"total"',
            '"difficulty"',
            '"outcome"',
            '"seed"',
            '"throw_params"',
        ):
            assert field_name in wire, f"missing wire field {field_name}"


# ---------------------------------------------------------------------------
# New dual-dial tests (Task 12)
# ---------------------------------------------------------------------------


def test_dice_throw_strike_player_advances_player_metric(dual_dial_test_setup):
    setup = dual_dial_test_setup
    outcome = setup.run_dice_throw(beat_id="attack", faces=[15], modifier=0)
    # DC for strike base=2 is 10 + 2*2 = 14; 15 → Success
    assert outcome.outcome.value == "Success"
    assert setup.encounter.player_metric.current == 2
    assert setup.encounter.opponent_metric.current == 0


def test_dice_throw_tie_at_dc_resolves_to_tie_tier(dual_dial_test_setup):
    setup = dual_dial_test_setup
    # DC 14 + face 14 + modifier 0 → Tie (graze: own += b // 2 = 1)
    outcome = setup.run_dice_throw(beat_id="attack", faces=[14], modifier=0)
    assert outcome.outcome.value == "Tie"
    assert setup.encounter.player_metric.current == 1


def test_dice_throw_critfail_strike_zero_metric(dual_dial_test_setup):
    setup = dual_dial_test_setup
    outcome = setup.run_dice_throw(beat_id="attack", faces=[1], modifier=0)
    assert outcome.outcome.value == "CritFail"
    assert setup.encounter.player_metric.current == 0
