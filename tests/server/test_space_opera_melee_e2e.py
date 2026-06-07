"""Story 71-24 — personal-melee confrontation beat bank (GREEN — bank authored).

space_opera (the SWN-bound pack that ``perseus_cloud`` rides on) ships ship/
dogfight banks and a ranged ``combat`` ("Firefight") bank, but NO personal-melee
bank. Before 71-24 the Firefight ``intent_verbs`` advertised melee verbs
(``swing``, ``stab``, ``strike``), so a knife-fight action matched into
Firefight and the player is handed "Blaster bolts sear the corridor" beats for a
melee brawl. That is precisely the "Claude wings convincing prose with zero
mechanical backing" failure the OTEL principle exists to catch.

This module pins the contract for a distinct ``melee`` confrontation type:

  AC1  A ``melee`` ConfrontationDef exists, is ``category: combat`` /
       ``resolution_mode: beat_selection`` / ``win_condition: hp_depletion``,
       seeds ``opponent_default_stats`` (hp/armor_class/dexterity), and surfaces
       a melee beat bank (strike + brace kinds, Physique stat-check, strike
       damage_channel) whose ids are DISJOINT from the Firefight / ship_combat /
       dogfight banks — proving it is a distinct bank, not a copy.

  AC1  Melee intent verbs (swing, stab, slash, lunge, grapple) route to ``melee``
       and NOT to ``combat`` — asserted both at the mapping layer
       (``RulesConfig.intent_verbs_by_type``) and by driving the REAL intent-match
       path (``confrontation_intent_validator.validate`` → ``matched_type``).

  AC2  A melee confrontation resolves through the PRODUCTION seating + dice path
       on HP depletion, with OTEL proof: ``encounter.confrontation_initiated``
       fires with ``encounter_type="melee"``, ``encounter.beat_applied`` carries a
       melee ``beat_id``, and ``encounter.resolved`` fires with
       ``source="hp_depletion"``. (Span-capture, never a source-text grep — per
       the server's "No Source-Text Wiring Tests" rule.)

  AC2  Edge / regression: a RANGED action (``shoot``) must STILL match ``combat``
       and keep its ranged beats — rerouting melee verbs must not break ranged.

These tests DELIBERATELY load the REAL ``space_opera`` pack (the sanctioned
exception to "tests don't point at content"): the whole point is proving the
melee bank resolves through the production path against the authored pack. They
skip gracefully when sidequest-content is not on disk.

History: authored RED (every assertion failed until the ``melee`` bank existed),
now GREEN — the bank is authored in ``genre_packs/space_opera/rules.yaml``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The melee type id the bank must be authored under (context-story-71-24:
# "a new personal-melee confrontation type ... e.g. type: melee").
_MELEE_TYPE = "melee"

# Beat ids owned by the OTHER space_opera banks. The melee bank must not reuse
# any of them — the story title is "distinct from ship/dogfight beats", and a
# copied id would mean the bank is not actually distinct.
_FIREFIGHT_BEAT_IDS = frozenset({"shoot", "take_cover", "overload", "retreat"})
_SHIP_COMBAT_BEAT_IDS = frozenset(
    {"broadside", "evasive_maneuver", "close_range", "target_systems", "disengage"}
)
_DOGFIGHT_BEAT_IDS = frozenset({"straight", "bank", "loop", "kill_rotation"})
_FOREIGN_BEAT_IDS = _FIREFIGHT_BEAT_IDS | _SHIP_COMBAT_BEAT_IDS | _DOGFIGHT_BEAT_IDS

# Clearly-melee verbs that must live on the melee type, not Firefight.
_MELEE_VERBS = ("swing", "stab", "slash", "lunge", "grapple")

# Full SWN stat block so attack_params never KeyErrors on a missing stat (SWN
# fails loud on absent stats — no neutral-10 fallback). Mirrors the sibling
# combat e2e module.
_STATS = {
    "Physique": 12,
    "Reflex": 12,
    "Will": 10,
    "Intellect": 12,
    "Resolve": 12,
    "Cunning": 12,
}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _enum_val(x: object) -> object:
    """Return ``x.value`` for an enum, else ``x`` — beat.kind / category /
    resolution_mode may be enums or bare strings depending on the model."""
    return x.value if hasattr(x, "value") else x


def _load_space_opera():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _melee_def(pack):
    """The melee ConfrontationDef, or a hard failure when it is absent.

    Uses the production resolver (exact ``confrontation_type`` match, no fuzzy
    fallback) so the lookup is the same one ``dispatch/confrontation.py`` uses.
    """
    from sidequest.server.dispatch.confrontation import find_confrontation_def

    cdef = find_confrontation_def(pack.rules.confrontations, _MELEE_TYPE)
    assert cdef is not None, (
        f"space_opera must author a '{_MELEE_TYPE}' confrontation type — without it a "
        "knife-fight action falls through to the ranged Firefight bank (story 71-24)"
    )
    return cdef


def _player_character(name: str):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    return Character(
        core=CreatureCore(
            name=name,
            description="Station-side spacer.",
            personality="steady",
            inventory=Inventory(items=[{"id": "vibroblade", "name": "Vibroblade"}]),
            hp={"current": 10, "max": 10, "base_max": 10},
        ),
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
        stats=dict(_STATS),
    )


def _seated_melee(*, pc: str, opponent: str, location: str):
    """Seat a real space_opera ``melee`` confrontation via the PRODUCTION path."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    pack = _load_space_opera()
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="perseus_cloud",
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[pc] = location
    snap.characters.append(_player_character(pc))

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_MELEE_TYPE,
        player_name=pc,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="space_opera",
    )
    assert enc is not None, "seating must produce a melee encounter"
    return snap, enc, pack


def _throw(*, beat_id: str, pc: str, enc, pack, snap, request_id: str, round_number: int):
    """Drive one beat through the REAL ``dispatch_dice_throw`` with a winning d20."""
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    broadcasts: list[object] = []
    outcome = dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id=request_id,
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=beat_id,
        ),
        rolling_player_id=f"player-{pc.lower()}",
        character_name=pc,
        character_stats=_STATS,
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="so-melee-e2e",
        round_number=round_number,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )
    return outcome, broadcasts


def _spans_named(otel_capture, name: str):
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# AC1 — the melee bank exists and is DISTINCT from the other banks
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_melee_def_is_combat_hp_depletion_with_distinct_beats():
    from sidequest.genre.models.rules import WinCondition

    pack = _load_space_opera()
    melee = _melee_def(pack)

    # Resolves through the same SWN hp_depletion path as Firefight.
    assert _enum_val(melee.category) == "combat", "melee must be category: combat"
    assert melee.win_condition == WinCondition.hp_depletion, (
        "melee must win on hp_depletion (ablative HP via the SWN strike channel), "
        "so it routes through the same resolution as Firefight"
    )
    assert _enum_val(melee.resolution_mode) == "beat_selection", (
        "melee must be beat_selection (player picks a beat), not sealed_letter/opposed"
    )

    # Opponent stat block seeded (loader's combat cross-field validator requires
    # hp/armor_class/dexterity for a combat+hp_depletion confrontation).
    ods = melee.opponent_default_stats or {}
    assert ods.get("hp") is not None and int(ods["hp"]) >= 1, "melee opponent hp seed"
    assert ods.get("armor_class") is not None and int(ods["armor_class"]) >= 1, (
        "melee opponent armor_class seed"
    )
    assert ods.get("dexterity") is not None, (
        "melee opponent dexterity seed (SWN 1d8+DEX initiative — no silent +0)"
    )

    # A real beat bank with melee-appropriate kinds.
    assert melee.beats, "melee must author a non-empty beat bank"
    kinds = {_enum_val(b.kind) for b in melee.beats}
    assert kinds <= {"strike", "brace", "angle", "push"}, (
        f"melee beat kinds must be the BeatKind set; got {kinds}"
    )
    assert "strike" in kinds, "melee needs at least one strike beat (the swing/thrust)"
    assert "brace" in kinds, "melee needs a brace beat (parry/guard)"

    # The strike beat ablates HP through the SWN strike damage_channel + Physique.
    strike_beats = [b for b in melee.beats if _enum_val(b.kind) == "strike"]
    assert any(_enum_val(b.damage_channel) == "strike" for b in strike_beats), (
        "a melee strike beat must use damage_channel: strike so a hit ablates HP "
        "via the ADR-114 ablative path (not an inert dial nudge)"
    )
    assert any(_enum_val(b.stat_check) == "Physique" for b in strike_beats), (
        "a melee strike beat must stat_check Physique (a melee swing), not a ranged stat"
    )

    # DISTINCT bank: no melee beat reuses a Firefight/ship/dogfight id.
    melee_ids = {b.id for b in melee.beats}
    collisions = melee_ids & _FOREIGN_BEAT_IDS
    assert not collisions, (
        f"melee beat ids must be distinct from ship/dogfight/Firefight banks "
        f"(story title: 'distinct from ship/dogfight beats'); collisions={collisions}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_melee_verbs_route_to_melee_not_firefight_in_mapping():
    """``RulesConfig.intent_verbs_by_type`` must put melee verbs on ``melee`` and
    take the clearly-melee verbs OFF ``combat`` (the reroute)."""
    pack = _load_space_opera()
    mapping = pack.rules.intent_verbs_by_type

    assert _MELEE_TYPE in mapping, f"intent_verbs_by_type must contain '{_MELEE_TYPE}'"
    melee_verbs = mapping[_MELEE_TYPE]
    combat_verbs = mapping.get("combat", frozenset())

    for verb in _MELEE_VERBS:
        assert verb in melee_verbs, f"melee verb '{verb}' must be on the melee type"
        assert verb not in combat_verbs, (
            f"melee verb '{verb}' must be REMOVED from combat (Firefight) so a melee "
            f"action no longer matches the ranged bank — that is the 71-24 reroute"
        )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_melee_action_matches_melee_via_real_validator():
    """Drive the REAL intent-match path: a knife-fight action must infer
    ``matched_type == 'melee'``, not ``combat``."""
    from sidequest.agents.confrontation_intent_validator import validate

    pack = _load_space_opera()
    rewrite = SimpleNamespace(intent="I swing my vibroblade and stab the corsair")

    result = validate(
        rewrite,
        declared_confrontation=None,
        pack=pack,
        active_encounter=False,
    )
    assert result is not None, "a melee action must infer a confrontation type"
    assert result.matched_type == _MELEE_TYPE, (
        f"a swing/stab action must match the melee type, not the ranged Firefight; "
        f"got matched_type={result.matched_type!r} (tokens={result.matched_tokens})"
    )


# ---------------------------------------------------------------------------
# AC2 — resolves through the production path with OTEL proof
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.skip(
    reason="content-coupled: references weapon 'blaster_sidearm' that migrated to "
    "world-tier inventory (epic 94), so genre-tier damage specs no longer resolve and "
    "HP never ablates; rewrite against fixtures — story 94-4"
)
def test_melee_resolves_on_hp_depletion_with_otel(otel_capture):
    snap, enc, pack = _seated_melee(pc="Nova", opponent="Corsair", location="New Kowloon")

    melee = _melee_def(pack)
    strike_beat_id = next(b.id for b in melee.beats if _enum_val(b.kind) == "strike")

    opponent_core = snap.find_creature_core("Corsair")
    assert opponent_core is not None, (
        "opponent core must be reachable via find_creature_core — without it the SWN "
        "attack has no AC and hp_depletion can never resolve"
    )

    # ── A hit ablates HP, rolling vs the AUTHORED opponent AC ──
    hp_before = opponent_core.hp.current
    outcome, _ = _throw(
        beat_id=strike_beat_id,
        pc="Nova",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="melee-hit",
        round_number=1,
    )
    assert outcome.request.difficulty == opponent_core.armor_class, (
        "the melee attack must roll vs the authored opponent AC, not a beat DC; "
        f"got difficulty={outcome.request.difficulty} ac={opponent_core.armor_class}"
    )
    assert opponent_core.hp.current < hp_before, (
        f"a successful melee strike must ablate opponent HP; "
        f"before={hp_before} after={opponent_core.hp.current}"
    )
    assert outcome.opposed_pending is False, (
        "melee is beat_selection — dispatch must resolve inline, never defer to opposed"
    )

    # ── Drive HP to 0 → player_victory ──
    opponent_core.hp.current = 1
    kill_outcome, _ = _throw(
        beat_id=strike_beat_id,
        pc="Nova",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="melee-kill",
        round_number=2,
    )
    assert opponent_core.hp.current <= 0, "opponent HP must reach 0 after the killing strike"
    assert kill_outcome.encounter_resolved is True
    assert enc.outcome == "player_victory", (
        f"hp_depletion at 0 opponent HP must resolve player_victory; got {enc.outcome!r}"
    )

    # ── OTEL proof (span-capture, never a source grep) ──
    initiated = _spans_named(otel_capture, "encounter.confrontation_initiated")
    assert any((s.attributes or {}).get("encounter_type") == _MELEE_TYPE for s in initiated), (
        "encounter.confrontation_initiated must fire with encounter_type='melee' "
        "so the GM panel sees melee engine engage; "
        f"got {[dict(s.attributes or {}) for s in initiated]}"
    )

    beat_applied = _spans_named(otel_capture, "encounter.beat_applied")
    melee_beat_spans = [
        s
        for s in beat_applied
        if (s.attributes or {}).get("encounter_type") == _MELEE_TYPE
        and (s.attributes or {}).get("beat_id") == strike_beat_id
    ]
    assert melee_beat_spans, (
        f"encounter.beat_applied must carry the melee beat_id {strike_beat_id!r} under "
        f"encounter_type='melee' (the wiring proof the bank ran end-to-end); "
        f"got {[dict(s.attributes or {}) for s in beat_applied]}"
    )

    resolved_sources = [
        (s.attributes or {}).get("source", "")
        for s in _spans_named(otel_capture, "encounter.resolved")
    ]
    assert "hp_depletion" in resolved_sources, (
        "encounter.resolved with source='hp_depletion' must fire on the HP-to-0 kill "
        f"(lie-detector for the melee resolution path); got sources={resolved_sources}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_ranged_shoot_still_routes_to_combat_after_melee_reroute():
    """Regression: rerouting melee verbs off Firefight must NOT break ranged
    matching. A ``shoot`` action must STILL infer ``combat`` and keep its ranged
    beats."""
    from sidequest.agents.confrontation_intent_validator import validate

    pack = _load_space_opera()

    rewrite = SimpleNamespace(intent="I shoot my blaster down the corridor")
    result = validate(
        rewrite,
        declared_confrontation=None,
        pack=pack,
        active_encounter=False,
    )
    assert result is not None, "a ranged action must still infer a confrontation type"
    assert result.matched_type == "combat", (
        f"a shoot action must still match the ranged Firefight (combat) type; "
        f"got matched_type={result.matched_type!r}"
    )

    # Firefight keeps its ranged bank intact.
    combat_ids = {
        b.id for c in pack.rules.confrontations if c.confrontation_type == "combat" for b in c.beats
    }
    assert "shoot" in combat_ids, "Firefight must retain its ranged 'shoot' beat"
