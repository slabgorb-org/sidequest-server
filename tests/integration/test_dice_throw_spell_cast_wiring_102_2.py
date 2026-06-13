"""Story 102-2 AC4 — WIRING: a WebSocket-level DICE_THROW reaches the cast spine.

Every test suite needs a wiring test (CLAUDE.md): the dispatch-level proof in
tests/integration/test_dice_path_spell_cast_102_2.py calls
``dispatch_dice_throw`` directly — necessary but not sufficient. This suite
drives ``WebSocketSessionHandler.handle_message`` with a real
``DiceThrowMessage`` (the wire shape the UI sends) and asserts the cast spine
engaged: ``wwn.spell.cast`` fired and the caster's cast economy was spent.
If the handler→dispatch→spine chain has a missing link (field dropped in the
handler, payload not threaded, spine not called), this is the test that
catches it while the unit suite stays green.

Pattern: tests/server/test_dice_throw_wiring.py (session_handler_factory +
mocked narrator). The narrator is an AsyncMock — the intent-router pass never
runs on a beat-commit replay turn (router-SUPPRESSED, story 91-2), so no
router stub is needed; the mechanical seam under test is upstream of prose.
The Monster Manual pregen (ADR-059) is likewise stubbed to "not loaded": it
runs inside the narration turn (downstream of the dispatch chain under test)
and on the real heavy_metal pack with the factory's ``world_slug=""`` it
fail-louds on the missing world bestiary — correct production behavior, but
out of scope for this seam.

Lives in tests/integration/ (not tests/server/) because tests/server's
autouse ``_fixture_pack_search_paths`` repoints genre resolution at the
frozen fixture packs — this proof needs the REAL heavy_metal pack
(ruleset: wwn, cast_spell beat, spells_wwn catalog). Skips when
sidequest-content is not on disk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR

_SPELL = "wracking_bolt"
_SPAN_CAST = "wwn.spell.cast"

_STATS = {"STR": 12, "DEX": 10, "CON": 10, "INT": 14, "WIS": 10, "CHA": 10}

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


def _hydrate_caster(sd) -> None:
    """Give the factory's 'Rux' a WWN SpellcastingState + ability scores."""
    from sidequest.game.wwn_magic import SpellcastingState

    char = sd.snapshot.characters[0]
    char.stats.update(_STATS)
    char.core.spellcasting = SpellcastingState(
        prepared=[_SPELL],
        casts_remaining=2,
        casts_per_day=2,
        max_spell_level=1,
    )


def _install_combat(sd, opponent: str = "Furnace Thrall") -> None:
    """Seat a combat encounter with Rux vs a resolvable opponent core."""
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import Npc
    from sidequest.protocol.models import InitiativeEntry

    sd.snapshot.npcs.append(
        Npc(
            core=CreatureCore(
                name=opponent,
                description="A furnace-fed revenant.",
                personality="relentless",
                inventory=Inventory(),
                hp={"current": 10, "max": 10, "base_max": 10},
                armor_class=12,
            )
        )
    )
    sd.snapshot.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    # Story 106-2 (Option A): WWN hp_depletion combat resolves through the sealed
    # initiative walk; a WWN fight with no persisted order now fails loud. Seat a
    # deterministic order (Rux first, then the opponent) so the cast wiring drives
    # through the production walk exactly as a P4-seated fight would.
    sd.snapshot.encounter.initiative = [
        InitiativeEntry(token_id="Rux", value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]


def _cast_throw_message():
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.messages import DiceThrowMessage

    return DiceThrowMessage(
        payload=DiceThrowPayload(  # type: ignore[call-arg]
            request_id="wire-cast-102-2",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[11],
            beat_id="cast_spell",
            spell_id=_SPELL,
        ),
        player_id="player-1",
    )


@pytest.mark.asyncio
async def test_ws_dice_throw_with_spell_id_reaches_cast_spine(
    session_handler_factory, otel_capture, monkeypatch
):
    """handle_message(DICE_THROW + spell_id) must spend a cast and emit
    wwn.spell.cast — the full production chain, no direct dispatch call."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _State

    monkeypatch.setattr("random.randint", lambda a, b: a)
    # Monster Manual pregen runs in the narration turn, AFTER the dispatch
    # chain under test; on real heavy_metal with the factory's world_slug=""
    # it fail-louds on the missing world bestiary. Stub to "not loaded" so
    # injection skips (the production `if manual is not None` gate).
    monkeypatch.setattr(
        "sidequest.server.dispatch.monster_manual_inject.ensure_loaded",
        lambda _sd: None,
    )

    sd, handler = session_handler_factory(genre="heavy_metal")
    handler._state = _State.Playing
    _hydrate_caster(sd)
    _install_combat(sd)

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="The working takes its toll."),
    )

    await handler.handle_message(_cast_throw_message())

    char = sd.snapshot.characters[0]
    assert char.core.spellcasting.casts_remaining == 1, (
        "the WebSocket-level cast commit must spend exactly one cast through "
        "the handler→dispatch→spine chain; got "
        f"{char.core.spellcasting.casts_remaining}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert _SPAN_CAST in span_names, (
        f"a wire-level cast commit must emit {_SPAN_CAST} (the GM-panel lie "
        f"detector); got spans: {span_names}"
    )


@pytest.mark.asyncio
async def test_ws_dice_throw_spell_id_survives_wire_validation(
    session_handler_factory, otel_capture
):
    """The handler's inbound message validation must not strip or reject
    spell_id — a GameMessage round-tripped from raw wire JSON keeps it."""
    from sidequest.protocol.messages import DiceThrowMessage

    raw = {
        "type": "DICE_THROW",
        "player_id": "player-1",
        "payload": {
            "request_id": "wire-cast-raw",
            "throw_params": {
                "velocity": (0.0, 5.0, -2.0),
                "angular": (1.0, 1.0, 1.0),
                "position": (0.5, 0.5),
            },
            "face": [11],
            "beat_id": "cast_spell",
            "spell_id": _SPELL,
        },
    }
    msg = DiceThrowMessage.model_validate(raw)
    assert msg.payload.spell_id == _SPELL, (
        "spell_id must survive wire-shape validation — a stripped field would "
        "silently degrade every UI cast into the generic INT throw"
    )
