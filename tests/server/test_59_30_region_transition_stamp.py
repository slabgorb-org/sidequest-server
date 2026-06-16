"""RED tests — Story 59-30: ``RegionTransition`` provenance stamp sites.

The ``movement`` engagement witness (see
``tests/agents/test_59_30_witnesses.py``) reads a NEW turn-stamped per-PC
relocation artifact, ``GameSnapshot.region_transitions`` (a list of
``RegionTransition``), mirroring the political ``BeliefLedgerEntry`` ledger. For
the witness to be correct on EVERY world type, the stamp must be written on
**both** relocation seams (there is no single choke point — region-mode worlds
bypass ``apply_world_patch`` entirely):

  - **Site A — ``GameSnapshot.apply_world_patch`` (session.py)**, inside the
    ``patch.pc_region`` genuine-change block (``to_region and to_region != prev``,
    beside ``notify_region_transition``). ``via="world_patch"``. This covers
    movement.py's two procedural relocation paths (surface-descent entrance-bind
    and the normal resolve) — both route through
    ``apply_world_patch(WorldStatePatch(pc_region=...))``.
  - **Site B — ``narration_apply`` region-mode block**, where oz/wonderland/
    gulliver advance ``current_region`` + ``pc_regions[player_name]`` from a
    resolved cartography heading. ``via="narration_apply"``. MANDATORY: region-
    mode worlds relocate here, NOT through ``apply_world_patch`` — without Site B
    the witness false-negatives on every region-mode move (the
    ``region_mode_deferred`` movement path applies no patch by design).

The Architect note also forbids stamping the ``current_region`` party-anchor
branch (spawn/teleport) — that is a party-level anchor, not a per-PC movement
engagement; stamping it would pollute the ledger.

Contract source of truth: "Architect Technical Note — MOVEMENT WITNESS CONTRACT"
in ``.session/59-30-session.md`` (file refs verified 2026-06-04). These tests are
fixture-driven behavior tests (CLAUDE.md "No Source-Text Wiring Tests"): they
drive the real relocation seams and assert on the resulting snapshot artifact.

Site-B fixtures mirror the existing
``tests/server/test_region_advance_on_location.py`` (the region-mode
``current_region`` advance these stamps ride alongside).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.session import GameSnapshot, WorldStatePatch
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# RegionTransition import shim (new model in 59-30; home left open by the
# Architect note — prefer the GameSnapshot module surface, try the dedicated
# module as fallback). Fails RED until Dev creates it.
# ---------------------------------------------------------------------------


def _region_transition_cls() -> Any:
    try:
        from sidequest.game.session import RegionTransition

        return RegionTransition
    except ImportError:
        from sidequest.game.region_transition import RegionTransition

        return RegionTransition


def _transitions(snap: GameSnapshot) -> list[Any]:
    """The region_transitions list (field added by Dev in GREEN)."""
    return list(snap.region_transitions)


# ===========================================================================
# Site A — apply_world_patch(pc_region=...) stamps via="world_patch"
# ===========================================================================


def test_pc_region_patch_genuine_change_stamps_region_transition() -> None:
    """A genuine per-PC region change via ``apply_world_patch`` records a
    turn-stamped ``RegionTransition(via="world_patch")`` — the artifact the
    movement witness reads on the procedural relocation paths."""
    rt_cls = _region_transition_cls()

    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=7),
    )
    snap.pc_regions["Rux"] = "region_a"

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "region_b"}))

    stamps = [t for t in _transitions(snap) if isinstance(t, rt_cls)]
    assert len(stamps) == 1, f"expected exactly one stamp, got {_transitions(snap)!r}"
    t = stamps[0]
    assert t.turn == 7
    assert t.pc_name == "Rux"
    assert t.from_region == "region_a"
    assert t.to_region == "region_b"
    assert t.via == "world_patch"


def test_pc_region_patch_noop_does_not_stamp() -> None:
    """Patching a PC to the region it is ALREADY in is not a relocation — no
    stamp (mirrors the ``to_region != prev`` genuine-change gate; avoids
    polluting the ledger with no-op churn)."""
    _region_transition_cls()  # force RED on the missing model

    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=7),
    )
    snap.pc_regions["Rux"] = "region_a"

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "region_a"}))

    assert _transitions(snap) == [], "a no-op region patch must not stamp a transition"


def test_current_region_party_anchor_patch_does_not_stamp() -> None:
    """The ``current_region`` party-anchor branch (spawn/teleport) must NOT stamp
    a per-PC ``RegionTransition`` — the Architect note forbids it (party-level
    anchor, not a per-PC movement engagement; stamping pollutes the ledger)."""
    _region_transition_cls()  # force RED on the missing model

    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=7),
        player_seats={"p1": "Rux"},
    )

    snap.apply_world_patch(WorldStatePatch(current_region="spawn_zone"))

    assert _transitions(snap) == [], (
        "the current_region party-anchor branch must not stamp a per-PC transition"
    )


# ===========================================================================
# Site B — narration_apply region-mode advance stamps via="narration_apply"
# ===========================================================================


def _oz_regions() -> dict[str, Region]:
    return {
        "munchkin_country": Region(
            name="The Munchkin Country",
            summary="The blue East.",
            description="The blue East.",
            adjacent=["the_yellow_brick_road", "the_emerald_city"],
        ),
        "the_yellow_brick_road": Region(
            name="The Yellow Brick Road",
            summary="The gold road.",
            description="The gold road.",
            adjacent=["munchkin_country", "the_emerald_city"],
        ),
        "the_emerald_city": Region(
            name="The Emerald City",
            summary="The green hub.",
            description="The green hub.",
            adjacent=["the_yellow_brick_road"],
        ),
    }


def _region_mode_pack(pack, *, mode: NavigationMode = NavigationMode.region):
    """Attach a synthetic cartography world keyed "oz" onto the (mock) pack."""
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=mode, regions=_oz_regions())
    )
    pack.worlds = {"oz": world_obj}
    return pack


def test_region_mode_location_change_stamps_region_transition(
    snapshot_with_pack,
    character_named_sam,
) -> None:
    """A region-mode relocation (narrator heading resolves to a different known
    cartography region) records a ``RegionTransition(via="narration_apply")`` for
    the acting PC at the current turn. This is the seam the witness MUST be able
    to read or oz/wonderland/gulliver moves all false-flag."""
    rt_cls = _region_transition_cls()

    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.turn_manager.interaction = 7
    snap.current_region = "munchkin_country"
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)

    result = NarrationTurnResult(
        narration="Susan follows the gold road to the green city's gates.",
        location="The Emerald City — The Green Street",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    # Sanity: the existing region advance still happened (Site B rides it).
    assert snap.current_region == "the_emerald_city"

    stamps = [t for t in _transitions(snap) if isinstance(t, rt_cls)]
    assert len(stamps) == 1, f"expected one narration_apply stamp, got {_transitions(snap)!r}"
    t = stamps[0]
    assert t.turn == 7
    assert t.pc_name == "Susan"
    assert t.from_region == "munchkin_country"
    assert t.to_region == "the_emerald_city"
    assert t.via == "narration_apply"


def test_region_mode_unchanged_heading_does_not_stamp(
    snapshot_with_pack,
    character_named_sam,
) -> None:
    """A heading resolving to the SAME region the PC is already in is not a
    relocation → no stamp (idempotent; no spurious ledger churn)."""
    _region_transition_cls()  # force RED on the missing model

    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.turn_manager.interaction = 7
    snap.current_region = "the_emerald_city"
    snap.characters.append(character_named_sam)

    result = NarrationTurnResult(
        narration="Susan crosses the plaza inside the city.",
        location="The Emerald City — The Palace Steps",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    assert _transitions(snap) == [], "an unchanged-region heading must not stamp a transition"
