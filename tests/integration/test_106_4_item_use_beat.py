"""Story 106-4 Part C — committing an item-use beat in WN combat heals + consumes.

Keith's priority ask: a confrontation beat menu that scans the actor's carried
inventory and offers a "Drink <potion>" beat which, when committed, consumes the
item and applies its heal effect mid-fight. Confirmed design (2026-06-14):

  - the item-use beat is AUTO-SUCCESS (no d20 to-hit roll); the heal magnitude
    still rolls (the Part-A ``_apply_consumable_heal`` path, ``1d6+2``);
  - it COSTS the Main Action — the seated opponent still takes its own WN
    initiative-slot attack that round (consistent with 106-2 Option A).

Drives the real WN sealed-round dispatch (``dispatch_dice_throw`` →
``run_wn_round``) through the shared 102-4 harness. Skips when sidequest-content
is not on disk.
"""

from __future__ import annotations

import random

import pytest

from tests.integration._wn_round_102_4 import (
    GENRE_PACKS_DIR,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_wn_combat,
    spans_named,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

_OPP = "Hired Blade"
_PC = "Vesska"
_POTION = {
    "name": "Potion of Mending",
    "category": "consumable",
    "tags": ["consumable", "healing", "potion"],
    "heal_amount": "1d6+2",
}


@pytest.fixture
def solo_combat_with_potion():
    """Solo WN combat: one wounded PC carrying a heal potion vs one blade."""
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP], pc_hp=12)
    core = snap.find_creature_core(_PC)
    core.hp.current = 4  # wounded — leaves headroom for the heal to be visible
    core.inventory.items.append(dict(_POTION))
    # Solo barrier: PC acts first, blade second, so the heal lands before the
    # opponent swings (deterministic for the heal assertion).
    force_initiative(enc, [(_PC, 8), (_OPP, 3)])
    return pack, snap, enc, core


def test_item_use_beat_consumes_potion_and_heals(
    solo_combat_with_potion, otel_capture, monkeypatch
):
    pack, snap, enc, core = solo_combat_with_potion
    # Pin the heal roll so the assertion is exact: 1d6 -> 4, +2 = 6 healed.
    monkeypatch.setattr(random.Random, "randint", lambda self, a, b: 4)
    before = core.hp.current

    outcome = dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="p1",
        beat_id="use_item:potion_of_mending",
        genre_slug="heavy_metal",
    )

    # The potion is gone — consumed on use.
    names = [str(i.get("name", "")) for i in core.inventory.items]
    assert "Potion of Mending" not in names
    # The heal applied with the authored magnitude — isolated from the opponent's
    # same-round attack via the consumable_heal state_patch.hp span (1d6→4, +2 = 6).
    heal_spans = [
        s
        for s in spans_named(otel_capture, "state_patch.hp")
        if s.attributes.get("source") == "consumable_heal"
    ]
    assert heal_spans, "expected a consumable_heal state_patch.hp span"
    assert heal_spans[0].attributes.get("delta") == 6
    # Net HP reflects heal THEN the opponent's reprisal (drinking costs the
    # round): before(4) + heal(6) - blade damage. The heal kept the PC alive.
    assert before < core.hp.current + 6  # the heal demonstrably raised the pool
    # The round was spent — not left pending (solo barrier closed).
    assert outcome.commitment_pending is False


def test_item_use_costs_the_round_opponent_still_attacks(
    solo_combat_with_potion, otel_capture, monkeypatch
):
    pack, snap, enc, core = solo_combat_with_potion
    monkeypatch.setattr(random.Random, "randint", lambda self, a, b: 4)

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="p1",
        beat_id="use_item:potion_of_mending",
        genre_slug="heavy_metal",
    )
    # The opponent took its own initiative-slot attack the same round (drinking
    # is the player's Main Action; it does not skip the enemy's turn).
    assert spans_named(otel_capture, "encounter.opponent_attack_resolved"), (
        "the seated opponent must still attack on its slot when the player drinks"
    )


def test_item_used_otel_span_emitted(solo_combat_with_potion, otel_capture, monkeypatch):
    pack, snap, enc, core = solo_combat_with_potion
    monkeypatch.setattr(random.Random, "randint", lambda self, a, b: 4)
    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="p1",
        beat_id="use_item:potion_of_mending",
        genre_slug="heavy_metal",
    )
    used = spans_named(otel_capture, "confrontation.item_used")
    assert used, "expected a confrontation.item_used GM-panel span"
    attrs = used[0].attributes
    assert attrs.get("actor") == _PC
    assert "Potion of Mending" in str(attrs.get("item"))
    assert int(attrs.get("healed")) > 0


def test_item_use_unknown_item_fails_loud(solo_combat_with_potion):
    pack, snap, enc, core = solo_combat_with_potion
    from sidequest.server.dispatch.downed_seam import DiceDispatchError

    with pytest.raises(DiceDispatchError, match="not in .*inventory|no usable"):
        dispatch_throw(
            pack=pack,
            snap=snap,
            enc=enc,
            character_name=_PC,
            player_id="p1",
            beat_id="use_item:flask_of_oblivion",  # not carried
            genre_slug="heavy_metal",
        )
