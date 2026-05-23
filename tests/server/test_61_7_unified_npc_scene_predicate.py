"""Story 61-7 RED — unify the npc-in-scene predicate across both call sites.

The 2026-05-23 cost-runaway-fix (epic 61) introduced a per-turn projection
predicate ``_npc_in_scene`` in ``sidequest/server/session_helpers.py``
that consults ``Npc.last_seen_location`` only, plus an unresolved-encounter
actor-membership branch. In parallel, the existing
``list_npcs_in_scene`` tool at ``sidequest/agents/tools/list_npcs_in_scene.py``
matches ``Npc.current_room`` or ``Npc.location``. Three NPC location
fields signal scene membership; the two call sites consult disjoint
subsets and therefore reach divergent verdicts on legitimate fixtures.

The 61-2 verify-phase added an adversarial probe
(``test_npc_in_scene_predicate_divergence_from_list_npcs_in_scene_tool``
in ``test_61_2_snapshot_seven_field_projection.py``) that measured the
divergence as a tripwire — the probe's docstring explicitly says
"if 61-7 later unifies the predicates and the projection changes
semantics, this test will surface the change." Per architect spec
that tripwire stays in place; 61-7 lands a unified predicate, the
tripwire flips state, and Dev updates it in lockstep during GREEN.

This file drives the **convergence contract** that 61-7 must deliver:
both production call sites — the snapshot projection AND the tool —
must reach the same scene-membership verdict on every NPC. The tests
exercise the real call paths (``_build_turn_context`` for the
projection, the default ``ToolRegistry`` dispatch for the tool) — no
source-text wiring assertions (server CLAUDE.md "No Source-Text
Wiring Tests").

Tested AC coverage (per ``sprint/context/context-story-61-7.md``):

* AC-1 / AC-6 — single named predicate, both call sites consume it
  (verified by behavior convergence across the real call paths).
* AC-2 — field-precedence order ``current_room > location >
  last_seen_location`` (architect recommendation, subject to red-phase
  verification; if Dev pivots to union semantics this file's
  precedence tests get rewritten via Design Deviation).
* AC-3 — encounter-actor membership branch preserved AND propagated
  to the tool path (the tool currently ignores encounters; 61-7 must
  add the branch).
* AC-4 — divergence-probe stays in 61-2 as the architect-designed
  tripwire; 61-7 GREEN flips it. This file adds the post-fix
  convergence guard the probe morphs into.

The tests are RED until Dev (a) extracts a single named predicate,
(b) updates both call sites to delegate to it, and (c) implements
the precedence + encounter-branch contract this file asserts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import list_npcs_in_scene as _list_npcs_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot, Npc, RoomState
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _npc(
    name: str,
    *,
    current_room: str | None = None,
    location: str | None = None,
    last_seen_location: str | None = None,
) -> Npc:
    """Construct an NPC with the three location fields independently settable.

    Default all-None values let each test set only the fields it cares
    about, mirroring real fixture shapes (narrator-declared NPCs often
    have just `last_seen_location`; structured-state NPCs often have
    `current_room` or `location`).
    """
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            edge=EdgePool(current=4, max=4, base_max=4),
        ),
        current_room=current_room,
        location=location,
        last_seen_location=last_seen_location,
    )


def _character(name: str, *, current_room: str | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            edge=EdgePool(current=10, max=10, base_max=10),
        ),
        backstory="hero",
        char_class="Delver",
        race="Human",
        current_room=current_room,
    )


def _make_snapshot(
    *,
    acting_pc: str = "Alice",
    pc_room: str = "main_hall",
    npcs: list[Npc] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    """Build a minimal snapshot with one acting PC and the supplied NPCs.

    The acting PC's `current_room` AND the snapshot's
    `character_locations` mapping are BOTH set to `pc_room` so that:

    * `list_npcs_in_scene._resolve_scene_id` (which consults
      `pc.current_room`) and
    * `session_helpers._build_turn_context` (which consults
      `snapshot.party_location(perspective=...)`, reading
      `character_locations`)

    resolve to the same scene id. Without this alignment, the two
    call sites would disagree by fixture artifact, not by the
    predicate-under-test.
    """
    pc = _character(acting_pc, current_room=pc_room)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=10),
        characters=[pc],
        npcs=list(npcs or []),
        room_states={pc_room: RoomState(room_id=pc_room)},
        atmosphere="dim",
        current_region="upper_caverns",
        encounter=encounter,
    )
    snap.character_locations[acting_pc] = pc_room
    snap.player_seats[f"player:{acting_pc.lower()}"] = acting_pc
    return snap


# ---------------------------------------------------------------------------
# Projection-side verdict (drives session_helpers._build_turn_context)
# ---------------------------------------------------------------------------


def _projection_npc_names(snap: GameSnapshot, *, player_name: str = "Alice") -> set[str]:
    """Drive the production projection and return the set of npc names
    that survived `_apply_phase_c_projections`.

    Reads `state_summary["npcs"]` and extracts the canonical name
    (handles both the nested-core and flat-name dump shapes — the
    61-2 projection assigns dicts back, but pydantic's default dump
    shape nests `core.name`).
    """
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snap.genre_slug)
    sd = _SessionData(
        genre_slug=snap.genre_slug,
        world_slug=snap.world_slug,
        player_name=player_name,
        player_id=f"player:{player_name.lower()}",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd._room = room_for(snap, slug=snap.world_slug)
    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.state_summary is not None, (
        "state_summary missing from TurnContext — fixture set-up broke "
        "before the unified-predicate change could be exercised."
    )
    payload = json.loads(ctx.state_summary)
    names: set[str] = set()
    for entry in payload.get("npcs") or []:
        core = entry.get("core")
        name = core.get("name", "") if isinstance(core, dict) else entry.get("name", "")
        if name:
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Tool-side verdict (drives list_npcs_in_scene through the registry)
# ---------------------------------------------------------------------------


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _tool_ctx(store: SqliteStore, *, perspective_pc: str = "Alice") -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _tool_npc_names(snap: GameSnapshot, *, perspective_pc: str = "Alice") -> set[str]:
    """Drive the production tool and return the set of npc names returned.

    Uses `scene_id=None` so the tool's perspective-fallback resolves to
    the PC's `current_room` — mirroring the projection's
    "acting-PC's-room" question.
    """
    store = _store_with(snap)
    ctx = _tool_ctx(store, perspective_pc=perspective_pc)
    registered = default_registry._tools["list_npcs_in_scene"]
    args = registered.args_model.model_validate({})
    result = cast(ToolResult, await registered.handler(args, ctx))
    assert result.status is ToolResultStatus.OK, (
        f"tool call failed: status={result.status} message={result.message!r}"
    )
    payload = cast(dict[str, Any], result.payload)
    return {entry["name"] for entry in payload.get("npcs", [])}


# ---------------------------------------------------------------------------
# AC-1 / AC-6 — convergence on simple in-scene / off-stage fixtures
# ---------------------------------------------------------------------------


async def test_projection_and_tool_converge_on_in_scene_npc() -> None:
    """Baseline convergence: an NPC structurally at the acting PC's room
    must be kept by both call sites.

    Fixture: NPC with `current_room == "main_hall"`, acting PC at
    `main_hall`. Both production code paths must include the NPC.

    Today: passes on the tool side (current_room matches) but fails
    on the projection side (projection consults last_seen_location
    only — which is None). After 61-7 unification both paths agree.
    """
    npc = _npc("OnSceneNpc", current_room="main_hall")
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert "OnSceneNpc" in proj, (
        "Projection dropped OnSceneNpc despite current_room == acting "
        "PC's room. The unified predicate must use `current_room` as a "
        "scene-membership signal, not just `last_seen_location`."
    )
    assert "OnSceneNpc" in tool, (
        "Tool dropped OnSceneNpc despite current_room match. Pre-existing "
        "tool behavior should still include it after unification."
    )
    assert proj == tool, (
        f"Projection/tool divergence: proj={sorted(proj)} tool={sorted(tool)}. "
        "Unified predicate contract violated — both call sites must reach "
        "the same scene-membership verdict on every NPC."
    )


async def test_projection_and_tool_converge_on_off_stage_npc() -> None:
    """Baseline convergence: an NPC structurally elsewhere must be
    dropped by both call sites.

    Fixture: NPC with `current_room == "distant_chamber"`, acting PC at
    `main_hall`. Both production code paths must drop the NPC.
    """
    npc = _npc("OffStageNpc", current_room="distant_chamber")
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert "OffStageNpc" not in proj, (
        "Projection kept OffStageNpc despite current_room mismatch. The "
        "unified predicate must drop NPCs whose authoritative location "
        "field disagrees with the acting PC's room."
    )
    assert "OffStageNpc" not in tool, (
        "Tool kept OffStageNpc despite current_room mismatch (tool's "
        "existing contract). Convergence requires both paths drop it."
    )
    assert proj == tool, (
        f"Projection/tool divergence: proj={sorted(proj)} tool={sorted(tool)}."
    )


# ---------------------------------------------------------------------------
# AC-2 — field-precedence order (current_room > location > last_seen_location)
# ---------------------------------------------------------------------------


async def test_unified_predicate_uses_current_room_when_set() -> None:
    """Precedence: `current_room` wins over `location` and
    `last_seen_location` when set.

    Fixture: NPC with `current_room == "main_hall"` (acting PC's room),
    `location == "distant"`, `last_seen_location == "distant"`. Under
    the precedence contract, `current_room` is authoritative — the NPC
    IS in scene. Both call sites must agree.
    """
    npc = _npc(
        "AnchoredNpc",
        current_room="main_hall",
        location="distant",
        last_seen_location="distant",
    )
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Precedence-current_room: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}. current_room must be authoritative."
    )
    assert "AnchoredNpc" in proj, (
        "AnchoredNpc dropped despite current_room == acting PC's room. "
        "Precedence contract requires current_room win over conflicting "
        "location / last_seen_location values."
    )


async def test_unified_predicate_falls_back_to_location_when_no_current_room() -> None:
    """Precedence: `location` wins when `current_room` is None.

    Fixture: NPC with `current_room == None`, `location == "main_hall"`,
    `last_seen_location == "distant"`. `location` is the next-most-trusted
    signal; NPC IS in scene. Both call sites must agree.
    """
    npc = _npc(
        "LocationOnlyNpc",
        current_room=None,
        location="main_hall",
        last_seen_location="distant",
    )
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Precedence-location-fallback: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}."
    )
    assert "LocationOnlyNpc" in proj, (
        "LocationOnlyNpc dropped despite location == acting PC's room "
        "and no current_room override. Precedence contract requires "
        "location win over conflicting last_seen_location."
    )


async def test_unified_predicate_falls_back_to_last_seen_when_no_structured_fields() -> None:
    """Precedence: `last_seen_location` is the final fallback when both
    structured fields are None.

    Fixture: NPC with `current_room == None`, `location == None`,
    `last_seen_location == "main_hall"`. Narrator-prose-declared NPC with
    no structured-state writes yet; `last_seen_location` is the only
    continuity signal. NPC IS in scene. Both call sites must agree.

    This is the case where the tool's pre-61-7 behavior (consult only
    `current_room` / `location`) DROPS the NPC; 61-7 unification adds
    `last_seen_location` to the tool's resolution.
    """
    npc = _npc(
        "ProseOnlyNpc",
        current_room=None,
        location=None,
        last_seen_location="main_hall",
    )
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Precedence-last_seen-fallback: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}. Tool must consult last_seen_location when "
        "the structured fields are unset (the projection already does)."
    )
    assert "ProseOnlyNpc" in proj, (
        "ProseOnlyNpc dropped despite last_seen_location == acting PC's "
        "room with no structured-field overrides. Narrator-prose-declared "
        "NPCs with no current_room/location must still resolve in-scene."
    )


async def test_unified_predicate_current_room_overrides_stale_last_seen() -> None:
    """Precedence: current_room is authoritative even when last_seen_location
    agrees with the acting PC's room (the inverse of the 61-2 divergence
    probe — same NPC shape, opposite expected outcome under unification).

    Fixture: NPC with `current_room == "distant_chamber"`,
    `location == "distant_chamber"`, `last_seen_location == "main_hall"`.
    Structured-state writes are higher-trust than narrator-prose
    observations; the NPC has structurally moved to a distant chamber
    and the narrator-prose hint is stale. NPC is NOT in scene. Both
    call sites must agree.

    This is the 61-2 divergence-probe fixture inverted: today the
    projection (using last_seen_location) keeps the NPC and the tool
    (using current_room / location) drops it. Post-unification both
    drop it (current_room precedence wins).
    """
    npc = _npc(
        "StaleProseNpc",
        current_room="distant_chamber",
        location="distant_chamber",
        last_seen_location="main_hall",
    )
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Stale-prose convergence: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}. The structured-vs-prose disagreement is "
        "exactly the divergence 61-7 exists to close."
    )
    assert "StaleProseNpc" not in proj, (
        "StaleProseNpc kept despite current_room/location pointing to "
        "distant_chamber. Precedence contract requires structured-state "
        "writes (current_room) to override stale narrator-prose hints "
        "(last_seen_location). If this assertion fails post-fix, Dev "
        "likely chose union semantics — raise as a Design Deviation."
    )


# ---------------------------------------------------------------------------
# AC-3 — encounter-actor membership branch preserved AND propagated to tool
# ---------------------------------------------------------------------------


def _make_encounter(actor_names: list[str]) -> StructuredEncounter:
    """Minimal unresolved encounter with the named actors on the opponent side.

    The exact metric thresholds and encounter_type don't matter for the
    scene-membership branch — only `resolved=False` and the `actors`
    list contents are read.
    """
    return StructuredEncounter(
        encounter_type="social",
        player_metric=EncounterMetric(name="poise", threshold=10),
        opponent_metric=EncounterMetric(name="suspicion", threshold=10),
        actors=[
            EncounterActor(name=n, role="bystander", side="opponent")
            for n in actor_names
        ],
        resolved=False,
    )


async def test_unified_predicate_preserves_encounter_actor_branch_in_projection() -> None:
    """An NPC named in an unresolved encounter's actors list is in-scene
    even when all three location fields point elsewhere.

    Fixture: NPC with all three location fields == "distant_chamber",
    named in the unresolved encounter's actor list. Projection must
    keep the NPC (encounter-actor branch overrides location mismatch).
    The 61-2 predicate already carries this branch; 61-7 must preserve
    it.
    """
    npc = _npc(
        "EncounterParticipant",
        current_room="distant_chamber",
        location="distant_chamber",
        last_seen_location="distant_chamber",
    )
    encounter = _make_encounter(["EncounterParticipant"])
    snap = _make_snapshot(npcs=[npc], encounter=encounter)

    proj = _projection_npc_names(snap)

    assert "EncounterParticipant" in proj, (
        "EncounterParticipant dropped from projection despite being "
        "named in the unresolved encounter's actor list. The 61-2 "
        "encounter-actor branch must be preserved through 61-7 "
        "unification — structured encounter participants are in-scene "
        "regardless of where their location fields point."
    )


async def test_unified_predicate_propagates_encounter_branch_to_tool() -> None:
    """The encounter-actor branch must be added to the tool side under
    unification — the tool currently has NO encounter awareness.

    Fixture: same as the projection encounter test — NPC at
    distant_chamber by all three location fields, named in an
    unresolved encounter's actors. Tool must include the NPC after
    61-7 unification (today it does not — the tool only consults
    location / current_room).

    This is the AC-3 propagation requirement: the unified predicate
    behaves identically at both call sites, including the encounter
    branch.
    """
    npc = _npc(
        "EncounterParticipant",
        current_room="distant_chamber",
        location="distant_chamber",
        last_seen_location="distant_chamber",
    )
    encounter = _make_encounter(["EncounterParticipant"])
    snap = _make_snapshot(npcs=[npc], encounter=encounter)

    tool = await _tool_npc_names(snap)

    assert "EncounterParticipant" in tool, (
        "EncounterParticipant absent from tool result despite being "
        "named in the unresolved encounter's actor list. 61-7 requires "
        "the encounter-actor branch be propagated to the tool path so "
        "both call sites reach identical verdicts. Today the tool only "
        "consults location/current_room — this assertion is the RED "
        "signal that the encounter branch needs to land on the tool side."
    )


async def test_resolved_encounter_does_not_anchor_npc_as_in_scene() -> None:
    """Only UNRESOLVED encounters anchor NPCs as in-scene via the
    actor-membership branch. A resolved encounter (`resolved=True`)
    means the encounter is over; the actors are no longer present-by-
    participation. Standard location resolution applies.

    Fixture: NPC at distant_chamber by all three location fields, named
    in a RESOLVED encounter's actors. Both call sites must DROP the
    NPC. Guard against an over-eager encounter branch that ignores the
    resolved flag.
    """
    npc = _npc(
        "ResolvedEncounterActor",
        current_room="distant_chamber",
        location="distant_chamber",
        last_seen_location="distant_chamber",
    )
    encounter = _make_encounter(["ResolvedEncounterActor"])
    encounter.resolved = True
    snap = _make_snapshot(npcs=[npc], encounter=encounter)

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Resolved-encounter convergence: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}."
    )
    assert "ResolvedEncounterActor" not in proj, (
        "ResolvedEncounterActor kept despite (a) all location fields "
        "pointing elsewhere and (b) the encounter being marked "
        "resolved. The encounter-actor branch must respect the "
        "`resolved` flag — completed encounters do not anchor NPCs "
        "as in-scene."
    )


# ---------------------------------------------------------------------------
# Edge cases — all-None fields, multiple NPCs, set equality
# ---------------------------------------------------------------------------


async def test_unified_predicate_drops_npc_with_all_none_location_fields() -> None:
    """An NPC with no location signals AND no encounter membership is
    NOT in scene. Convergence on the degenerate case.

    Without this assertion, a unified predicate that defaults to True
    when all fields are None would silently include un-placed NPCs in
    every scene — exactly the gaslighting failure mode the projection
    exists to prevent.
    """
    npc = _npc("UnplacedNpc")  # all three location fields == None
    snap = _make_snapshot(npcs=[npc])

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"All-None convergence: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}."
    )
    assert "UnplacedNpc" not in proj, (
        "UnplacedNpc kept despite all three location fields being None "
        "and no encounter membership. The unified predicate must drop "
        "NPCs with no scene-membership signal — defaulting to in-scene "
        "is a gaslighting-doctrine violation (the narrator would see an "
        "NPC that has no positional truth)."
    )


async def test_unified_predicate_converges_on_mixed_roster() -> None:
    """End-to-end wiring test: a mixed NPC roster with every precedence
    case, encounter participation, and the degenerate all-None NPC.
    Both call sites must produce IDENTICAL sets.

    This is the AC-6 wiring assertion — the test drives the real
    production call paths for both sites (no source-text wiring) and
    asserts set equality across the full roster. If a future code
    change desyncs the two predicates, this single test catches it.
    """
    npcs = [
        _npc("ByCurrentRoom", current_room="main_hall"),
        _npc("ByLocation", location="main_hall"),
        _npc("ByLastSeen", last_seen_location="main_hall"),
        _npc(
            "StructuredOverridesProse",
            current_room="distant_chamber",
            last_seen_location="main_hall",
        ),
        _npc(
            "AllElsewhere",
            current_room="distant_chamber",
            location="distant_chamber",
            last_seen_location="distant_chamber",
        ),
        _npc(
            "EncounterAnchor",
            current_room="distant_chamber",
            location="distant_chamber",
            last_seen_location="distant_chamber",
        ),
        _npc("Unplaced"),
    ]
    encounter = _make_encounter(["EncounterAnchor"])
    snap = _make_snapshot(npcs=npcs, encounter=encounter)

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert proj == tool, (
        f"Mixed-roster convergence (AC-6 wiring): divergence\n"
        f"  projection: {sorted(proj)}\n"
        f"  tool:       {sorted(tool)}\n"
        "Every NPC must receive the same verdict from both call sites."
    )

    # Sanity bounds on the expected verdict so a future regression that
    # silently agrees on the WRONG set still surfaces. Each named NPC
    # has a deterministic expected outcome under the precedence +
    # encounter-branch contract:
    expected_in_scene = {
        "ByCurrentRoom",
        "ByLocation",
        "ByLastSeen",
        "EncounterAnchor",
    }
    expected_out_of_scene = {
        "StructuredOverridesProse",
        "AllElsewhere",
        "Unplaced",
    }
    assert proj == expected_in_scene, (
        f"Expected exactly {sorted(expected_in_scene)} in scene under "
        f"the precedence + encounter contract; got {sorted(proj)}. "
        f"Out-of-scope: {sorted(expected_out_of_scene)}."
    )
