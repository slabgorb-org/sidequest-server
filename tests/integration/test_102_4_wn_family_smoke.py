"""Story 102-4 AC4 — sealed-commitment smoke across all four WN sisters.

Parametrized over the four REAL WN-bound packs (space_opera->swn,
heavy_metal->wwn, neon_dystopia->cwn, mutant_wasteland->awn): in each, a
first commit with a seated peer uncommitted must SEAL — no opponent
ablation, no ``{slug}.round.resolved`` span. This is the family-wide proof
that the turn model rides the module seam, not a heavy_metal special case;
the deep behavioral suite (ordering, dead premise) runs on heavy_metal in
its sibling files.

Each pack's combat shape is discovered from its own authored content (first
``category=combat`` + ``win_condition=hp_depletion`` ConfrontationDef, its
first ``kind=strike`` beat, stats from the pack's attribute_map flavor) —
the test adapts to the content, never the reverse
(feedback_tests_not_point_at_content is satisfied by reading, not pinning,
pack internals).

NATIVE REGRESSION (green today, must stay green): the same first-commit
drive on a native synthetic pack resolves immediately — commitment semantics
must never leak into native dispatch.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

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

_PC_A = "Vesska"
_PC_B = "Brakka"
_OPP = "Test Adversary"

FAMILY = [
    ("space_opera", "swn"),
    ("heavy_metal", "wwn"),
    ("neon_dystopia", "cwn"),
    ("mutant_wasteland", "awn"),
]


def _combat_shape(pack):
    """(encounter_type, strike_beat_id, stats) discovered from the pack."""
    cdef = next(
        c
        for c in pack.rules.confrontations
        if c.category == "combat" and c.win_condition == "hp_depletion"
    )
    beat = next(b for b in cdef.beats if b.kind == "strike")
    cfg = pack.rules.ruleset_config()
    assert cfg is not None, "a WN pack must carry its ruleset config block"
    stats = {flavor: 10 for flavor in cfg.attribute_map.values()}
    return cdef.confrontation_type, beat.id, stats


@pytest.mark.parametrize(("genre", "slug"), FAMILY)
def test_first_commit_seals_across_the_wn_family(
    genre: str, slug: str, otel_capture, monkeypatch
) -> None:
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack = load_pack(genre)
    encounter_type, beat_id, stats = _combat_shape(pack)
    snap, enc = seat_wn_combat(
        pack,
        [_PC_A, _PC_B],
        [_OPP],
        encounter_type=encounter_type,
        genre_slug=genre,
        stats=stats,
    )
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])
    opp_hp_before = snap.find_creature_core(_OPP).hp.current

    outcome = dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC_A,
        player_id="p1",
        beat_id=beat_id,
        genre_slug=genre,
        stats=stats,
    )

    assert outcome.commitment_pending is True, (
        f"[{slug}] a first commit with a peer uncommitted must seal — the "
        "turn model is family behavior behind the module seam"
    )
    assert snap.find_creature_core(_OPP).hp.current == opp_hp_before, (
        f"[{slug}] sealed commit must not ablate the opponent"
    )
    assert not spans_named(otel_capture, f"{slug}.round.resolved"), (
        f"[{slug}] no round.resolved may fire before the barrier closes — "
        "and the span must carry the HONEST slug (awn, not cwn)"
    )


def test_native_pack_still_resolves_immediately(otel_capture) -> None:
    """Characterization guard (green today, stays green): native dispatch
    has no commitment phase — the first throw applies its beat in-dispatch.
    Uses the synthetic native fixture so no real pack's flavor is pinned."""
    from tests.game.ruleset._dispatch_fixture import resolve_one_combat_beat

    outcome = resolve_one_combat_beat()
    assert getattr(outcome, "commitment_pending", False) is False, (
        "native throws must never seal — commitment semantics are WN-only"
    )
    assert not spans_named(otel_capture, "native.round.resolved"), (
        "native packs emit no WN round-phase spans (no ordering is the "
        "truthful state, not a fallback)"
    )
