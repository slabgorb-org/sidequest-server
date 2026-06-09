"""ADR-114 Task 10 — SWN combat e2e acceptance proof.

Proves that ONE SWN combat turn delivers ALL THREE of the ADR-114
success criteria in a single strike-beat resolution:

  (HP layer)   target CreatureCore.hp.current decreased by damage_total - mitigation
  (OTEL)       a state_patch.hp span fired
  (dial layer) the encounter's momentum dial also advanced (narrative pacing layer
               runs alongside HP — both legible in one turn)

Beat chosen: ``shoot`` (kind=strike, damage_channel=strike) on a synthetic
Firefight-shaped ConfrontationDef.
Weapon: blaster_sidearm (damage: 1d6+0) from the swn_test_pack fixture's
world-tier catalog.
Stat: Physique (the shoot beat's stat_check).

**Fixture note (space_opera→SWN binding, Task 8):**
space_opera's ``combat`` (Firefight) confrontation moved off
``resolution_mode: opposed_check`` to SWN ``beat_selection`` +
``win_condition: hp_depletion`` (HP-to-0 combat, no dual-dial metrics). It can
therefore no longer supply an opposed_check OR a dual-dial confrontation from
the real pack. Both tests in this module consequently mint their confrontation
shape SYNTHETICALLY via ``_make_synthetic_firefight_pack``:

  - Task 10 (simple-DC, ``dispatch_dice_throw``): synthetic cdef with the
    default ``beat_selection`` mode so the damage roll fires inline.
  - Task 11 (opposed-check, ``narration_apply``): synthetic cdef with
    ``resolution_mode=opposed_check`` so ``_apply_narration_result_to_snapshot``
    routes through ``_resolve_opposed_check_branch`` — the engine-level branch
    still used by any pack that DOES declare opposed_check.

Story 96-1: the ``blaster_sidearm`` weapon + its damage spec (1d6+0) are
pulled from the ``swn_test_pack`` FIXTURE's world-tier catalog
(``worlds/test_world/inventory.yaml``) via the production
``resolve_inventory`` seam — the same world-REPLACE path live migrated
packs use — so the weapon-lookup path is still exercised without coupling
to live content. The synthetic shoot beat carries the same
``stat_check: Physique`` and ``damage_channel: strike`` as the
historically-authored shoot beat.

The ``otel_capture`` fixture is imported from
``tests/integration/conftest.py`` which re-exports it from
``tests/server/conftest.py``.
"""

from __future__ import annotations

from typing import Any

from tests._helpers.fixture_packs import SWN_TEST_PACK, TEST_WORLD, load_fixture_pack

# ---------------------------------------------------------------------------
# Pack guard
# ---------------------------------------------------------------------------


def _load_fixture_swn_pack():
    return load_fixture_pack(SWN_TEST_PACK)


# ---------------------------------------------------------------------------
# Fixture helpers (inline — no cross-test coupling)
# ---------------------------------------------------------------------------


def _make_creature_core_with_hp(name: str, *, hp: int = 12):
    """CreatureCore with an HpPool seeded to a known value."""
    from sidequest.game.creature_core import CreatureCore, Inventory

    return CreatureCore(
        name=name,
        description=f"{name} description",
        personality="aggressive",
        inventory=Inventory(),
        hp={"current": hp, "max": hp, "base_max": hp},
    )


def _make_snapshot_with_blaster_attacker(attacker_name: str, target_name: str):
    """GameSnapshot with:
    - attacker Character whose inventory.items carries a blaster_sidearm dict
    - target NPC with known HP, unarmored (no mitigation item)

    The inventory uses list[dict] (not InventorySlot objects).
    _resolve_damage_spec_from_beat_and_actor priority-3 path looks up the
    item id in the pack's item_catalog; blaster_sidearm now carries
    damage: {dice: "1d6", bonus: 0} so the DamageSpec resolves cleanly.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    # --- Attacker ---
    # Seed the inventory with a blaster_sidearm dict so the catalog lookup fires.
    atk_inventory = Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}])
    atk_core = CreatureCore(
        name=attacker_name,
        description="Soldier on station duty",
        personality="disciplined",
        inventory=atk_inventory,
        hp={"current": 10, "max": 10, "base_max": 10},
    )

    attacker_char = Character(
        core=atk_core,
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
    )

    # --- Target ---
    # Unarmored — no mitigation item in inventory. HP damage is raw dice total.
    target_core = _make_creature_core_with_hp(target_name, hp=15)

    # --- Snapshot ---
    snap = GameSnapshot(
        genre_slug=SWN_TEST_PACK,
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=target_core))

    return snap


def _make_firefight_encounter(attacker_name: str, target_name: str):
    """StructuredEncounter matching the space_opera combat/Firefight shape.

    Metric: momentum, threshold=7 (per rules.yaml).
    Actors: attacker on player side, target on opponent side.
    """
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=attacker_name, role="combatant", side="player"),
            EncounterActor(name=target_name, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _make_synthetic_firefight_pack(real_pack, *, opposed_check: bool = False):
    """Build a synthetic GenrePack-shaped object that mirrors the space_opera
    Firefight ConfrontationDef. The resolution_mode is caller-chosen:

    - ``opposed_check=False`` (default): resolution_mode defaults to
      ``beat_selection`` so ``dispatch_dice_throw`` applies the beat inline
      (no deferral). Used by the Task 10 simple-DC acceptance test.
    - ``opposed_check=True``: resolution_mode is ``opposed_check`` so
      ``_apply_narration_result_to_snapshot`` routes through
      ``_resolve_opposed_check_branch`` — the branch under test for Task 11.

    The real pack's inventory catalog is preserved so the blaster_sidearm
    weapon lookup (damage: 1d6+0) exercises the Part A canary content. The
    weapon damage spec genuinely lives in the real pack's item_catalog, so we
    reuse only ``real_pack.inventory`` for that lookup — everything else
    (confrontation shape, beat, metrics) is synthetic.

    Rationale for synthetic fixture (space_opera→SWN binding, Task 8):
    The authored space_opera ``combat`` confrontation moved off
    ``resolution_mode: opposed_check`` to ``beat_selection`` +
    ``win_condition: hp_depletion`` (HP-to-0 SWN combat, no dual-dial
    metrics). The real pack can therefore no longer supply an opposed_check
    confrontation. Both tests now mint the confrontation shape they need
    synthetically rather than coupling to the real pack's (now-changed)
    combat resolution_mode. All beat attributes (stat_check=Physique,
    damage_channel=strike, base=2, momentum dial/threshold=7) mirror the
    historically-authored shoot beat.
    """
    from unittest.mock import MagicMock

    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
    )

    # Player beat: the strike "shoot" beat (damage_channel=strike).
    shoot_beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Shoot",
            "kind": "strike",
            "base": 2,
            "stat_check": "Physique",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Blaster bolts sear the corridor. Sparks off bulkheads.",
        }
    )
    # Opponent beat: a non-strike beat so the opposed test can give the
    # opponent a beat that does NOT deal HP damage, isolating the player path.
    cover_beat = BeatDef.model_validate(
        {
            "id": "take_cover",
            "label": "Take Cover",
            "kind": "brace",
            "base": 2,
            "stat_check": "Physique",
            "effect": "Brace against incoming fire",
            "narrator_hint": "Duck behind the bulkhead.",
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        resolution_mode=(
            ResolutionMode.opposed_check if opposed_check else ResolutionMode.beat_selection
        ),
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[shoot_beat, cover_beat],
    )

    pack = MagicMock()
    pack.rules = RulesConfig(confrontations=[cdef])
    # Mirror the fixture pack's two inventory tiers so the production
    # resolve_inventory(pack, snapshot.world_slug) seam resolves the
    # world-tier blaster_sidearm catalog exactly as it would in live play.
    pack.inventory = real_pack.inventory
    pack.worlds = real_pack.worlds
    return pack


# ---------------------------------------------------------------------------
# The three-criterion acceptance test
# ---------------------------------------------------------------------------


def test_space_opera_shoot_beat_deals_hp_damage_while_dials_advance(otel_capture):
    """ADR-114 Task 10 — three-part acceptance proof.

    Drives the swn_test_pack fixture through dispatch_dice_throw with the
    ``shoot`` beat (damage_channel=strike, stat_check=Physique) and a
    face value that guarantees SUCCESS so the damage roll fires.

    Asserts:
    (HP layer)   target hp.current < hp_before
    (OTEL)       state_patch.hp span present in otel_capture
    (dial layer) encounter.player_metric.current > 0 (momentum advanced)

    Uses the swn_test_pack FIXTURE: the Part A canary weapon
    (blaster_sidearm, 1d6+0, world-tier catalog) and the shoot beat's
    damage_channel=strike annotation are both exercised.
    """
    real_pack = _load_fixture_swn_pack()

    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.inventory_resolve import resolve_inventory
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    attacker_name = "Nova"
    target_name = "Corsair"

    # Part A canary checks against the FIXTURE pack:
    # 1. shoot beat exists and carries damage_channel=strike.
    real_combat_cdef = next(
        (c for c in real_pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert real_combat_cdef is not None, "swn_test_pack must have a 'combat' ConfrontationDef"
    real_shoot_beat = next((b for b in real_combat_cdef.beats if b.id == "shoot"), None)
    assert real_shoot_beat is not None, (
        "swn_test_pack combat confrontation must have a 'shoot' beat"
    )
    assert str(real_shoot_beat.damage_channel) == "strike", (
        "shoot beat must carry damage_channel=strike (Part A content canary)"
    )

    # 2. blaster_sidearm has a damage spec in the WORLD-TIER catalog, reached
    #    through the production resolve_inventory seam (epic 94 shape).
    resolved_inventory = resolve_inventory(real_pack, TEST_WORLD)
    sidearm_item = next(
        (
            i
            for i in (resolved_inventory.item_catalog if resolved_inventory else [])
            if i.id == "blaster_sidearm"
        ),
        None,
    )
    assert sidearm_item is not None, "blaster_sidearm must be in the world-tier item_catalog"
    assert sidearm_item.damage is not None, (
        "blaster_sidearm must carry a damage spec (Part A content canary)"
    )

    # Synthetic pack: mirrors Firefight shape but with beat_selection mode so
    # dispatch_dice_throw applies the beat inline (real pack uses opposed_check
    # which defers damage to the narrator phase — see module docstring).
    # Real pack's inventory catalog is preserved for blaster_sidearm lookup.
    pack = _make_synthetic_firefight_pack(real_pack)

    enc = _make_firefight_encounter(attacker_name, target_name)
    snap = _make_snapshot_with_blaster_attacker(attacker_name, target_name)

    # Record target HP before.
    target_core = snap.find_creature_core(target_name)
    assert target_core is not None, "target must be discoverable in snapshot"
    hp_before = target_core.hp.current

    # face=18, Physique=10 (+0 mod).
    # shoot beat base=2 → DC = 10 + |2|*2 = 14.
    # total = 18 + 0 = 18 > 14 → Success tier → damage fires.
    broadcasts: list[object] = []
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="so-e2e-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[18],
            beat_id="shoot",
        ),
        rolling_player_id="player-nova",
        character_name=attacker_name,
        character_stats={"Physique": 10},
        encounter=enc,
        pack=pack,
        genre_slug=SWN_TEST_PACK,
        session_id="so-e2e-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )

    # ── Criterion 1: HP layer ──────────────────────────────────────────────
    hp_after = target_core.hp.current
    assert hp_after < hp_before, (
        f"(HP layer) target HP must decrease after a successful shoot beat; "
        f"before={hp_before} after={hp_after}"
    )

    # ── Criterion 2: OTEL span ────────────────────────────────────────────
    finished_span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished_span_names, (
        f"(OTEL) state_patch.hp span must fire on strike beat; got spans: {finished_span_names}"
    )

    # ── Criterion 3: dial layer ───────────────────────────────────────────
    assert enc.player_metric.current > 0, (
        f"(dial layer) momentum dial must advance after successful shoot beat "
        f"(proving narration pacing layer runs alongside HP); "
        f"got player_metric.current={enc.player_metric.current}"
    )


# ---------------------------------------------------------------------------
# ADR-114 Task 11 — opposed_check path: HP damage fires via narration_apply
# ---------------------------------------------------------------------------


def test_opposed_check_shoot_beat_deals_hp_damage_in_narration_apply(otel_capture, monkeypatch):
    """ADR-114 Task 11 regression guard.

    Proves that strike-beat HP damage fires on the OPPOSED-CHECK path
    (``narration_apply._resolve_opposed_check_branch``) — NOT on the simple-DC
    path that Task 10 already covers.

    **Fixture strategy (documented):**
    This test drives ``_apply_narration_result_to_snapshot`` directly with a
    stashed player d20 + committed ``shoot`` strike beat, rather than spinning
    up a full WebSocket round-trip. Rationale: the opposed-check handshake
    requires two round-trips (DICE_THROW → narrator turn) and testing the full
    stack in a unit test would require mocking the Anthropic SDK; driving
    narration_apply directly is both faster and more focused on the wiring gap.

    **Fixture strategy (space_opera→SWN binding, Task 8):**
    The opposed-check resolution branch is engine-level and shared by every
    pack whose confrontations declare ``resolution_mode: opposed_check`` — the
    branch is selected by the cdef's resolution_mode, not by any space_opera
    specifics. space_opera's OWN ``combat`` confrontation moved off opposed_check
    to SWN ``beat_selection`` + ``win_condition: hp_depletion`` (Task 8), so it
    can no longer supply an opposed_check confrontation. This test therefore
    drives a SYNTHETIC opposed_check pack (``_make_synthetic_firefight_pack(...,
    opposed_check=True)``) — mirroring how the Task 10 sibling already uses a
    synthetic pack — instead of coupling to the real pack's (now-changed)
    combat resolution_mode. The real pack's inventory catalog is still reused so
    the ``blaster_sidearm`` weapon (1d6+0) damage lookup exercises real content.

    The opponent's synthetic beat (``take_cover``, kind=brace, no
    damage_channel) deals no HP damage, isolating the player-side path. The
    opponent's server-side d20 is monkeypatched to 3 (Fail tier) for the same
    isolation.

    Asserts (the three ADR-114 criteria on the opposed path):
    1. Target CreatureCore.hp.current decreased.
    2. A state_patch.hp span fired.
    3. The encounter's momentum dial advanced (player_metric.current > 0).
    """
    real_pack = _load_fixture_swn_pack()

    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import RollOutcome
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    # Synthetic opposed_check pack: mirrors the Firefight shape with
    # resolution_mode=opposed_check so _apply_narration_result_to_snapshot
    # routes through _resolve_opposed_check_branch (the branch under test).
    # blaster_sidearm catalog lookup runs against the fixture world-tier inventory.
    pack = _make_synthetic_firefight_pack(real_pack, opposed_check=True)
    combat_cdef = pack.rules.confrontations[0]

    # ── Snapshot: attacker with blaster_sidearm, target with known HP ─────
    attacker_name = "Vex"
    target_name = "Gunner"
    hp_target = 15

    atk_inventory = Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}])
    atk_core = CreatureCore(
        name=attacker_name,
        description="Freighter pilot",
        personality="cautious",
        inventory=atk_inventory,
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
    )
    target_core = CreatureCore(
        name=target_name,
        description="Corsair guard",
        personality="brutal",
        inventory=Inventory(),
        hp={"current": hp_target, "max": hp_target, "base_max": hp_target},
    )
    snap = GameSnapshot(
        genre_slug=SWN_TEST_PACK,
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=target_core))

    # ── Encounter: Firefight (opposed_check) ──────────────────────────────
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(
                name=attacker_name,
                role="combatant",
                side="player",
                per_actor_state={"stats": {"Physique": 12}},
            ),
            EncounterActor(
                name=target_name,
                role="combatant",
                side="opponent",
                per_actor_state={"stats": {"Physique": 10}},
            ),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    snap.encounter = enc

    # Monkeypatch the opponent's server-side d20 to 3 (Fail tier) so only
    # the player-side shoot beat deals damage; we isolate the player path.
    monkeypatch.setattr(
        "sidequest.server.narration_apply._roll_d20_server_side",
        lambda: 3,
    )

    # Capture broadcasts so we can assert the DICE_REQUEST+DICE_RESULT pair
    # was emitted from _resolve_opposed_check_branch.
    broadcasts: list[object] = []

    # Narrator emits the opponent's beat selection. The player's beat (shoot)
    # is stashed via pending_player_d20 / pending_player_beat_id.
    # Opponent emits a non-strike beat (take_cover/brace) so only the player
    # deals HP damage.
    opponent_beat_id = _pick_non_strike_beat_id(combat_cdef)
    result = NarrationTurnResult(
        narration="Blasters light up the corridor.",
        beat_selections=[
            BeatSelection(
                actor=target_name,
                beat_id=opponent_beat_id,
                outcome=RollOutcome.Fail,
            ),
        ],
    )

    # Record HP before applying.
    hp_before = target_core.hp.current

    # Room object. We override room.broadcast with a simple captures list so
    # the test can inspect which DiceResultMessages were emitted by the
    # opposed-check damage path without needing registered WebSocket queues.
    # Production uses room.broadcast to fan to connected sockets; here we just
    # capture to a list to prove the broadcast fires.
    from tests._helpers.session_room import room_for

    room = room_for(snap)
    room.broadcast = broadcasts.append  # type: ignore[method-assign]

    # Player d20=18, Physique mod=+1 → total=19 vs DC=14 (base=2 → 14) → Success.
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name=attacker_name,
        pack=pack,
        opposed_player_d20=18,
        opposed_player_beat_id="shoot",
        opposed_player_actor=attacker_name,
        from_explicit_action=True,
        room=room,
        acting_character_name=attacker_name,
    )

    # ── Criterion 1: HP layer ─────────────────────────────────────────────
    hp_after = target_core.hp.current
    assert hp_after < hp_before, (
        f"(HP layer / opposed path) target HP must decrease after opposed shoot beat; "
        f"before={hp_before} after={hp_after}"
    )

    # ── Criterion 2: OTEL span ────────────────────────────────────────────
    finished_span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished_span_names, (
        f"(OTEL / opposed path) state_patch.hp span must fire; got spans: {finished_span_names}"
    )

    # ── Criterion 3: dial layer ───────────────────────────────────────────
    assert enc.player_metric.current > 0, (
        f"(dial layer / opposed path) momentum dial must advance after shoot beat; "
        f"got player_metric.current={enc.player_metric.current}"
    )

    # ── Criterion 4 (bonus): damage DICE_RESULT was broadcast ─────────────
    from sidequest.protocol.messages import DiceResultMessage

    dice_results = [m for m in broadcasts if isinstance(m, DiceResultMessage)]
    assert len(dice_results) >= 1, (
        f"(broadcast / opposed path) at least one DICE_RESULT must be broadcast "
        f"for the damage roll; got {len(dice_results)} results from "
        f"{[type(m).__name__ for m in broadcasts]}"
    )


def _pick_non_strike_beat_id(cdef: Any) -> str:
    """Pick the first beat with kind != strike from cdef, or the first beat.

    Used to give the opponent a non-damaging beat so the test cleanly
    isolates the player-side damage path.
    """
    from sidequest.game.beat_kinds import BeatKind

    for beat in cdef.beats:
        if beat.kind != BeatKind.strike:
            return beat.id
    # Fallback: first beat (even if strike — damage is gated on damage_channel,
    # not just kind, so it's safe if the beat lacks damage_channel=strike).
    return cdef.beats[0].id
