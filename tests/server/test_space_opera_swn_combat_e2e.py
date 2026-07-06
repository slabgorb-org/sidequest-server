"""SWN ruleset binding, Task 10 — end-to-end wiring proof.

Story 96-1: the combat e2e tests drive the ``swn_test_pack`` FIXTURE (with
``test_world`` world-tier inventory carrying ``blaster_sidearm``) so
content-only changes can never turn them red. Only Test 4
(``test_world_loads_clean_under_swn``) still deliberately loads the REAL
space_opera pack — it is a content-loads-clean smoke and the sanctioned
exception, gated on content presence.

What is proven here (the payoff of Tasks 1-9):

  Test 1  Personal Firefight (``combat``) resolves on HP depletion:
          - the attack rolls vs the AUTHORED opponent AC (12), not a beat DC;
          - a hit ablates the opponent's runtime CreatureCore hp.current;
          - bringing the opponent to 0 HP resolves ``player_victory`` and fires
            ``encounter.resolved`` with ``source="hp_depletion"`` (asserted via
            the OTEL span-capture fixture, never a source-text grep);
          - the opposed_check branch is NOT reached (combat is beat_selection
            now) — asserted behaviorally via ``DiceThrowOutcome.opposed_pending
            is False`` plus the positive hp_depletion resolution proof.

  Test 2  Ship combat (``ship_combat``) resolves on HULL depletion (hull = HP):
          - attack vs ship AC (14); hull ablates; 0 hull resolves via the same
            hp_depletion span.

  Test 3  The mid-turn CONFRONTATION payload emitted by the REAL dispatcher
          carries ``win_condition == "hp_depletion"`` and ``player_hp`` /
          ``opponent_hp`` dicts sourced from the real opponent/player cores —
          the production-path proof that ``core_resolver=snapshot.
          find_creature_core`` is threaded (deferred from the Task 6 review).

  Test 4  Both authored worlds load clean under ``ruleset: swn``.

Driving the real path WITHOUT the narrator: the combat seating runs through
``instantiate_encounter_from_trigger`` (which seeds the opponent's CreatureCore
hp/AC from content and registers it on the snapshot), and resolution runs
through ``dispatch_dice_throw`` with a controlled winning d20 face. No
Anthropic call is required — the dice-dispatch path applies the beat and
resolves hp_depletion inline (combat/ship_combat are ``beat_selection``, so the
narrator-deferred opposed_check fork is never taken).

Skips gracefully when sidequest-content is not present on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, find_pack_path

# Fixture-authored opponent stats (Task 9 reserved keys on
# opponent_default_stats in swn_test_pack rules.yaml — frozen from the values
# live space_opera shipped with).
_COMBAT_HP = 7
_COMBAT_AC = 12
_SHIP_HP = 30
_SHIP_AC = 14

# Full SWN-flavor stat block: covers every stat_check any combat / ship_combat
# strike beat declares (Physique for shoot, Intellect/Resolve for ship beats),
# so attack_params never KeyErrors on a missing stat (SWN module fails loud on
# absent stats — no neutral-10 fallback).
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


def _load_swn_fixture():
    from tests._helpers.fixture_packs import SWN_TEST_PACK, load_fixture_pack

    return load_fixture_pack(SWN_TEST_PACK)


def _player_character(name: str):
    """A player Character with a backing CreatureCore registered on the snapshot.

    The core lets ``snapshot.find_creature_core(name)`` resolve the player so
    the CONFRONTATION payload can surface ``player_hp`` (Test 3) and so the
    attacker_core is available to the SWN attack/damage path.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    return Character(
        core=CreatureCore(
            name=name,
            description="Station-side spacer.",
            personality="steady",
            inventory=Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}]),
            hp={"current": 10, "max": 10, "base_max": 10},
        ),
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
        # SWN P4: the player's DEX (Reflex flavor) must be resolvable at the
        # instantiation seam so initiative can roll 1d8+DEX for the PC.
        stats=dict(_STATS),
    )


def _seated_combat(*, encounter_type: str, pc: str, opponent: str, location: str):
    """Seat a swn_test_pack combat / ship_combat via the PRODUCTION path.

    Returns ``(snapshot, encounter, pack)``. The opponent's CreatureCore is
    seeded with the fixture-authored hp/AC and reachable via find_creature_core
    (Task 9), exactly as in live play. ``test_world`` is bound so weapon
    damage specs resolve through the world-tier inventory catalog (epic 94
    production shape).
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from tests._helpers.fixture_packs import SWN_TEST_PACK, TEST_WORLD

    pack = _load_swn_fixture()
    snap = GameSnapshot(
        genre_slug=SWN_TEST_PACK,
        world_slug=TEST_WORLD,
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[pc] = location
    snap.characters.append(_player_character(pc))

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=encounter_type,
        player_name=pc,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug=SWN_TEST_PACK,
        allow_synthetic_opponent=True,
    )
    assert enc is not None, "seating must produce an encounter"
    return snap, enc, pack


def _throw(*, beat_id: str, pc: str, enc, pack, snap, request_id: str, round_number: int):
    """Drive one beat through the REAL ``dispatch_dice_throw`` with a winning d20.

    face=[20] guarantees a hit vs the content AC so the strike-damage path fires.
    ``room_broadcast`` is captured into a list so callers can inspect emitted
    wire messages (Test 3 reads the CONFRONTATION frame off it).
    """
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
        genre_slug=snap.genre_slug,
        session_id="so-swn-e2e",
        round_number=round_number,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )
    return outcome, broadcasts


def _hp_depletion_sources(otel_capture) -> list[str]:
    """Return the ``source`` attribute of every ``encounter.resolved`` span."""
    sources: list[str] = []
    for span in otel_capture.get_finished_spans():
        if span.name == "encounter.resolved":
            sources.append((span.attributes or {}).get("source", ""))
    return sources


# ---------------------------------------------------------------------------
# Test 1 — personal Firefight resolves on HP depletion (the core proof)
# ---------------------------------------------------------------------------


def test_firefight_resolves_on_hp_depletion_vs_content_ac(otel_capture):
    snap, enc, pack = _seated_combat(
        encounter_type="combat",
        pc="Nova",
        opponent="Corsair",
        location="Docking Ring",
    )

    opponent_core = snap.find_creature_core("Corsair")
    assert opponent_core is not None, (
        "opponent core must be reachable via find_creature_core — without it the "
        "SWN attack has no AC and hp_depletion can never resolve"
    )
    # Task 9 seam: content hp/AC seeded onto the runtime core.
    assert opponent_core.armor_class == _COMBAT_AC
    assert opponent_core.hp.current == _COMBAT_HP

    # ── Assertion 1: a hit ablates HP, and the attack rolled vs the AUTHORED AC ──
    hp_before = opponent_core.hp.current
    outcome, _ = _throw(
        beat_id="shoot",
        pc="Nova",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="firefight-hit",
        round_number=1,
    )
    # The DiceRequest difficulty is the SWN attack target_number == opponent AC,
    # NOT a beat-derived DC. This is the load-bearing "rolls vs content AC" proof.
    assert outcome.request.difficulty == _COMBAT_AC, (
        f"attack must roll vs the authored opponent AC ({_COMBAT_AC}), not a beat DC; "
        f"got difficulty={outcome.request.difficulty}"
    )
    assert opponent_core.hp.current < hp_before, (
        f"a successful shoot strike must ablate opponent HP; "
        f"before={hp_before} after={opponent_core.hp.current}"
    )
    # ── Assertion (opposed_check NOT reached): combat is beat_selection now,
    # so the dispatcher must NOT defer to the narrator-driven opposed path. ──
    assert outcome.opposed_pending is False, (
        "swn combat is beat_selection (Task 8) — dispatch must resolve "
        "inline, never set opposed_pending (the opposed_check branch is not reached)"
    )

    # ── Assertion 2/3: drive HP to 0 → player_victory via the hp_depletion span ──
    # Seed the opponent low so the next hit drops it to <= 0 deterministically.
    opponent_core.hp.current = 1
    kill_outcome, _ = _throw(
        beat_id="shoot",
        pc="Nova",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="firefight-kill",
        round_number=2,
    )
    assert opponent_core.hp.current <= 0, "opponent HP must reach 0 after the killing hit"
    assert kill_outcome.encounter_resolved is True
    assert enc.outcome == "player_victory", (
        f"hp_depletion at 0 opponent HP must resolve player_victory; got {enc.outcome!r}"
    )

    # OTEL span-capture proof (NOT a source-text grep): the resolution came
    # through the hp_depletion path. This simultaneously proves the opposed_check
    # resolution path was not the one that ended the fight.
    sources = _hp_depletion_sources(otel_capture)
    assert "hp_depletion" in sources, (
        f"encounter.resolved span with source='hp_depletion' must fire on the "
        f"HP-to-0 kill (lie-detector for the SWN combat resolution path); "
        f"got resolved-span sources={sources}"
    )


# ---------------------------------------------------------------------------
# Test 1b — SWN P4 initiative rolled + persisted at the instantiation seam
# ---------------------------------------------------------------------------


def test_initiative_rolled_and_persisted_on_instantiation(otel_capture):
    """SWN P4: instantiating a combat hp_depletion confrontation rolls 1d8+DEX
    for player + opponent, persists the order, and fires the polygraph span."""
    _snap, enc, _pack = _seated_combat(
        encounter_type="combat",
        pc="Nova",
        opponent="Corsair",
        location="Docking Ring",
    )
    assert enc is not None
    names = {e.token_id for e in enc.initiative}
    assert "Nova" in names and "Corsair" in names, (
        f"both player + opponent must be seated in the initiative order; got {names}"
    )
    assert len(enc.initiative) >= 2  # player + opponent both seated and ordered
    init_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "encounter.initiative_rolled"
    ]
    assert init_spans, "encounter.initiative_rolled span must fire on instantiation"
    assert init_spans[0].attributes["encounter_type"]
    assert init_spans[0].attributes["initiative_order"]


# ---------------------------------------------------------------------------
# Test 2 — ship_combat resolves on hull depletion
# ---------------------------------------------------------------------------


def test_ship_combat_resolves_on_hull_depletion_vs_ship_ac(otel_capture):
    snap, enc, pack = _seated_combat(
        encounter_type="ship_combat",
        pc="Vance",
        opponent="Raider Frigate",
        location="Bridge",
    )

    hull_core = snap.find_creature_core("Raider Frigate")
    assert hull_core is not None
    assert hull_core.armor_class == _SHIP_AC
    assert hull_core.hp.current == _SHIP_HP

    # The enemy ship fires back each round. ship_combat authors no ship-tier
    # ``opponent_damage`` (unlike personal combat's 1d6), so the reprisal borrows
    # the player's strike beat — ``broadside`` is 2d6, which clears the player's
    # 10 HP roughly 1-in-6 (P(2d6>=10)). When it does, the player is downed and
    # the fight resolves ``opponent_victory`` on round 1, before the kill salvo —
    # the source of this test's flake. This test proves HULL depletion resolves
    # ``player_victory``, NOT player survivability, so harden the player against
    # the reprisal to keep the encounter active through both salvos. (The
    # reprisal-deals-real-damage behavior is exercised by Test 3.)
    pc_core = snap.find_creature_core("Vance")
    assert pc_core is not None, "player core must be reachable to harden against reprisal"
    pc_core.hp.current = pc_core.hp.max = 999

    # ── Hull ablates, attack rolls vs ship AC ──
    hull_before = hull_core.hp.current
    outcome, _ = _throw(
        beat_id="broadside",
        pc="Vance",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="ship-hit",
        round_number=1,
    )
    assert outcome.request.difficulty == _SHIP_AC, (
        f"ship attack must roll vs the authored ship AC ({_SHIP_AC}); "
        f"got difficulty={outcome.request.difficulty}"
    )
    assert hull_core.hp.current < hull_before, (
        f"a successful broadside must ablate the enemy ship's hull; "
        f"before={hull_before} after={hull_core.hp.current}"
    )
    assert outcome.opposed_pending is False

    # ── Drive hull to 0 → player_victory via the hp_depletion span ──
    hull_core.hp.current = 1
    kill_outcome, _ = _throw(
        beat_id="close_range",
        pc="Vance",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="ship-kill",
        round_number=2,
    )
    assert hull_core.hp.current <= 0, "hull must reach 0 after the killing salvo"
    assert kill_outcome.encounter_resolved is True
    assert enc.outcome == "player_victory"

    sources = _hp_depletion_sources(otel_capture)
    assert "hp_depletion" in sources, (
        f"encounter.resolved span with source='hp_depletion' must fire on 0 hull; "
        f"got resolved-span sources={sources}"
    )


# ---------------------------------------------------------------------------
# Test 3 — CONFRONTATION payload carries HP through the REAL dispatch path
# ---------------------------------------------------------------------------


def test_confrontation_payload_carries_hp_through_real_dispatch():
    """Production-path proof for ``core_resolver=snapshot.find_creature_core``.

    The mid-turn CONFRONTATION frame is built inside ``dispatch_dice_throw``
    (via ``build_confrontation_payload``) and broadcast on the room. We capture
    the broadcast and assert the emitted payload carries ``win_condition`` and
    HP dicts sourced from the real cores — i.e. the resolver is actually
    threaded in production, not only in the Task 6 unit test.
    """
    from sidequest.protocol.messages import ConfrontationMessage

    snap, enc, pack = _seated_combat(
        encounter_type="combat",
        pc="Nova",
        opponent="Corsair",
        location="Docking Ring",
    )

    _, broadcasts = _throw(
        beat_id="shoot",
        pc="Nova",
        enc=enc,
        pack=pack,
        snap=snap,
        request_id="payload-probe",
        round_number=1,
    )

    confrontations = [m for m in broadcasts if isinstance(m, ConfrontationMessage)]
    assert confrontations, (
        "dispatch must broadcast a mid-turn CONFRONTATION frame; "
        f"got {[type(m).__name__ for m in broadcasts]}"
    )
    payload = confrontations[-1].payload

    assert payload.win_condition == "hp_depletion", (
        f"CONFRONTATION payload must carry win_condition=hp_depletion; "
        f"got {payload.win_condition!r}"
    )
    # player_hp / opponent_hp are {"current","max"} dicts sourced from the real
    # cores. The opponent was seeded to the content HP (12) and just took a hit,
    # so its current is below max; the player core was registered at 10/10.
    assert payload.player_hp is not None, "player_hp must be surfaced under hp_depletion"
    assert payload.opponent_hp is not None, "opponent_hp must be surfaced under hp_depletion"
    assert set(payload.player_hp) == {"current", "max"}
    assert set(payload.opponent_hp) == {"current", "max"}
    assert payload.opponent_hp["max"] == _COMBAT_HP, (
        "opponent_hp.max must come from the content-seeded opponent core"
    )
    assert payload.opponent_hp["current"] < payload.opponent_hp["max"], (
        "opponent_hp.current must reflect the damage just applied (resolver is live)"
    )
    # player_hp is sourced from the live player core via the resolver. Its max
    # comes from the registered core (10); its current may be BELOW max because
    # the opponent's reprisal can ablate the player this turn (playtest 67-10 —
    # the enemy now actually deals damage via cdef.opponent_damage). Pinning this
    # to 10/10 would re-encode the old "player never takes damage" bug.
    assert payload.player_hp["max"] == 10, (
        "player_hp.max must come from the registered player core via the resolver"
    )
    assert 0 <= payload.player_hp["current"] <= 10, (
        "player_hp.current must be sourced from the live player core (0..max)"
    )


# ---------------------------------------------------------------------------
# Test 4 — both authored worlds load clean under swn
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("world_slug", ["aureate_span", "coyote_star"])
def test_world_loads_clean_under_swn(world_slug):
    """The pack (with its world overlay) loads clean under ruleset: swn, and the
    combat / ship_combat hp_depletion confrontations validate with opponent stats.

    Note on ``aureate_span``: it is authored ``draft: true`` and is therefore
    silently skipped at aggregate ``load_genre_pack`` time BY DESIGN (the loader
    excludes draft worlds from ``pack.worlds``) — that's content-team state, not
    a load failure. So for the draft world we assert its overlay parses cleanly
    through the real ``_load_single_world`` path (no ValidationError, returns
    None purely because of the draft gate); for the wired world we assert it is
    present in ``pack.worlds``. The pack-level swn rules are common to both.
    """
    from pathlib import Path

    from sidequest.genre.loader import _load_single_world, load_genre_pack
    from sidequest.genre.models.rules import WinCondition

    pack_root = find_pack_path("space_opera")

    # Pack loads clean under swn (no ValidationError) — common to both worlds.
    pack = load_genre_pack(pack_root)
    assert pack.rules.ruleset == "swn", "space_opera must be bound ruleset: swn"

    # The combat / ship_combat hp_depletion confrontations validate and carry
    # opponent stats (the SWN-bound combat surface both worlds share).
    combat = next(c for c in pack.rules.confrontations if c.confrontation_type == "combat")
    ship = next(c for c in pack.rules.confrontations if c.confrontation_type == "ship_combat")
    for cdef, exp_hp, exp_ac in ((combat, _COMBAT_HP, _COMBAT_AC), (ship, _SHIP_HP, _SHIP_AC)):
        assert cdef.win_condition == WinCondition.hp_depletion
        assert cdef.opponent_hp == exp_hp, f"{cdef.confrontation_type} opponent hp"
        assert cdef.opponent_armor_class == exp_ac, f"{cdef.confrontation_type} opponent AC"

    # World overlay loads clean.
    world_dir = Path(pack_root) / "worlds" / world_slug
    assert world_dir.is_dir(), f"world dir {world_slug} must exist on disk"
    loaded = _load_single_world(world_dir, pack.tropes, Path(pack_root))

    if world_slug in pack.worlds:
        # Wired (non-draft) world: present in the aggregate and re-loads clean.
        assert loaded is not None, (
            f"{world_slug} is in pack.worlds but _load_single_world returned None"
        )
    else:
        # Draft world: excluded from pack.worlds BY DESIGN. _load_single_world
        # parses world.yaml without error and returns None purely on the draft
        # gate — proving the overlay is well-formed under swn, not broken.
        from sidequest.genre.loader import _load_yaml
        from sidequest.genre.models.world import WorldConfig

        cfg = _load_yaml(world_dir / "world.yaml", WorldConfig)
        assert cfg.draft is True, (
            f"{world_slug} is absent from pack.worlds but world.yaml is not draft — "
            "that would be a real load failure, not a by-design skip"
        )
        assert loaded is None, "draft world must draft-skip cleanly (no validation crash)"
