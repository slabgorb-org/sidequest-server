"""Provenance marker ``manual_origin`` on ``NpcPatch`` → ``Npc`` — Story 72-3.

Epic 72 (NPC Identity Hardening): once a Monster-Manual-authored NPC
(ADR-059 pre-generated, game-state-injected) lands in ``snapshot.npcs``,
nothing distinguishes it from a narrator-invented one. ``Npc.pool_origin``
only records the *pool member* an NPC was promoted from — it never says
"this came from the Monster Manual." This story carries one provenance
flag, end to end:

    NpcPatch.manual_origin  →  Npc.manual_origin  (through both seam legs)

The marker is a **boolean** authorship predicate, modelled alongside the
existing ``pool_origin``. ``is_creature`` already distinguishes a Manual
*creature* from a Manual *human*; ``manual_origin`` answers the orthogonal
question "did the Manual author this NPC at all?" — which is binary.

These tests pin the data model (AC1/AC3) and the two materialization legs
(AC2) plus the negative/collision edges (E1/E2). They are deliberately
decoupled from the OTEL emission path (covered in
``tests/integration/test_npc_manual_origin_otel.py``) so a failure here is
unambiguous: the field model or the merge seam itself is wrong.
"""

from __future__ import annotations

import json

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, NpcPatch, WorldStatePatch


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[],
    )


def _bare_npc(name: str) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
    )


# ---------------------------------------------------------------------------
# AC1 — NpcPatch carries a provenance marker (default = non-MM)
# ---------------------------------------------------------------------------


def test_npc_patch_defaults_manual_origin_false() -> None:
    """An unmarked patch (the narrator path) defaults to the non-MM value.

    Existing narrator-emitted patches and fixtures must round-trip
    unchanged, so the default cannot be ``True``."""
    patch = NpcPatch(name="Shopkeeper")
    assert patch.manual_origin is False


def test_npc_patch_accepts_manual_origin_true() -> None:
    """A Monster-Manual emitter constructs the patch with the marker set."""
    patch = NpcPatch(name="Chalk Moth", threat_level=2, hp=6, manual_origin=True)
    assert patch.manual_origin is True


def test_npc_patch_manual_origin_is_declared_field_not_extra() -> None:
    """``NpcPatch`` is ``extra="forbid"`` — the marker must be a declared
    model field, not an ad-hoc dict key that the validator rejects."""
    # Round-trips through validation: a forbidden/undeclared key would raise.
    patch = NpcPatch.model_validate({"name": "Hob", "manual_origin": True})
    assert patch.manual_origin is True
    assert "manual_origin" in NpcPatch.model_fields


# ---------------------------------------------------------------------------
# AC3 — Npc stores the marker and it round-trips through persistence
# ---------------------------------------------------------------------------


def test_npc_defaults_manual_origin_false() -> None:
    assert _bare_npc("Wexley").manual_origin is False


def test_npc_manual_origin_is_declared_field() -> None:
    assert "manual_origin" in Npc.model_fields


def test_npc_manual_origin_survives_pydantic_round_trip() -> None:
    """Game state round-trips through Pydantic (and JSON save files). The
    provenance must survive ``model_dump`` → JSON → ``model_validate``
    losslessly or it would silently reset to ``False`` on every save/load."""
    npc = _bare_npc("Salt Burrower")
    npc.manual_origin = True

    restored = Npc.model_validate(json.loads(json.dumps(npc.model_dump(mode="json"))))
    assert restored.manual_origin is True


# ---------------------------------------------------------------------------
# AC2 — the marker survives materialization into the canonical Npc
# ---------------------------------------------------------------------------


def test_marked_patch_materializes_manual_origin_npc() -> None:
    """A Manual-marked patch driven through ``apply_world_patch`` materializes
    (``_npc_from_patch``) an ``Npc`` whose provenance reads manual-origin."""
    snap = _snapshot()
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Chalk Moth", threat_level=2, hp=6, manual_origin=True)]
        )
    )
    moth = next(n for n in snap.npcs if n.core.name == "Chalk Moth")
    assert moth.manual_origin is True


def test_marked_patch_records_manual_origin_on_existing_npc_via_merge() -> None:
    """The same marked patch applied when an ``Npc`` of that name already
    exists takes the ``_merge_npc_patch`` leg and must also record
    manual-origin on the existing record."""
    snap = _snapshot()
    snap.npcs.append(_bare_npc("Chalk Moth"))  # pre-existing, unmarked
    assert snap.npcs[0].manual_origin is False  # precondition

    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Chalk Moth", threat_level=2, hp=6, manual_origin=True)]
        )
    )
    # Merged in place — not duplicated.
    assert len(snap.npcs) == 1
    assert snap.npcs[0].manual_origin is True


# ---------------------------------------------------------------------------
# E1 — narrator-invented NPC must NOT get manual_origin (negative)
# ---------------------------------------------------------------------------


def test_narrator_patch_materializes_non_manual_origin() -> None:
    """An ``NpcPatch`` emitted by the narrator path (no marker set)
    materializes an ``Npc`` whose provenance reads *not* manual-origin.
    Guards against an accidental default-true on the materializer."""
    snap = _snapshot()
    snap.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Shopkeeper", role="merchant")])
    )
    keep = next(n for n in snap.npcs if n.core.name == "Shopkeeper")
    assert keep.manual_origin is False


# ---------------------------------------------------------------------------
# E2 — merge collision: MM authority is authoritative and monotonic
# ---------------------------------------------------------------------------


def test_merge_mm_patch_over_invented_npc_records_manual_origin() -> None:
    """E2 (forward): an MM-marked patch collides with an already-present
    *narrator-invented* ``Npc`` of the same ``core.name``. MM authorship is
    authoritative — the surviving record reads manual-origin. The engine must
    not silently keep "invented" when an authored Manual patch arrives."""
    snap = _snapshot()
    # Narrator invented this NPC first (no marker).
    snap.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Hob", description="a wary scavenger")])
    )
    assert snap.npcs[0].manual_origin is False  # precondition: invented

    # The Monster Manual now authors the same name.
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Hob", threat_level=1, hp=4, manual_origin=True)]
        )
    )
    assert len(snap.npcs) == 1
    assert snap.npcs[0].manual_origin is True


def test_merge_narrator_patch_does_not_clear_manual_origin() -> None:
    """E2 (reverse / monotonicity): once an ``Npc`` is marked manual-origin,
    a later *narrator* patch (marker absent/False) for the same name must NOT
    clear the marker. Provenance is monotonic — authorship cannot be undone by
    subsequent narrator improv."""
    snap = _snapshot()
    # Manual authors the NPC first.
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Hob", threat_level=1, hp=4, manual_origin=True)]
        )
    )
    assert snap.npcs[0].manual_origin is True  # precondition: manual

    # Narrator re-describes the same NPC (no marker).
    snap.apply_world_patch(
        WorldStatePatch(
            npcs_present=[NpcPatch(name="Hob", description="now seems almost friendly")]
        )
    )
    assert len(snap.npcs) == 1
    assert snap.npcs[0].manual_origin is True, (
        "narrator patch cleared MM provenance — authorship must be monotonic"
    )
