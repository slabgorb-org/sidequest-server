"""ADR-153 §6 — a sealed-letter dogfight is ship-scale; it never conscripts a
co-located *ground* creature as the enemy vessel (playtest finding 158-34).

The 2026-06-25 coyote_star playtest seated a Monster-Manual ground creature
("Gengineered Killer") as the enemy ship, because the seater's location
fallback fires for any confrontation type not in
``_SHIP_SCALE_CONFRONTATION_TYPES`` (which held only ``{"ship_combat"}``) and a
dogfight's ``adversary_only`` fallback then grabbed the nearest hostile.

Per ADR-153 §6 the dogfight Other is a ship/chassis sourced from the def frame
(or a router-named contact), **never** the nearest co-located creature. With no
router-named opponent the seater must NOT seat the co-located ground creature —
it seats a default-from-frame enemy ship instead (see
``test_dogfight_default_opponent.py``).

RED today: the location fallback conscripts "Gengineered Killer" as blue, so the
seated opponent IS the ground creature.
"""

from __future__ import annotations

from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from tests.fixtures.dogfight_playtest_encounter import (
    GENRE_SLUG,
    make_dogfight_pack,
    make_snapshot_with_npc,
)


def test_sealed_letter_dogfight_does_not_seat_colocated_ground_creature() -> None:
    """A hostile PERSONAL-scale creature shares the scene (the 158-34
    "Gengineered Killer"). A ship dogfight must NOT conscript it as the enemy
    vessel — the seated opponent must be a ship/chassis from the def frame."""
    pack = make_dogfight_pack()
    snapshot = make_snapshot_with_npc(
        pc_name="Pilot",
        npcs=[("Gengineered Killer", {"role": "hostile", "is_creature": True})],
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type="dogfight",
        player_name="Pilot",
        npcs_present=[],  # router named no opponent
        genre_slug=GENRE_SLUG,
    )

    assert enc is not None, "the dogfight must still seat (ADR-116 requires an Other)"
    opponents = [a for a in enc.actors if a.side == "opponent"]
    assert len(opponents) == 1, (
        f"expected exactly one opponent actor, got {[a.name for a in opponents]!r}"
    )
    # The ground creature was NOT conscripted as the enemy vessel (ADR-153 §6).
    assert opponents[0].name != "Gengineered Killer", (
        "a co-located ground creature must never be seated as the dogfight "
        "opponent — the Other is a ship/chassis from the def frame"
    )
    # And it is not lurking in the scene as a seated actor under any side.
    assert all(a.name != "Gengineered Killer" for a in enc.actors), (
        "the ground creature must not be seated in the dogfight at all"
    )
