"""Task 4: net_run security_tier stamping at instantiation.

spec 2026-05-29 — net_run is a CWN hacking confrontation (category "hacking",
dial: player_metric=data / opponent_metric=alert). Its "Other" is the alert
dial, not a seated NPC opponent (ADR-116). The security_tier is stamped at
instantiation time from the dispatch param or the pack's authored
cwn.hacking.default_tier. Unknown tier and missing cwn.hacking block both
fail loud (No Silent Fallbacks).
"""

from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    CwnConfig,
    HackingConfig,
    MetricDef,
    RulesConfig,
)
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)


def _net_run_def() -> ConfrontationDef:
    return ConfrontationDef(
        type="net_run",
        label="Net Run",
        category="hacking",
        player_metric=MetricDef(name="data", starting=0, threshold=10),
        opponent_metric=MetricDef(name="alert", starting=0, threshold=10),
        beats=[
            BeatDef(
                id="run_program", label="Run Program", kind="strike", base=2, stat_check="Tech"
            ),
        ],
        mood="tension",
    )


def _pack(hacking: HackingConfig | None) -> GenrePack:
    cwn = CwnConfig(
        attribute_map={
            "STRENGTH": "Brawn",
            "DEXTERITY": "Reflex",
            "CONSTITUTION": "Body",
            "INTELLIGENCE": "Tech",
            "WISDOM": "Instinct",
            "CHARISMA": "Cool",
        },
        hacking=hacking,
    )
    rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"],
        cwn=cwn,
        confrontations=[_net_run_def()],
    )
    return GenrePack.model_construct(rules=rules)


def _ladder() -> HackingConfig:
    return HackingConfig(
        default_tier="office",
        security_tiers={"home": 7, "office": 9, "facility": 11, "black_site": 12},
    )


def _snapshot() -> GameSnapshot:
    snap = GameSnapshot()
    snap.genre_slug = "test_neon"
    return snap


def test_net_run_stamps_named_tier():
    snap = _snapshot()
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(_ladder()),
        encounter_type="net_run",
        player_name="Rux",
        npcs_present=[],
        genre_slug="test_neon",
        security_tier="black_site",
    )
    assert enc is not None
    assert enc.security_tier == "black_site"


def test_net_run_defaults_tier_when_omitted():
    snap = _snapshot()
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(_ladder()),
        encounter_type="net_run",
        player_name="Rux",
        npcs_present=[],
        genre_slug="test_neon",
        # no security_tier supplied → authored default
    )
    assert enc is not None
    assert enc.security_tier == "office"


def test_net_run_unknown_tier_fails_loud():
    snap = _snapshot()
    with pytest.raises(ValueError, match="security_tier"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=_pack(_ladder()),
            encounter_type="net_run",
            player_name="Rux",
            npcs_present=[],
            genre_slug="test_neon",
            security_tier="moon_base",
        )


def test_net_run_without_ladder_fails_loud():
    snap = _snapshot()
    with pytest.raises(ValueError, match="cwn.hacking"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=_pack(None),
            encounter_type="net_run",
            player_name="Rux",
            npcs_present=[],
            genre_slug="test_neon",
        )
