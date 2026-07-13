"""Provenance marker ``manual_origin`` on ``NpcPatch`` → ``Npc`` — Story 72-3.

Epic 72 (NPC Identity Hardening): once a Monster-Manual-authored NPC
(ADR-059 pre-generated, game-state-injected) lands in ``snapshot.npcs``,
nothing distinguishes it from a narrator-invented one. ``Npc.pool_origin``
only records the *pool member* an NPC was promoted from — it never says
"this came from the Monster Manual." This story carries one provenance
flag, end to end:

    NpcPatch.manual_origin  →  Npc.manual_origin

The marker is a **boolean** authorship predicate, modelled alongside the
existing ``pool_origin``. ``is_creature`` already distinguishes a Manual
*creature* from a Manual *human*; ``manual_origin`` answers the orthogonal
question "did the Manual author this NPC at all?" — which is binary.

These tests pin the data model (AC1/AC3) and the materialization builder
``GameSnapshot._npc_from_patch`` (AC2/E1) — the production leg
``monster_manual_inject`` drives before ``green_room.admit()`` (ADR-156).
The old ``_merge_npc_patch`` monotonicity tests (E2) were deleted with that
function and the ``WorldStatePatch.npcs_present`` lane it served (Green Room
follow-up, 2026-07-11): identity merges now happen in ``admit()``, whose
additive semantics are covered by the green-room suites. OTEL coverage lives
in ``tests/integration/test_npc_manual_origin_otel.py``.
"""

from __future__ import annotations

import json

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, NpcPatch


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
    """An unmarked patch defaults to the non-MM value.

    Existing patches and fixtures must round-trip unchanged, so the default
    cannot be ``True``."""
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
    """A Manual-marked patch materializes (``_npc_from_patch`` — the builder
    the production MM inject drives) an ``Npc`` whose provenance reads
    manual-origin."""
    snap = _snapshot()
    moth = snap._npc_from_patch(
        NpcPatch(name="Chalk Moth", threat_level=2, hp=6, manual_origin=True),
        emit_spawn_span=False,
    )
    assert moth.manual_origin is True


# ---------------------------------------------------------------------------
# E1 — unmarked patch must NOT get manual_origin (negative)
# ---------------------------------------------------------------------------


def test_unmarked_patch_materializes_non_manual_origin() -> None:
    """An ``NpcPatch`` with no marker set materializes an ``Npc`` whose
    provenance reads *not* manual-origin. Guards against an accidental
    default-true on the materializer."""
    snap = _snapshot()
    keep = snap._npc_from_patch(NpcPatch(name="Shopkeeper", role="merchant"), emit_spawn_span=False)
    assert keep.manual_origin is False
