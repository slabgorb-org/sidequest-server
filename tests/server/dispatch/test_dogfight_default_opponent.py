"""ADR-153 §6 — a dogfight with no router-named contact and no co-located Other
still seats an enemy ship, sourced from the def's ``opponent_default_stats``
frame (default-from-frame). A dogfight requires an Other (ADR-116); the def
frame IS that Other (a generic enemy fighter), so the duel always seats rather
than refusing.

RED today: after the §6 ship-scale gate (test_dogfight_seating_scale) the
no-named-opponent path has nothing to seat and raises ``SealedLetterArityError``;
this test asserts the seater instead synthesizes a frame-default ship whose
backing core HP comes from the def frame (8).
"""

from __future__ import annotations

from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from tests.fixtures.dogfight_playtest_encounter import (
    GENRE_SLUG,
    make_dogfight_pack,
    make_empty_snapshot,
)


def test_dogfight_seats_default_ship_opponent_from_frame_when_none_named() -> None:
    """No router-named opponent, no co-located Other: the dogfight still seats
    an enemy ship sourced from the def frame (ADR-116 + ADR-153 §6), and its
    backing core HP equals ``opponent_default_stats["hp"]`` (8)."""
    pack = make_dogfight_pack()  # dogfight def: opponent_default_stats.hp == 8
    snapshot = make_empty_snapshot(pc_name="Pilot")

    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type="dogfight",
        player_name="Pilot",
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )

    assert enc is not None, "a dogfight with a framed def must seat, not refuse"
    opponents = [a for a in enc.actors if a.side == "opponent"]
    assert len(opponents) == 1, (
        f"expected exactly one frame-default opponent, got {[a.name for a in opponents]!r}"
    )
    # The seated opponent's backing core HP came from the def frame, not a
    # native dial or a co-located creature.
    core = snapshot.find_creature_core(opponents[0].name)
    assert core is not None, (
        f"frame-default opponent {opponents[0].name!r} has no backing creature "
        "core — the duel cannot resolve via hp_depletion without one"
    )
    assert core.hp.max == 8, (
        f"frame-default opponent HP must come from opponent_default_stats['hp']=8, "
        f"got max={core.hp.max}"
    )
