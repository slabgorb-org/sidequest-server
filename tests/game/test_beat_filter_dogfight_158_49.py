"""Story 158-49 (RED) — a sealed-letter SWN dogfight must NOT be handed the
Without-Number PERSONAL-combat action menu.

PLAYTEST (2026-06-27, solo space_opera/coyote_star, SWN, player "Moe"): a
forced-dispatch dogfight (ADR-153 §7 / 158-29) seated a "Fighter Duel" whose beat
menu was Attack (STR) / Total Defense (DEX) / Fighting Withdrawal (DEX) / Run (DEX)
— the Without-Number SRD §2.4.4 *ground/personal* combat action set synthesized by
``beat_filter.beats_available_for`` (the ``is_wn_binding and win_condition ==
'hp_depletion'`` block at beat_filter.py:368). Committing "Attack (STR)" crashed the
SWN resolver and soft-locked the confrontation.

ROOT CAUSE: that synthesis block gates on ``win_condition == 'hp_depletion'`` ALONE
and ignores ``resolution_mode``. Since #508 made the sealed-letter dogfight resolve
via SWN ``hp_depletion``, the dogfight wrongly inherits the personal-combat menu
instead of its ADR-153 sealed-letter maneuvers (Throttle Up / Break Right / loop /
kill_rotation, from ``dogfight/interactions_mvp.yaml``).

DISCRIMINATOR NOTE (post-#510): the original story framed the bug as a stat-NAME
mismatch (SWN had no STR/DEX). After #510 canonicalized space_opera to STR/DEX, the
stat name no longer distinguishes a valid dogfight beat from the WWN-default pool —
the discriminator is beat IDENTITY: the personal-combat action set
(attack/total_defense/fighting_withdrawal/run) is semantically wrong for a ship
dogfight regardless of which stat key resolves. These tests assert on beat identity.

Real fixture cdefs (``swn_test_pack``: ``dogfight`` is sealed_letter_lookup /
hp_depletion, ``combat`` is beat_selection / hp_depletion) are used so the tests
exercise content-shaped defs that mirror live space_opera, not hand-rolled stubs.
"""

from __future__ import annotations

import pytest

from sidequest.game.beat_filter import beats_available_for
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef, ResolutionMode
from tests._helpers.fixture_packs import SWN_TEST_PACK, load_fixture_pack

# The synthesized Without-Number PERSONAL-combat action set (beat_filter.wn_action_beat):
# Attack (STR strike) + the defensive/move actions (DEX). NONE of these belong on a
# sealed-letter ship dogfight — its actions are the interaction-table maneuvers.
_WN_PERSONAL_COMBAT_IDS = {"attack", "total_defense", "fighting_withdrawal", "run"}


@pytest.fixture(scope="module")
def swn_pack() -> GenrePack:
    return load_fixture_pack(SWN_TEST_PACK)


def _cdef(pack: GenrePack, confrontation_type: str) -> ConfrontationDef:
    cdef = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == confrontation_type),
        None,
    )
    assert cdef is not None, f"fixture must author a {confrontation_type!r} confrontation"
    return cdef


def _class(display_name: str, choices: list[str]) -> ClassDef:
    """A minimal non-caster ClassDef whose ``encounter_beat_choices`` enumerate NO
    Without-Number action id — so the ONLY way a WN action reaches the menu is the
    is_wn_binding synthesis under test (not gate-2 whitelisting)."""
    return ClassDef(
        id=display_name.lower(),
        display_name=display_name,
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table=f"{display_name.lower()}_kit",
        flavor="-",
        encounter_beat_choices=choices,
    )


def test_sealed_letter_dogfight_is_a_sealed_letter_hp_depletion_combat(swn_pack: GenrePack) -> None:
    """Precondition guard: the fixture dogfight is the shape that trips the bug —
    sealed_letter_lookup resolution with hp_depletion win condition. If this drifts,
    the RED tests below would pass for the wrong reason."""
    dogfight = _cdef(swn_pack, "dogfight")
    assert dogfight.resolution_mode == ResolutionMode.sealed_letter_lookup
    assert dogfight.win_condition == "hp_depletion"


def test_sealed_letter_dogfight_does_not_offer_wn_personal_combat_menu(swn_pack: GenrePack) -> None:
    """RED (AC2): under a Without-Number binding the sealed-letter dogfight menu must
    NOT contain the synthesized personal-combat action set. Today the synthesis fires
    (gates on hp_depletion alone) and leaks attack/total_defense/fighting_withdrawal/run
    onto a ship dogfight."""
    dogfight = _cdef(swn_pack, "dogfight")
    pilot = _class("Pilot", ["sprint"])  # non-empty, enumerates no WN action id

    offered = beats_available_for(
        dogfight, pilot, spell_slots_remaining=0.0, is_wn_binding=True
    )
    leaked = {b.id for b in offered} & _WN_PERSONAL_COMBAT_IDS

    assert not leaked, (
        "a sealed-letter SWN dogfight must not surface the WN personal-combat action "
        f"set (those are ground-combat actions); leaked ids: {sorted(leaked)}"
    )


def test_sealed_letter_dogfight_offers_no_personal_stat_check_beats(swn_pack: GenrePack) -> None:
    """RED (AC2): every beat the dogfight offers must be ruleset-valid for a
    sealed-letter duel — a maneuver resolves by table lookup and carries NO personal
    STR/DEX stat_check. No offered beat may carry one (that is the personal-combat
    pool whose STR check reaches without_number.attack_params)."""
    dogfight = _cdef(swn_pack, "dogfight")
    pilot = _class("Pilot", ["sprint"])

    offered = beats_available_for(
        dogfight, pilot, spell_slots_remaining=0.0, is_wn_binding=True
    )
    personal_stat_beats = [
        b.id for b in offered if (b.stat_check or "").upper() in {"STR", "DEX"}
    ]

    assert not personal_stat_beats, (
        "sealed-letter dogfight offered beats carrying a personal STR/DEX stat_check "
        f"(the WWN-default pool that crashes the SWN resolver): {personal_stat_beats}"
    )


def test_ground_combat_still_synthesizes_wn_action_menu(swn_pack: GenrePack) -> None:
    """GREEN guard (AC6): the fix must scope to sealed-letter dogfights only — a
    NON-sealed-letter (beat_selection) SWN hp_depletion combat still gets the WN
    action menu, so ordinary ground/personal SWN combat keeps working. This passes
    today and must keep passing: it stops the fix over-correcting by ripping out the
    WN action synthesis wholesale."""
    combat = _cdef(swn_pack, "combat")
    assert combat.resolution_mode == ResolutionMode.beat_selection  # not sealed-letter
    warrior = _class("Warrior", ["sprint"])

    offered = beats_available_for(
        combat, warrior, spell_slots_remaining=0.0, is_wn_binding=True
    )

    assert "attack" in {b.id for b in offered}, (
        "ground SWN combat must keep the synthesized WN Attack action — the fix must "
        "not remove the WN action menu for non-dogfight hp_depletion combat"
    )
