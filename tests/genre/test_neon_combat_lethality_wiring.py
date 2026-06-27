"""CWN Combat Lethality — END-TO-END wiring proof (plan 2026-05-28, Task 15).

This is the wiring test that proves the WHOLE chain with the REAL on-disk
``neon_dystopia`` pack — no synthetic fixture pack:

    real pack.yaml/rules.yaml/inventory.yaml (content Tasks 13-14)
      -> ``ruleset: cwn`` binding (CwnRulesetModule)
        -> production ``dispatch_dice_throw``
          -> ablative HP + ``cwn.*`` OTEL (Trauma roll, Mortal Injury)

Per CLAUDE.md "No Source-Text Wiring Tests": this asserts CONFIG truth (loaded
pydantic models) and BEHAVIOR (OTEL spans + engine state after driving a real
strike), never source-greps.

Two kinds of assertion, deliberately split:

* ``test_neon_combat_lethality_config_wiring`` — needs NO database and NO
  dispatch. Loads the real pack and asserts the cwn config + content wiring is
  present (trauma target, hp_depletion, momentum-combat retired, net_run
  dials intact). MUST run (not skip) when content is on disk.
* The two dispatch-drive tests load the real pack, seat its REAL ``combat``
  confrontation, equip a REAL catalog weapon that carries a ``trauma_die``, and
  drive ``dispatch_dice_throw`` — asserting ``cwn.trauma.roll`` and
  ``cwn.mortal_injury.declared`` fire. ``dispatch_dice_throw`` on this path
  takes synthetic snapshot/encounter and touches NO database (the Task-9
  dispatch test runs the same call with no DB gate), so these run too.

Content-on-disk guard (mirrors ``tests/genre/test_neon_loads_cwn.py``): skip
ONLY when sidequest-content is genuinely absent. If ``load_pack`` RAISES —
i.e. the real pack fails to load/validate — that is a REAL content defect from
Tasks 13-14 and the exception is surfaced, NOT swallowed by a skip.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import WinCondition
from tests._helpers.genre_paths import GENRE_PACKS_DIR
from tests.genre.test_resolution_mode import load_pack


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


_HAS_CONTENT = _has_real_content()

# Attacker stat block (canonical Without Number codes). The combat beats'
# stat_check is "DEX"; 14 is comfortably enough that a nat-20 d20 beats the
# street-mook AC and the strike-damage path fires.
_STATS = {"STR": 14, "DEX": 14, "CON": 14, "INT": 12, "WIS": 12, "CHA": 12}


# ---------------------------------------------------------------------------
# otel_capture: tests/genre has no conftest fixture for this, so mirror the
# server conftest one (in-memory exporter on the global TracerProvider).
# ---------------------------------------------------------------------------
@pytest.fixture
def otel_capture() -> Iterator:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # Drop accumulated processors from prior invocations (see server conftest).
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Seating helpers — REAL pack, synthetic snapshot/encounter (the Task-9 shape).
# ---------------------------------------------------------------------------
def _make_snapshot_and_encounter(
    attacker: str,
    opponent: str,
    *,
    opponent_hp: int,
    weapon_id: str,
):
    """Snapshot + StructuredEncounter seated like a real neon ``combat``.

    The attacker is a player Character whose CreatureCore inventory carries one
    item dict ``{"id": <weapon_id>}`` — the catalog (Priority-3) lookup in
    ``resolve_damage_spec_from_beat_and_actor`` resolves the weapon's DamageSpec
    (with ``trauma_die``) from the REAL ``pack.inventory.item_catalog``. No
    ``damage_override`` on the beat: this proves the pack catalog wiring, not a
    synthetic spec.

    The opponent is an Npc seeded with ``opponent_hp`` and the street-mook AC so
    a nat-20 strike lands and ablates real HP.
    """
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

    atk_core = CreatureCore(
        name=attacker,
        description="Street runner.",
        personality="reckless",
        inventory=Inventory(items=[{"id": weapon_id, "name": weapon_id}]),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Solo",
        race="Street",
        backstory="Ran with the franchise gangs.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Corp security.",
        personality="cold",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=13,
    )

    snap = GameSnapshot(
        genre_slug="neon_dystopia",
        world_slug="franchise_nations",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=opp_core))

    # hp_depletion combats carry no momentum dials; the encounter metrics here
    # are inert placeholders (dispatch reads them but the win condition is HP).
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=attacker, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap, enc


def _drive_strike(*, snap, enc, pack, attacker: str, beat_id: str, request_id: str):
    """Drive one strike beat through the REAL ``dispatch_dice_throw``.

    face=[20] guarantees the d20 beats the opponent's AC so the strike-damage
    block (and the Trauma + downed seams inside it) fire. The bound ruleset is
    resolved by dispatch from ``pack.rules.ruleset`` ("cwn") — no module is
    injected, proving pack -> registry -> module reachability.
    """
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    return dispatch_dice_throw(
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
        rolling_player_id=f"player-{attacker.lower()}",
        character_name=attacker,
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="neon_dystopia",
        session_id="neon-cwn-wiring",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )


# ---------------------------------------------------------------------------
# 1+2: Config / content wiring — NO database, NO dispatch. MUST run on disk.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_combat_lethality_config_wiring() -> None:
    """The real neon pack carries the CWN combat-lethality config + content.

    If ``load_pack`` raises (a real validation failure from content Tasks
    13-14), the exception surfaces here — it is NOT masked by the content guard.
    """
    pack: GenrePack = load_pack("neon_dystopia")

    # --- Step 1: cwn config + hp_depletion combat + a strike beat -----------
    assert pack.rules.ruleset == "cwn"
    assert pack.rules.cwn is not None
    assert pack.rules.cwn.trauma.default_trauma_target == 6

    combat = next(c for c in pack.rules.confrontations if c.confrontation_type == "combat")
    assert combat.win_condition == WinCondition.hp_depletion

    strike_beats = [
        b
        for b in combat.beats
        if str(getattr(b.damage_channel, "value", b.damage_channel)) == "strike"
    ]
    assert strike_beats, (
        "the real neon combat confrontation must declare at least one strike "
        f"beat (damage_channel=strike); beats={[b.id for b in combat.beats]}"
    )

    # --- Step 2: momentum combat retired; net_run dials intact --------------
    # The hp_depletion combat must NOT carry momentum dials — proves the old
    # dual-track-momentum combat was retired in favour of ablative HP. The
    # hacking confrontation (net_run, which superseded net_combat in the CWN
    # net-run plan) stays dial-based, proving only personal combat moved to HP.
    assert combat.player_metric is None, (
        "the hp_depletion combat confrontation must have NO player_metric "
        "(the momentum-dial combat was retired); "
        f"got {combat.player_metric!r}"
    )
    assert combat.opponent_metric is None, (
        "the hp_depletion combat confrontation must have NO opponent_metric; "
        f"got {combat.opponent_metric!r}"
    )

    net = next(c for c in pack.rules.confrontations if c.confrontation_type == "net_run")
    assert net.player_metric is not None and net.opponent_metric is not None, (
        "net_run must retain its dual-track dials (only personal combat was "
        "converted to hp_depletion)"
    )
    assert net.player_metric.name == "data"
    assert net.opponent_metric.name == "alert"


# ---------------------------------------------------------------------------
# 3: Dispatch-drive — Trauma roll fires through the real pack chain.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_strike_fires_cwn_trauma_roll(otel_capture, monkeypatch) -> None:
    """Driving a real neon strike with a trauma-die weapon fires cwn.trauma.roll.

    The attacker equips ``smart_pistol`` (real catalog item: 1d6 + trauma_die
    1d8). The weapon's DamageSpec resolves through the REAL
    ``pack.inventory.item_catalog`` (Priority-3 lookup), so the Trauma seam has
    a die to roll. The trauma die value is forced deterministic (low) — the
    span fires for ANY roll when a trauma_die is present.
    """
    # dice.random.randint is the rng the seam threads into resolve_trauma; the
    # damage faces roll via damage_roll.random (a different module), untouched.
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = load_pack("neon_dystopia")
    snap, enc = _make_snapshot_and_encounter(
        "Razor", "Mr. Vex", opponent_hp=20, weapon_id="smart_pistol"
    )

    _drive_strike(
        snap=snap,
        enc=enc,
        pack=pack,
        attacker="Razor",
        beat_id="shoot",
        request_id="neon-trauma",
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.trauma.roll" in span_names, (
        "cwn.trauma.roll must fire when a real neon strike with a trauma_die "
        "weapon resolves damage through dispatch_dice_throw — proves "
        "pack(catalog) -> cwn module -> dispatch reachability; "
        f"got spans: {span_names}"
    )


# ---------------------------------------------------------------------------
# 4: Dispatch-drive — a 0-HP drop declares a Mortal Injury through the chain.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_zero_hp_drop_declares_mortal_injury(otel_capture, monkeypatch) -> None:
    """A real neon strike that drops a target to 0 HP declares a Mortal Injury.

    Opponent seeded at hp=1 so the smart_pistol's 1d6 strike (nat-20 hits)
    drops it to 0, firing the downed seam. The trauma die is forced LOW so the
    hit is NOT traumatic (no Major Injury branch) — the Mortal Injury must still
    fire (it is unconditional at 0 HP).
    """
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = load_pack("neon_dystopia")
    snap, enc = _make_snapshot_and_encounter(
        "Razor", "Mr. Vex", opponent_hp=1, weapon_id="smart_pistol"
    )
    target_core = snap.find_creature_core("Mr. Vex")
    assert target_core is not None

    _drive_strike(
        snap=snap,
        enc=enc,
        pack=pack,
        attacker="Razor",
        beat_id="shoot",
        request_id="neon-mortal",
    )

    assert target_core.hp.current == 0, (
        "precondition: the strike must drop the target to 0 HP through the real "
        f"pack chain; hp={target_core.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.mortal_injury.declared" in span_names, (
        f"a real neon target dropped to 0 HP must declare a Mortal Injury; got spans: {span_names}"
    )
    assert any("Mortal Injury" in s.text for s in target_core.statuses), (
        "the downed target must carry a Mortal Injury Status on its core; "
        f"statuses={[s.text for s in target_core.statuses]}"
    )
