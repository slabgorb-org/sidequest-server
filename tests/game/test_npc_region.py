"""Task 1 — NPC region stamp.

Verifies that ``NpcPatch.region`` is accepted and carried through both
materialization legs:
  - ``_npc_from_patch`` (spawn): sets ``npc.region`` on first encounter.
  - ``_merge_npc_patch`` (merge): updates ``npc.region`` when a later patch
    carries a non-None region.

These are pure-model tests — no Postgres, no OTEL side-effects asserted.
"""

from sidequest.game.session import GameSnapshot, NpcPatch, WorldStatePatch


def _snap() -> GameSnapshot:
    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def test_npc_from_patch_carries_region() -> None:
    """Spawn leg: region flows from NpcPatch → Npc.region."""
    snap = _snap()
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Gnaw-Swarm", hp=6, threat_level=1, region="exp002.r3")]
        )
    )
    npc = next(n for n in snap.npcs if n.core.name == "Gnaw-Swarm")
    assert npc.region == "exp002.r3"


def test_merge_npc_patch_updates_region() -> None:
    """Merge leg: a subsequent patch with a non-None region updates npc.region."""
    snap = _snap()
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Gnaw-Swarm", hp=6, threat_level=1, region="exp002.r3")]
        )
    )
    snap.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Gnaw-Swarm", region="exp004.r1")])
    )
    npc = next(n for n in snap.npcs if n.core.name == "Gnaw-Swarm")
    assert npc.region == "exp004.r1"
