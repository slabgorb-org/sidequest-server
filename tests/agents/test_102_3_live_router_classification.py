"""Story 102-3 AC4 live half — REAL router classifies an explicit named cast.

Opt-in LIVE verification (the 91-3 `SIDEQUEST_VERIFY_*` pattern): the real
IntentRouter, real SDK adapter, real Haiku call. This is the only check that
exercises the production router-prompt extension (named casts must emit
``params={"actor", "spell"}`` and clear the confidence threshold) against the
actual model — the deterministic suite stubs the router by design.

LOCATION MATTERS (review finding R1, 2026-06-10): this test originally lived
in ``tests/server/``, where the autouse hermeticity tripod
(``tests/server/conftest.py``: ``_stub_intent_router_factory`` +
``_no_real_anthropic_sdk``) makes a live call structurally impossible — the
opted-in run failed against a stub's empty package, and a stub that emitted
dispatches would have false-PASSED. ``tests/agents/`` carries no router/SDK
stubs (only the cost-ledger reset at the tests/ root), so the opt-in here
reaches the real model. Do not move this back under ``tests/server/``.

Gating: skips unless ``SIDEQUEST_VERIFY_FREEPLAY_CAST_LIVE=1`` AND
``ANTHROPIC_API_KEY`` are set — CI and xdist runs stay deterministic.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import SpellcastingState

_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "CONSTITUTION": "Vigor",
    "DEXTERITY": "Grace",
    "INTELLIGENCE": "Lore",
    "WISDOM": "Wit",
    "CHARISMA": "Bearing",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_CASTER = "Vesska"
_SPELL_ID = "foundation_of_flame"


def _make_wwn_pack() -> Any:
    """Mirror of the deterministic suite's WN pack fixture
    (tests/server/test_102_3_freeplay_cast_magic_working.py) — kept local so
    this file has zero imports from the hermetic tests/server tree."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig, SystemStrainConfig, WwnConfig
    from sidequest.genre.models.wwn_spell import WwnSpell, WwnSpellCatalog

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        wwn=WwnConfig(
            attribute_map=dict(_ATTRIBUTE_MAP),
            system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        ),
    )
    pack.worlds = {}
    pack.wwn_spell_catalog = WwnSpellCatalog(
        spells=[
            WwnSpell(
                id=_SPELL_ID,
                name="Foundation of Flame",
                level=1,
                save=None,
                damage_die="1d6",
                damage_per_level=False,
                genre_description="A floor of coals spreads from the caster's heel.",
                mechanical_effect="1d6 fire; ignites unattended tinder",
            ),
        ]
    )
    pack.witnessed_acts = None
    return pack


def _wwn_snapshot() -> GameSnapshot:
    core = CreatureCore(
        name=_CASTER,
        description="A foundry-witch of the Long Foundry.",
        personality="deliberate",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        spellcasting=SpellcastingState(
            prepared=[_SPELL_ID],
            casts_remaining=2,
            casts_per_day=2,
            max_spell_level=1,
        ),
    )
    char = Character(
        core=core,
        char_class="Mage",
        race="Human",
        backstory="Apprenticed at the slag-gates.",
        stats={name: 10 for name in _ABILITY_SCORE_NAMES},
    )
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="long_foundry",
        turn_manager=TurnManager(),
        player_seats={"player:Keith": _CASTER},
    )
    snap.characters.append(char)
    return snap


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_live_router_classifies_explicit_named_cast_as_magic_working() -> None:
    """AC4 live half (opt-in): live Haiku classification of an explicit named
    cast emits a ``magic_working`` dispatch carrying the spell name, at a
    confidence that clears the bank's engagement threshold (story guardrail:
    an explicit named cast must clear confidence gating).
    """
    if os.environ.get("SIDEQUEST_VERIFY_FREEPLAY_CAST_LIVE") != "1":
        pytest.skip("set SIDEQUEST_VERIFY_FREEPLAY_CAST_LIVE=1 (+ ANTHROPIC_API_KEY) to run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY required for live classification")

    from sidequest.agents.subsystems import DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD
    from sidequest.server.intent_router_pass import (
        _build_state_summary,
        build_intent_router_for_session,
    )

    snap = _wwn_snapshot()
    pack = _make_wwn_pack()
    router = build_intent_router_for_session(session_id=None)

    # Live-reach tripwire (review R1): the production factory must hand back a
    # real IntentRouter, not a test double — if a conftest stub intercepts the
    # factory again, fail HERE with a diagnosis instead of asserting against a
    # stub's empty package downstream.
    from sidequest.agents.intent_router import IntentRouter

    assert isinstance(router, IntentRouter), (
        f"build_intent_router_for_session returned {type(router).__name__!r} — a "
        "conftest stub intercepted the factory; this LIVE test must run in a tree "
        "with no router stub (see module docstring)"
    )

    package = await router.decompose(
        action="I cast foundation_of_flame at the rust-wight blocking the gate.",
        state_summary=_build_state_summary(snap, pack=pack),
    )

    magic_dispatches = [
        d for pd in package.per_player for d in pd.dispatch if d.subsystem == "magic_working"
    ] + [d for ca in package.cross_player for d in ca.dispatch if d.subsystem == "magic_working"]
    assert magic_dispatches, (
        "an explicit named cast ('I cast foundation_of_flame ...') must "
        "classify as magic_working; got subsystems="
        f"{[d.subsystem for pd in package.per_player for d in pd.dispatch]}"
    )
    d = magic_dispatches[0]
    assert "foundation" in str(d.params).lower(), (
        f"the dispatch params must carry the named spell; got params={d.params}"
    )
    assert d.confidence >= DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD, (
        f"an explicit named cast must clear the engagement threshold "
        f"({DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD}); got {d.confidence}"
    )
