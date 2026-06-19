"""Regression: a Fate confrontation opponent must carry a FateSheet (ADR-144 F2d /
playtest 150-2 re-brick at THROW RESOLUTION).

Fixing #964 (the d20 projection gate) moved the failure one stage downstream: the
player's Attack throw now reaches the Fate-native resolver, but the seated opponent
("Western Diamondback") had no FateSheet — every NPC in the pack carried a native
HP core with ``fate_sheet=None`` — so ``_seat_opponent_commits`` →
``decide_opponent_action`` raised ``ValueError`` (the correct No-Silent-Fallbacks
guard) and the round never resolved.

The fix seeds every seated Fate opponent with a FateSheet:
``FateRulesetModule.seed_opponent_fate_sheet`` builds it from the genre's authored
skill ladder (no invented balance — "Bind the Ruleset"), and the
``_seed_fate_opponents`` sweep (the Fate sibling of
``_seed_combat_hp_depletion_to_npcs``) attaches/creates it at encounter
instantiation.

Guards (mirrors the codebase's fate-subsystem test shape):
  * builder unit — seeds from the genre ladder + SRD baseline tracks; fails loud
    without a FateConfig.
  * sweep behavior — attach / create / leave-untouched / no-op-off-Fate, plus the
    end-to-end un-brick (``decide_opponent_action`` resolves after seeding).
  * wiring (reflection, not a source grep) — the sweep is CALLED from
    ``instantiate_encounter_from_trigger``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateEconomyError, FateRulesetModule
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.models.rules import FateConfig, RulesConfig
from sidequest.server.dispatch.encounter_lifecycle import (
    _seed_fate_opponents,
    instantiate_encounter_from_trigger,
)

# The engine's attack (Fight/Shoot/Provoke), defense (Athletics/Will), and order
# (Notice/Empathy) skills — a credible peer gunhand ladder.
_SKILLS = {"Shoot": 4, "Fight": 2, "Provoke": 3, "Athletics": 1, "Will": 2, "Notice": 3}


def _fate_rules() -> RulesConfig:
    return RulesConfig(ruleset="fate", fate=FateConfig(skills=dict(_SKILLS), refresh=3))


def _fate_pack() -> SimpleNamespace:
    # _seed_fate_opponents only reads ``pack.rules`` — a real RulesConfig is enough.
    return SimpleNamespace(rules=_fate_rules())


def _native_npc(name: str) -> Npc:
    # A bestiary-style creature: native core, NO fate_sheet (the bug's seated state).
    return Npc(core=CreatureCore(name=name, description="d", personality="p"))


def _enc(actors: list[EncounterActor]) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="standoff",
        category="pre_combat",
        player_metric=EncounterMetric(name="tension", threshold=10),
        opponent_metric=EncounterMetric(name="tension", threshold=10),
        actors=actors,
    )


def _duel_actors() -> list[EncounterActor]:
    return [
        EncounterActor(name="Reb", role="lead", side="player"),
        EncounterActor(name="Western Diamondback", role="foe", side="opponent"),
    ]


# ---------------------------------------------------------------------------
# 1 — seed_opponent_fate_sheet: the builder.
# ---------------------------------------------------------------------------


def test_builder_seeds_from_genre_skill_ladder() -> None:
    module = get_ruleset_module("fate")
    assert isinstance(module, FateRulesetModule)
    sheet = module.seed_opponent_fate_sheet(rules=_fate_rules())
    # Genre ladder verbatim — a peer adversary using the genre's OWN skills, not
    # invented balance ("Bind the Ruleset, Don't Balance It").
    assert sheet.skills == _SKILLS
    # SRD baseline tracks present so absorb_shifts / taken-out can resolve.
    assert set(sheet.stress) == {"physical", "mental"}
    assert [c.level for c in sheet.consequences] == ["mild", "moderate", "severe", "extreme"]


def test_builder_fails_loud_without_fate_config() -> None:
    module = get_ruleset_module("fate")
    assert isinstance(module, FateRulesetModule)
    with pytest.raises(FateEconomyError):
        module.seed_opponent_fate_sheet(rules=RulesConfig(ruleset="dial"))


# ---------------------------------------------------------------------------
# 2 — _seed_fate_opponents: the sweep.
# ---------------------------------------------------------------------------


def test_attaches_sheet_to_seated_native_opponent() -> None:
    """The core regression: a seated native creature (fate_sheet=None) gets a sheet
    ATTACHED beside its existing core — no duplicate NPC fabricated."""
    actors = _duel_actors()
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        characters=[],
        npcs=[_native_npc("Western Diamondback")],
        encounter=_enc(actors),
    )
    assert snap.find_creature_core("Western Diamondback").fate_sheet is None

    _seed_fate_opponents(
        snapshot=snap, actors=actors, pack=_fate_pack(), turn=1, acting_character_name="Reb"
    )

    opp = snap.find_creature_core("Western Diamondback")
    assert opp.fate_sheet is not None
    assert opp.fate_sheet.skills == _SKILLS
    assert sum(1 for n in snap.npcs if n.core.name == "Western Diamondback") == 1


def test_creates_backing_npc_when_opponent_has_none() -> None:
    """A router-named opponent with no backing Npc is fabricated WITH a sheet
    (ephemeral, reaped with its encounter) so the engine has an Other to resolve."""
    actors = [
        EncounterActor(name="Reb", role="lead", side="player"),
        EncounterActor(name="The Drifter", role="foe", side="opponent"),
    ]
    snap = GameSnapshot(
        genre_slug="spaghetti_western", characters=[], npcs=[], encounter=_enc(actors)
    )
    assert snap.find_creature_core("The Drifter") is None

    _seed_fate_opponents(
        snapshot=snap, actors=actors, pack=_fate_pack(), turn=1, acting_character_name="Reb"
    )

    core = snap.find_creature_core("The Drifter")
    assert core is not None and core.fate_sheet is not None
    npc = next(n for n in snap.npcs if n.core.name == "The Drifter")
    assert npc.ephemeral is True


def test_leaves_authored_fate_adversary_untouched() -> None:
    """An opponent already carrying a sheet (an authored Fate adversary) is not
    re-seeded — its authored skills survive."""
    authored = Npc(
        core=CreatureCore(
            name="El Lobo",
            description="d",
            personality="p",
            fate_sheet=FateSheet(skills={"Shoot": 5}),
        )
    )
    actors = [
        EncounterActor(name="Reb", role="lead", side="player"),
        EncounterActor(name="El Lobo", role="foe", side="opponent"),
    ]
    snap = GameSnapshot(
        genre_slug="spaghetti_western", characters=[], npcs=[authored], encounter=_enc(actors)
    )
    _seed_fate_opponents(
        snapshot=snap, actors=actors, pack=_fate_pack(), turn=1, acting_character_name="Reb"
    )
    assert snap.find_creature_core("El Lobo").fate_sheet.skills == {"Shoot": 5}


def test_noop_off_fate_pack() -> None:
    """A native/WN pack's opponent seeding is owned elsewhere — the Fate sweep is a
    no-op, never fabricating a sheet on a non-Fate opponent."""
    actors = [
        EncounterActor(name="Reb", role="lead", side="player"),
        EncounterActor(name="Goblin", role="foe", side="opponent"),
    ]
    snap = GameSnapshot(
        genre_slug="caverns", characters=[], npcs=[_native_npc("Goblin")], encounter=_enc(actors)
    )
    _seed_fate_opponents(
        snapshot=snap,
        actors=actors,
        pack=SimpleNamespace(rules=RulesConfig(ruleset="dial")),
        turn=1,
        acting_character_name="Reb",
    )
    assert snap.find_creature_core("Goblin").fate_sheet is None


def test_decide_opponent_action_resolves_after_seeding() -> None:
    """End-to-end of the un-brick: pre-seed it RAISES (the playtest crash); post-seed
    it returns a real attack decision."""
    from sidequest.game.fate_opponent import decide_opponent_action

    actors = _duel_actors()
    enc = _enc(actors)
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        characters=[],
        npcs=[_native_npc("Western Diamondback")],
        encounter=enc,
    )
    opp_actor = enc.find_actor("Western Diamondback")

    with pytest.raises(ValueError):
        decide_opponent_action(encounter=enc, snapshot=snap, opponent=opp_actor, mental=False)

    _seed_fate_opponents(
        snapshot=snap, actors=actors, pack=_fate_pack(), turn=1, acting_character_name="Reb"
    )

    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=opp_actor, mental=False
    )
    assert decision is not None
    assert decision.action == "attack"
    assert decision.target == "Reb"
    assert decision.skill in {"Fight", "Shoot"}  # physical attack skills


# ---------------------------------------------------------------------------
# 3 — Wiring: the sweep is called from encounter instantiation.
# ---------------------------------------------------------------------------


def _referenced_names(code: object) -> set[str]:
    """All names referenced by a compiled code object, recursing nested code objects
    (the reflection-based wiring check allowed by CLAUDE.md — NOT a source grep)."""
    names: set[str] = set(getattr(code, "co_names", ()))
    for const in getattr(code, "co_consts", ()):
        if hasattr(const, "co_names"):
            names |= _referenced_names(const)
    return names


def test_sweep_is_wired_into_instantiate() -> None:
    referenced = _referenced_names(instantiate_encounter_from_trigger.__code__)
    assert "_seed_fate_opponents" in referenced, (
        "_seed_fate_opponents is never called from instantiate_encounter_from_trigger — "
        "Fate opponents would seat without a FateSheet and re-brick at throw resolution"
    )
    # Sanity anchor: the native sibling seeder is still wired here too.
    assert "_seed_combat_hp_depletion_to_npcs" in referenced
