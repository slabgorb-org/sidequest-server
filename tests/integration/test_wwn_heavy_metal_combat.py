"""Heavy Metal → WWN Story 1 — end-to-end combat wiring proof.

Drives the REAL heavy_metal pack (ruleset: wwn) through the production seating
seam (instantiate_encounter_from_trigger) and the production dice seam
(dispatch_dice_throw) with the converted Blade-work combat
(beat_selection / hp_depletion). Proves on the real bound pack:

  1. pack.rules.ruleset == "wwn";
  2. the opponent seats with hp/armor_class from opponent_default_stats;
  3. a strike beat ablates the opponent's HP through the HP channel;
  4. the state_patch.hp span fires (the GM-panel lie detector).

The strike beat under test ("committed_blow") carries a deterministic
damage_override (2d6) so the proof does not depend on weapon-catalog plumbing;
rng is pinned so the damage roll is deterministic.

Skips cleanly when sidequest-content is not present on disk.

The ``otel_capture`` fixture is re-exported from ``tests/integration/conftest.py``
(itself re-exporting from ``tests.server.conftest``).
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Authored on heavy_metal Blade-work opponent_default_stats (rules.yaml, Task 2).
_OPPONENT_HP = 10
_OPPONENT_AC = 12
_STRIKE_BEAT = "committed_blow"  # strike, damage_override 2d6 (deterministic)

# Seating-time lie-detector: an hp_depletion combat seated an opponent with NO
# resolvable reprisal damage source (no opponent_damage, no strike damage_override,
# weaponless seeded mook). Fires at instantiation. A lethality: high combat that
# emits this is shipping a Toothless Other (ADR-139 Invariant 3 / playtest-#16 /
# space_opera-67-10) — the enemy lands hits for 0 HP and the player is invulnerable.
_SPAN_TOOTHLESS = "encounter.opponent_toothless"
# Per-reprisal lie-detector: the opponent's server-driven to-hit decision. Fires
# once per opponent attack turn (story 71-21). Its presence proves the reprisal
# actually ran (not narrator improv); its absence on a seated hp_depletion combat
# means the enemy never swung.
_SPAN_OPPONENT_ATTACK = "encounter.opponent_attack_resolved"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_attacker(name: str, *, armor_class: int = 10):
    """Synthetic Warrior attacker. Story 1 has no classes.yaml yet (Story 2),
    so the attacker is built directly, not through CharacterBuilder.

    ``armor_class`` is the attacker's *defensive* AC — the ``target_ac`` the
    seated opponent rolls against on its reprisal turn. The default 10
    (CreatureCore unarmored) leaves the existing player→opponent test unchanged;
    the reprisal test passes a low AC so the opponent's answer is a guaranteed
    hit and the proof keys on damage, not on a lucky d20."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name=name,
        description="A blade-bearer of a house that is ending.",
        personality="grim",
        inventory=Inventory(),
        hp={"current": 12, "max": 12, "base_max": 12},
        armor_class=armor_class,
    )
    # WWN seating rolls initiative (1d8 + DEX) off character.stats, so the six
    # ability scores must be populated even though Story 1 has no classes.yaml
    # to build through. STR 12 matches the character_stats passed to the dice
    # seam below; the rest are flat 10s.
    return Character(
        core=core,
        char_class="Warrior",
        race="Human",
        backstory="—",
        stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_combat_is_wwn_bound_and_ablates_hp(otel_capture, monkeypatch):
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    pack = _load_heavy_metal()

    # ── Assertion 0: the pack is bound ruleset: wwn ───────────────────────
    assert pack.rules is not None
    assert pack.rules.ruleset == "wwn", (
        f"heavy_metal must be bound ruleset: wwn; got {pack.rules.ruleset!r}"
    )

    # ── Snapshot + attacker ───────────────────────────────────────────────
    attacker_name = "Sael"
    opponent = "The Collector's Blade"
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(_make_attacker(attacker_name))
    snap.character_locations[attacker_name] = "The Antechamber"

    # ── Seat the real Blade-work combat via the production seam ────────────
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=attacker_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc
    # Story 102-4: the WN sealed round resolves in the PERSISTED initiative
    # order, and the seam's real 1d8+DEX roll is unseeded — pin the order this
    # suite's choreography assumes (player strikes, opponent answers) so the
    # walk order can never flip on a random roll.
    from sidequest.protocol.models import InitiativeEntry

    enc.initiative = [
        InitiativeEntry(token_id=attacker_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]

    opponent_core = snap.find_creature_core(opponent)
    assert opponent_core is not None, (
        "opponent core must be reachable via find_creature_core — without it the "
        "strike has no defender HP to ablate"
    )
    assert opponent_core.armor_class == _OPPONENT_AC, (
        "opponent AC must come from opponent_default_stats"
    )
    assert opponent_core.hp.current == _OPPONENT_HP, (
        "opponent HP must come from opponent_default_stats"
    )

    # ── Pin rng: the damage faces are rolled by
    # ``damage_roll.generate_server_faces`` via ``random.randint`` (NOT in the
    # dice module). Pin to MIN (1 per die = 2 on 2d6) so the opponent survives
    # the ablation and the downed seam is not tripped (this story proves
    # ablation, not the kill path). The d20 attack uses the provided face=[20],
    # not rng. ─────────────────────────────────────────────────────────────
    monkeypatch.setattr("sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a)

    hp_before = opponent_core.hp.current

    # face high enough to clear the strike DC (base=4 → DC = 10 + 4*2 = 18;
    # face 20 + STR mod clears it). character_stats passes the attacker's STR.
    broadcasts: list[object] = []
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="hm-wwn-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=_STRIKE_BEAT,
        ),
        rolling_player_id="player-sael",
        character_name=attacker_name,
        character_stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="hm-wwn-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )

    # ── Assertion 1: HP ablated through the HP channel ────────────────────
    assert opponent_core.hp.current < hp_before, (
        f"committed_blow must ablate the opponent's HP on the real wwn pack; "
        f"before={hp_before} after={opponent_core.hp.current}"
    )

    # ── Assertion 2: state_patch.hp span fired (the lie detector) ─────────
    finished = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished, (
        f"the wwn combat spine must emit a state_patch.hp span (GM-panel lie "
        f"detector); got spans: {finished}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_combat_seats_no_toothless_opponent(otel_capture):
    """RED rework (review-rejected GREEN): the Blade-work combat must seat an
    opponent that can actually hurt the player.

    ``lethality: high`` is the headline of this pack; a combat whose seated Other
    deals 0 HP contradicts genre truth (ADR-139 Invariant 3). The seating seam
    (``_seed_combat_hp_depletion_to_npcs``) fires ``encounter.opponent_toothless``
    the instant it seats an opponent with no resolvable reprisal damage source —
    no ``opponent_damage`` on the cdef, no ``damage_override`` on its strike beat,
    and a weaponless seeded mook. This test asserts that span stays SILENT.

    RED today: the converted combat authors no ``opponent_damage`` and its first
    strike beat carries no ``damage_override``, so the span fires at seating.
    GREEN once Dev authors ``opponent_damage`` (mirroring EH's
    ``{dice: "1d6", bonus: 0}``). The companion ``cdef.opponent_damage is not
    None`` assertion pins the content fix directly so the proof cannot pass on a
    refactor that merely silences the span.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    pack = _load_heavy_metal()

    # The real Blade-work `combat` ConfrontationDef must author opponent_damage —
    # the reprisal damage source for the weaponless seeded mook. RED today (None).
    cdef = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert cdef is not None, "heavy_metal must expose a 'combat' (Blade-work) confrontation"
    assert cdef.opponent_damage is not None, (
        "the lethality: high Blade-work combat must author `opponent_damage` so the "
        "seeded (weaponless) mook can ablate the player; got None — Toothless Other"
    )

    attacker_name = "Sael"
    opponent = "The Collector's Blade"
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(_make_attacker(attacker_name))
    snap.character_locations[attacker_name] = "The Antechamber"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=attacker_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"

    finished = [s.name for s in otel_capture.get_finished_spans()]
    assert _SPAN_TOOTHLESS not in finished, (
        "seating the lethality: high Blade-work combat must NOT flag a Toothless "
        f"Other — the opponent must have a resolvable damage source; got spans: {finished}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_opponent_reprisal_ablates_player_hp(otel_capture, monkeypatch):
    """RED rework: prove the opponent→player half of the combat contract.

    The original GREEN test proved only player→opponent ablation, so a Toothless
    Other passed review. Combat is a mutual exchange: after the player's
    ``committed_blow`` resolves, the seated opponent takes a server-driven attack
    turn (``_resolve_opponent_reprisal`` → ``ruleset.resolve_opponent_attack``,
    inherited by wwn from swn) and must ablate the PLAYER's HP through the strike
    channel.

    Determinism without touching the player's seam:
      * player AC = 2 → the opponent's d20-vs-AC always HITS regardless of its
        (content-authored) attack modifier;
      * the opponent's d20 is rolled by ``dice.random.randint`` (``rng`` is the
        dice module's ``random``) — pinned to MAX so the hit is unconditional;
      * damage faces roll through ``damage_roll.random.randint`` — pinned to MIN
        so the player's own 2d6 ``committed_blow`` deals only 2 (opponent survives
        at 8 HP, so the reprisal is not gated out by an already-resolved encounter)
        and the opponent's reprisal deals its minimum (still > 0 → player HP drops).

    RED today: the weaponless seeded mook + no ``opponent_damage`` →
    ``damage_spec is None`` → the hit lands but deals 0 HP
    (``opponent_damage_spec_missing``), so the player's HP is unchanged. GREEN once
    Dev authors ``opponent_damage``.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    pack = _load_heavy_metal()

    attacker_name = "Sael"
    opponent = "The Collector's Blade"
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    # AC 2: every opponent reprisal roll clears it → guaranteed hit, so the proof
    # keys on whether a hit DEALS HP (the toothless gap), not on the d20.
    snap.characters.append(_make_attacker(attacker_name, armor_class=2))
    snap.character_locations[attacker_name] = "The Antechamber"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=attacker_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc
    # Story 102-4: the WN sealed round resolves in the PERSISTED initiative
    # order, and the seam's real 1d8+DEX roll is unseeded — pin the order this
    # suite's choreography assumes (player strikes, opponent answers) so the
    # walk order can never flip on a random roll.
    from sidequest.protocol.models import InitiativeEntry

    enc.initiative = [
        InitiativeEntry(token_id=attacker_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]

    player_core = snap.find_creature_core(attacker_name)
    assert player_core is not None, "attacker core must be reachable to ablate"
    hp_before = player_core.hp.current

    # Pin the opponent's to-hit d20 (dice module random) to MAX → unconditional hit;
    # pin damage faces (damage_roll random) to MIN → player's 2d6 = 2 (opponent
    # survives at 8 so the reprisal isn't gated out) and the reprisal deals its min.
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)
    monkeypatch.setattr("sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a)

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="hm-wwn-reprisal-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=_STRIKE_BEAT,
        ),
        rolling_player_id="player-sael",
        character_name=attacker_name,
        character_stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="hm-wwn-reprisal-session",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    finished = [s.name for s in otel_capture.get_finished_spans()]

    # ── The reprisal actually ran (lie detector) ──────────────────────────
    assert _SPAN_OPPONENT_ATTACK in finished, (
        "the seated opponent must take a server-driven attack turn and emit "
        f"encounter.opponent_attack_resolved; got spans: {finished}"
    )

    # ── The hit ablated the PLAYER's HP through the strike channel ─────────
    assert player_core.hp.current < hp_before, (
        f"a lethality: high opponent must ablate the player's HP on a guaranteed "
        f"hit (Toothless Other if not); before={hp_before} "
        f"after={player_core.hp.current}"
    )
    assert SPAN_STATE_PATCH_HP in finished, (
        f"the opponent's damage must flow through the HP channel and emit a "
        f"state_patch.hp span; got spans: {finished}"
    )
