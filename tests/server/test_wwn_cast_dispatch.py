"""WWN Content Plan 3 Task 7 — cast_spell routes to WwnRulesetModule.resolve_spellcast.

Proves the WWN cast spine is wired into ``_apply_narration_result_to_snapshot``'s
cast_spell branch (ruleset-gated) by driving ``_resolve_wwn_cast_for_beat`` against
a synthetic wwn-bound pack + snapshot and asserting on OTEL spans + HP state — NOT
source text (CLAUDE.md forbids source-grep wiring tests).

Path-trap note: these synthetic tests prove the dispatch logic; Task 14 proves the
spine on the REAL wwn pack end-to-end. This is necessary-but-not-sufficient.

Determinism: ``_resolve_wwn_cast_for_beat`` passes the ``random`` module imported
into ``sidequest.server.narration_apply`` as the rng to ``resolve_spellcast`` (save
roll + damage roll) and to the downed seam (Physical save + Major Injury roll). We
monkeypatch ``sidequest.server.narration_apply.random.randint`` to force those rolls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# WWN attribute_map: the six SWN/WWN attributes -> this fixture pack's flavor stats.
_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "CONSTITUTION": "Vigor",
    "DEXTERITY": "Grace",
    "INTELLIGENCE": "Lore",
    "WISDOM": "Wit",
    "CHARISMA": "Bearing",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 10 for name in _ABILITY_SCORE_NAMES}

_OPPONENT_AC = 12


def _make_wwn_pack(*, with_catalog: bool = True):
    """A MagicMock pack whose ``.rules`` is a real wwn-bound RulesConfig.

    The ``combat`` ConfrontationDef carries one ``cast_spell`` beat and
    ``opponent_default_stats`` so the downed seam can resolve a Physical save.
    ``wwn_spell_catalog`` carries a single damage spell (``firebolt``) with a
    mental save (save-for-half).
    """
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        SystemStrainConfig,
        WwnConfig,
    )
    from sidequest.genre.models.wwn_spell import WwnSpell, WwnSpellCatalog

    cast_beat = BeatDef.model_validate(
        {
            "id": "cast_spell",
            "label": "Cast a Spell",
            "kind": "strike",
            "base": 2,
            "stat_check": "Lore",
            "effect": "Unleash arcane force.",
            "narrator_hint": "Runes flare.",
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Skirmish",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[cast_beat],
        opponent_default_stats={name: 10 for name in _ABILITY_SCORE_NAMES},
    )

    wwn_cfg = WwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        wwn=wwn_cfg,
    )
    if with_catalog:
        pack.wwn_spell_catalog = WwnSpellCatalog(
            spells=[
                WwnSpell(
                    id="firebolt",
                    name="Firebolt",
                    level=1,
                    save="mental",
                    damage_die="1d6",
                    damage_per_level=True,
                    genre_description="A lance of flame.",
                    mechanical_effect="caster_level d6, mental save halves",
                ),
            ]
        )
    else:
        pack.wwn_spell_catalog = None
    return pack


def _make_snapshot_and_encounter(caster: str, opponent: str, *, opponent_hp: int = 20):
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
    from sidequest.game.wwn_magic import SpellcastingState

    caster_core = CreatureCore(
        name=caster,
        description="A hedge-mage.",
        personality="curious",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        spellcasting=SpellcastingState(
            prepared=["firebolt"],
            casts_remaining=2,
            casts_per_day=2,
            max_spell_level=1,
        ),
    )
    caster_char = Character(
        core=caster_core,
        char_class="Mage",
        race="Human",
        backstory="Studied the old runes.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="A brigand.",
        personality="cruel",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="wwn_test",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(caster_char)
    snap.npcs.append(Npc(core=opp_core))

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=caster, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap, enc


def _cast(*, snap, enc, pack, caster: str, spell_id: str | None):
    """Drive one cast_spell beat through ``_resolve_wwn_cast_for_beat``."""
    from sidequest.agents.orchestrator import BeatSelection
    from sidequest.server.narration_apply import _resolve_wwn_cast_for_beat

    actor = enc.find_actor(caster)
    cdef = pack.rules.confrontations[0]
    sel = BeatSelection(actor=caster, beat_id="cast_spell", spell_id=spell_id)
    _resolve_wwn_cast_for_beat(
        sel=sel,
        actor=actor,
        snapshot=snap,
        pack=pack,
        encounter=enc,
        cdef=cdef,
    )


class _RecordingHub:
    """Drop-in replacement for the watcher hub that records every event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    def __call__(self, event_type, payload, *, component, **_kw) -> None:
        self.events.append((event_type, payload, component))


@pytest.fixture
def watcher_hub(monkeypatch):
    hub = _RecordingHub()
    monkeypatch.setattr("sidequest.server.narration_apply._watcher_publish", hub)
    return hub


# ---------------------------------------------------------------------------
# refuse: casts_remaining == 0
# ---------------------------------------------------------------------------


def test_cast_refused_when_no_casts_remaining(otel_capture, monkeypatch):
    """A caster with casts_remaining=0 refuses the cast — casts unchanged, no
    damage applied, and wwn.spell.cast fires with refused=True."""
    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Wisp", "Brigand")
    snap.find_creature_core("Wisp").spellcasting.casts_remaining = 0
    opp_before = snap.find_creature_core("Brigand").hp.current

    _cast(snap=snap, enc=enc, pack=pack, caster="Wisp", spell_id="firebolt")

    assert snap.find_creature_core("Wisp").spellcasting.casts_remaining == 0
    assert snap.find_creature_core("Brigand").hp.current == opp_before

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(spans) == 1, f"wwn.spell.cast must fire on a refused cast; got {len(spans)}"
    assert spans[0].attributes["refused"] is True


# ---------------------------------------------------------------------------
# damage spell: cast spends, save rolled, damage applied (save halves)
# ---------------------------------------------------------------------------


def test_damage_spell_applies_to_defender_hp(otel_capture, monkeypatch):
    """A successful firebolt spends one cast, rolls the defender's mental save,
    and applies damage to the opponent's HP. Save MADE halves the damage."""
    # Force every randint to its max: the d20 save rolls 20 (>= difficulty 15 at
    # level 1) so save is MADE, and the 1d6 damage die rolls 6 -> halved to 3.
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: b)

    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Wisp", "Brigand")
    opp_before = snap.find_creature_core("Brigand").hp.current

    _cast(snap=snap, enc=enc, pack=pack, caster="Wisp", spell_id="firebolt")

    caster = snap.find_creature_core("Wisp")
    opp = snap.find_creature_core("Brigand")
    assert caster.spellcasting.casts_remaining == 1, "exactly one cast spent"
    # caster_level (1) x d6 = 1d6 -> 6, save MADE halves -> 3.
    assert opp.hp.current == opp_before - 3, (
        f"save-for-half damage must ablate the opponent's HP; "
        f"hp={opp.hp.current} expected {opp_before - 3}"
    )

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(spans) == 1
    assert spans[0].attributes.get("refused") is False
    assert spans[0].attributes["save_made"] is True
    # The HP delta emits a state_patch.hp span (the GM-panel lie-detector).
    hp_spans = [s for s in otel_capture.get_finished_spans() if s.name == "state_patch.hp"]
    assert hp_spans, "applying spell damage must emit a state_patch.hp span"


# ---------------------------------------------------------------------------
# 0 HP / downed: a killing cast drops the defender and fires the WWN downed seam
# ---------------------------------------------------------------------------


def test_killing_cast_fires_wwn_downed_seam(otel_capture, monkeypatch):
    """A firebolt that drops the defender to 0 HP runs the shared WWN downed
    seam — wwn.mortal_injury.declared fires and a Mortal Injury Scar attaches."""
    # Force the save to FAIL (randint low) so damage is NOT halved; opponent
    # seeded at hp=3 so a 1d6 (rolled via the same low randint -> 1... too low).
    # Use a bound-keyed lambda: 1d20 save -> 1 (fail), but force the 1d6 damage
    # high enough to kill. randint(1,20)->1 and randint(1,6)->6 can't both come
    # from one constant. Seed opponent at hp=1 so even a min damage roll kills,
    # and force ALL randint low so the save fails (no half) and the Major Injury
    # save (1d20) also fails deterministically.
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: a)

    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Wisp", "Brigand", opponent_hp=1)

    _cast(snap=snap, enc=enc, pack=pack, caster="Wisp", spell_id="firebolt")

    opp = snap.find_creature_core("Brigand")
    assert opp.hp.current == 0, (
        f"precondition: the killing cast must drop the defender to 0 HP; hp={opp.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.mortal_injury.declared" in span_names, (
        f"a WWN defender dropped to 0 HP by a cast must declare a Mortal Injury; "
        f"got spans: {span_names}"
    )
    assert any("Mortal Injury" in s.text for s in opp.statuses), (
        f"the downed defender must carry a Mortal Injury Scar; "
        f"statuses={[s.text for s in opp.statuses]}"
    )


# ---------------------------------------------------------------------------
# BUG 2a (eh-opp-damage): a killing cast must RESOLVE the encounter via
# hp_depletion. Playtest (elemental_harmony/burning_peace, WWN): a cast_spell
# CritSuccess drove the opponent to 0 HP but the encounter stayed
# resolved=False / active=True — the win condition never fired, so combat
# could not be WON. The strike path resolves hp_depletion inside apply_beat,
# but the cast path (_resolve_wwn_cast_for_beat) applied spell damage + ran the
# downed seam WITHOUT ever calling check_hp_depletion.
# ---------------------------------------------------------------------------


def test_killing_cast_resolves_encounter_via_hp_depletion(otel_capture, monkeypatch):
    """A cast that drops the seated opponent to 0 HP must RESOLVE the encounter
    (player_victory via hp_depletion) and emit encounter.resolved source=
    hp_depletion — the cast path's parity with the strike path's win-condition
    firing. RED before the fix: the opponent hits 0 HP but enc.resolved stays
    False (combat can't be won by a spell kill)."""
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: a)

    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Wisp", "Brigand", opponent_hp=1)

    _cast(snap=snap, enc=enc, pack=pack, caster="Wisp", spell_id="firebolt")

    opp = snap.find_creature_core("Brigand")
    assert opp.hp.current == 0, (
        f"precondition: the killing cast must drop the defender to 0 HP; hp={opp.hp.current}"
    )
    assert enc.resolved is True, (
        "a cast that drops the seated opponent to 0 HP must RESOLVE the encounter "
        "(the player can finally WIN via a spell kill)"
    )
    assert enc.outcome == "player_victory", (
        f"the resolution outcome must be player_victory; got {enc.outcome!r}"
    )
    resolved_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "encounter.resolved"
    ]
    assert resolved_spans, "an encounter.resolved span must fire on the spell kill"
    sources = [s.attributes.get("source") for s in resolved_spans]
    assert any("hp_depletion" in str(src) for src in sources), (
        f"resolution must be sourced to hp_depletion; got sources={sources}"
    )


def test_nonlethal_cast_leaves_encounter_active(otel_capture, monkeypatch):
    """A cast that damages but does NOT drop the opponent to 0 HP must leave the
    encounter unresolved — check_hp_depletion is a no-op above 0 HP, so the win
    condition only fires on a genuine kill (guards against an over-eager fix)."""
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: b)

    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Wisp", "Brigand", opponent_hp=20)

    _cast(snap=snap, enc=enc, pack=pack, caster="Wisp", spell_id="firebolt")

    opp = snap.find_creature_core("Brigand")
    assert opp.hp.current > 0, "precondition: the opponent survives the cast"
    assert enc.resolved is False, (
        "a non-lethal cast must NOT resolve the encounter — combat continues"
    )


# ---------------------------------------------------------------------------
# unknown spell id: refuse-recorded via the watcher event (no raise)
# ---------------------------------------------------------------------------


def test_unknown_spell_id_publishes_watcher_event(watcher_hub, monkeypatch):
    """An unknown spell id publishes wwn.cast_spell_unknown and refuses (no raise,
    no cast spent)."""
    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Wisp", "Brigand")
    before = snap.find_creature_core("Wisp").spellcasting.casts_remaining

    _cast(snap=snap, enc=enc, pack=pack, caster="Wisp", spell_id="meteor_swarm")

    assert snap.find_creature_core("Wisp").spellcasting.casts_remaining == before
    kinds = [e[0] for e in watcher_hub.events]
    assert "wwn.cast_spell_unknown" in kinds, (
        f"an unknown spell id must publish wwn.cast_spell_unknown; got {kinds}"
    )
    payload = next(e[1] for e in watcher_hub.events if e[0] == "wwn.cast_spell_unknown")
    assert "firebolt" in payload["available_ids"]
