"""Task 1 — NPC region stamp.

Verifies that ``NpcPatch.region`` is carried through the materialization
builder ``GameSnapshot._npc_from_patch`` (spawn): sets ``npc.region`` on
first encounter. This is the production leg — ``monster_manual_inject``
builds every candidate Npc through ``_npc_from_patch`` before routing it
through ``green_room.admit()`` (ADR-156).

The old merge leg (``_merge_npc_patch`` updating ``npc.region`` from a later
patch) was removed with the ``WorldStatePatch.npcs_present`` lane (Green Room
follow-up, 2026-07-11); merge semantics now live in ``green_room.admit()``'s
additive ``_fill_absent`` (region fills only when absent) PLUS, since the
final-review Finding 1 fix (2026-07-12),
``monster_manual_inject._refresh_merged_placement`` — a per-turn placement
refresh scoped to the MM feeder that restores the deleted leg's behavior for
``location``/``region`` specifically (never HP/disposition/beliefs, ADR-139
Inv-2). ``_fill_absent`` alone would leave a re-injected identity's region
frozen at first materialization; the refresh is what makes a *later* patch's
region win again. See
``tests/server/test_green_room_mm_feeders.py::test_reinject_at_new_location_refreshes_merged_placement``
for the restored regression coverage (inject at region A, then region B,
assert the merged NPC's region moved — plus the legacy origin=None
first-stamp).

Pure-model test — no Postgres, no OTEL side-effects asserted.
"""

from sidequest.game.session import GameSnapshot, NpcPatch


def _snap() -> GameSnapshot:
    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def test_npc_from_patch_carries_region() -> None:
    """Spawn leg: region flows from NpcPatch → Npc.region."""
    snap = _snap()
    npc = snap._npc_from_patch(
        NpcPatch(name="Gnaw-Swarm", hp=6, threat_level=1, region="exp002.r3"),
        emit_spawn_span=False,
    )
    assert npc.region == "exp002.r3"
