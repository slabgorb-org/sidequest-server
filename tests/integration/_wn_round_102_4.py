"""Shared fixture helpers for the 102-4 WN turn-model suite.

Builds real-pack (heavy_metal by default) WN combat encounters with one or
two PCs through the PRODUCTION seating seam (`instantiate_encounter_from_trigger`),
then drives `dispatch_dice_throw` per committed action. Initiative is forced
deterministically AFTER instantiation — the 1d8+DEX roll itself is the P4
spine's job and is already proven by tests/telemetry/test_initiative_rolled_span.py;
this suite tests what the ROUND does with the persisted order.

Lives in tests/integration/ because tests/server's autouse
``_fixture_pack_search_paths`` repoints genre resolution at frozen fixture
packs and this suite needs the REAL packs (ruleset: wwn et al., authored
opponent dexterity). Pattern: tests/integration/test_dice_path_spell_cast_102_2.py.
"""

from __future__ import annotations

from typing import Any

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

__all__ = [
    "GENRE_PACKS_DIR",
    "dispatch_throw",
    "force_initiative",
    "load_pack",
    "make_pc",
    "seat_npc_ally",
    "seat_wn_combat",
    "spans_named",
]

# heavy_metal "Blade-work" combat constants (authored in rules.yaml; the
# same block tests/integration/test_dice_path_spell_cast_102_2.py pins).
HM_STRIKE_BEAT = "committed_blow"  # strike, damage_override 2d6 (deterministic under a pinned rng)
HM_OPPONENT_HP = 10  # opponent_default_stats.hp
HM_STATS = {"STR": 12, "DEX": 10, "CON": 10, "INT": 14, "WIS": 10, "CHA": 10}


def load_pack(slug: str = "heavy_metal"):
    import pytest

    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path(slug))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def make_pc(name: str, *, hp: int = 12, stats: dict[str, int] | None = None):
    """A plain WN martial PC (no spellcasting) with an ablative HpPool."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name=name,
        description="A blade for hire.",
        personality="grim",
        inventory=Inventory(),
        hp={"current": hp, "max": hp, "base_max": hp},
        level=1,
    )
    return Character(
        core=core,
        char_class="Warrior",
        race="Human",
        backstory="—",
        stats=dict(stats or HM_STATS),
    )


def seat_wn_combat(
    pack,
    pc_names: list[str],
    opponents: list[str],
    *,
    encounter_type: str = "combat",
    genre_slug: str = "heavy_metal",
    pc_hp: int = 12,
    stats: dict[str, int] | None = None,
):
    """Seat a WN hp_depletion combat with N PCs + M opponents via production seam.

    Returns (snapshot, encounter). Opponent CreatureCores are seeded from the
    pack's authored opponent_default_stats by the instantiation seam.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    for name in pc_names:
        snap.characters.append(make_pc(name, hp=pc_hp, stats=stats))
        snap.character_locations[name] = "The Reliquary Gate"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=encounter_type,
        player_name=pc_names[0],
        additional_player_names=list(pc_names[1:]) or None,
        npcs_present=[NpcMention(name=o, side="opponent") for o in opponents],
        genre_slug=genre_slug,
    )
    assert enc is not None, "seating WN combat must produce an encounter"
    snap.encounter = enc
    for o in opponents:
        assert snap.find_creature_core(o) is not None, (
            f"opponent {o!r} core must resolve — the hp_depletion seeding seam "
            "is a precondition of this suite, not its subject"
        )
    return snap, enc


def seat_npc_ally(snap, enc, name: str, *, hp: int = 8, role: str = "ally") -> None:
    """Seat a friendly NPC ally on the player side — the coyote_star crew shape.

    Mirrors the story 59-35 ally seater (SOUL Guitar-Solo / ADR-116 friendly
    half): an ``Npc`` in ``snap.npcs`` plus a ``side="player"`` EncounterActor.
    Per 59-35 the ally carries no SWN ability scores and is NOT given an
    initiative slot — it acts on narrator beats, never seals a Main Action.
    The caller forces initiative WITHOUT this name (PC + opponent only), exactly
    as the live coyote_star ship_combat snapshot did (the deadlock repro).
    """
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import EncounterActor
    from sidequest.game.session import Npc

    core = CreatureCore(
        name=name,
        description="crew at the second seat",
        personality="steady",
        inventory=Inventory(),
        hp={"current": hp, "max": hp, "base_max": hp},
        level=1,
    )
    snap.npcs.append(Npc(core=core))
    enc.actors.append(EncounterActor(name=name, role=role, side="player"))


def force_initiative(enc, order: list[tuple[str, int]]) -> None:
    """Overwrite the persisted initiative with a deterministic order.

    The P4 spine rolls real 1d8+DEX at instantiation; tests force the order
    so kill sequences are reproducible (resume-safe-randomness analogue of
    the ADR-128 'seeded fixture' pattern — AC2 demands an exact order).
    """
    from sidequest.protocol.models import InitiativeEntry

    enc.initiative = [InitiativeEntry(token_id=n, value=v) for n, v in order]


def dispatch_throw(
    *,
    pack,
    snap,
    enc,
    character_name: str,
    player_id: str,
    beat_id: str = HM_STRIKE_BEAT,
    face: int = 13,
    request_id: str | None = None,
    genre_slug: str = "heavy_metal",
    stats: dict[str, int] | None = None,
    room_broadcast=None,
):
    """One PC's sealed Main Action arriving on the production dice seam."""
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    payload = DiceThrowPayload(
        request_id=request_id or f"req-102-4-{character_name}",
        throw_params=ThrowParams(
            velocity=(0.0, 5.0, -2.0),
            angular=(1.0, 1.0, 1.0),
            position=(0.5, 0.5),
        ),
        face=[face],
        beat_id=beat_id,
    )
    return dispatch_dice_throw(
        payload=payload,
        rolling_player_id=player_id,
        character_name=character_name,
        character_stats=dict(stats or HM_STATS),
        encounter=enc,
        pack=pack,
        genre_slug=genre_slug,
        session_id="s-102-4",
        round_number=1,
        room_broadcast=room_broadcast,
        snapshot=snap,
    )


def spans_named(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def span_start_order(otel_capture, *names: str) -> list[str]:
    """The given span names sorted by span start time (first started first).

    Asserting on START order (not finished-list order) keeps the assertion
    valid if the implementation nests child spans inside a round span.
    """
    spans = [s for s in otel_capture.get_finished_spans() if s.name in names]
    spans.sort(key=lambda s: s.start_time)
    return [s.name for s in spans]
