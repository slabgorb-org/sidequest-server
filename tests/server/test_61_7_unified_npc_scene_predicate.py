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
* AC-2 — field resolution across ``current_room`` / ``location`` /
  ``last_seen_location``. Architect's initial recommendation was
  STRICT PRECEDENCE; Dev surveyed the model comments and pivoted to
  **union-of-structured + prose-fallback** (the Npc model defines
  ``current_room`` and ``location`` as ORTHOGONAL coordinate axes,
  not same-axis precedence competitors). The fixtures in this file
  pass identically under either semantic — they exercise the
  structured-overrides-prose anchor and the prose-fallback branch,
  neither of which distinguishes union from precedence. See the
  Design Deviation logged in ``sidequest/game/npc_scene.py:52-65``
  and ``.session/61-7-session.md`` §Design Deviations §Dev #1.
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

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import list_npcs_in_scene as _list_npcs_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
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
            hp=HpPool(current=4, max=4, base_max=4),
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
            hp=HpPool(current=10, max=10, base_max=10),
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
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
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


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: ToolContext.repository is a real PgSaveRepository)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _store_with(snapshot: GameSnapshot):
    """Build a real PgSaveRepository and persist the snapshot (ADR-115 F1)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug="npc-scene-predicate",
        mode="solo",
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
    )
    repo.init_session()
    repo.save(snapshot)
    return repo


def _tool_ctx(store, *, perspective_pc: str = "Alice") -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        repository=store,
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
    assert proj == tool, f"Projection/tool divergence: proj={sorted(proj)} tool={sorted(tool)}."


# ---------------------------------------------------------------------------
# AC-2 — field resolution across current_room / location / last_seen_location
#
# Implementation semantic (per ``sidequest/game/npc_scene.py`` Design
# Deviation block): UNION across the two structured fields
# (``current_room`` OR ``location``) plus a PROSE FALLBACK to
# ``last_seen_location`` only when BOTH structured fields are None.
# The architect's RED-phase recommendation was strict precedence
# (``current_room > location > last_seen_location``); Dev's write-path
# survey surfaced that ``current_room`` and ``location`` are ORTHOGONAL
# coordinate axes per the Npc model comments and pivoted to union+fallback.
# The fixtures in this section pass identically under either semantic —
# they exercise (a) each individual field driving a positive match and
# (b) the structured-overrides-stale-prose gaslighting-doctrine anchor.
# ---------------------------------------------------------------------------


async def test_unified_predicate_uses_current_room_when_set() -> None:
    """Union semantic: ``current_room`` matches the scene id, the NPC
    is in scene. ``location`` and ``last_seen_location`` set to a
    non-matching value do NOT contradict ``current_room`` (they live
    on different coordinate axes — chassis interior vs general-world
    vs prose-derived continuity hint).

    Fixture: NPC with ``current_room == "main_hall"`` (acting PC's room),
    ``location == "distant"``, ``last_seen_location == "distant"``. NPC
    IS in scene because ``current_room`` matches. Both call sites must
    agree.
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
        f"Union(current_room): divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}. ``current_room`` match must be honored "
        "by both call sites."
    )
    assert "AnchoredNpc" in proj, (
        "AnchoredNpc dropped despite ``current_room`` == acting PC's "
        "room. The union check (``current_room == scene or location == "
        "scene``) requires either structured field matching to anchor "
        "the NPC; ``location`` being set to a non-matching value does "
        "not override the matching ``current_room`` (orthogonal axes, "
        "not precedence competitors)."
    )


async def test_unified_predicate_falls_back_to_location_when_no_current_room() -> None:
    """Union semantic: ``location`` matches the scene id, ``current_room``
    is unset (None) — the union check still resolves positive. The
    prose fallback (``last_seen_location``) is NOT consulted because at
    least one structured field is set.

    Fixture: NPC with ``current_room == None``, ``location == "main_hall"``,
    ``last_seen_location == "distant"``. NPC IS in scene because
    ``location`` matches and the prose fallback is gated off (one
    structured field is set). Both call sites must agree.
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

    assert proj == tool, f"Union(location): divergence proj={sorted(proj)} tool={sorted(tool)}."
    assert "LocationOnlyNpc" in proj, (
        "LocationOnlyNpc dropped despite ``location`` == acting PC's "
        "room. The union check matches on ``location``; the prose "
        "fallback is gated off because ``location`` is set, so the "
        "stale ``last_seen_location`` value cannot disqualify the NPC."
    )


async def test_unified_predicate_falls_back_to_last_seen_when_no_structured_fields() -> None:
    """Prose fallback: when BOTH structured fields (``current_room`` and
    ``location``) are None, the predicate consults
    ``last_seen_location`` as a narrator-prose continuity hint. This
    is the case where the tool's pre-61-7 behavior (consult only
    structured fields) DROPS the NPC; 61-7 adds the prose fallback to
    the tool's resolution.

    Fixture: NPC with ``current_room == None``, ``location == None``,
    ``last_seen_location == "main_hall"``. Narrator-prose-declared NPC
    with no structured-state writes yet; ``last_seen_location`` is the
    only continuity signal. NPC IS in scene. Both call sites must agree.
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
        f"Prose-fallback: divergence proj={sorted(proj)} "
        f"tool={sorted(tool)}. Tool must consult last_seen_location when "
        "the structured fields are unset (the projection already does)."
    )
    assert "ProseOnlyNpc" in proj, (
        "ProseOnlyNpc dropped despite last_seen_location == acting PC's "
        "room with no structured-field overrides. Narrator-prose-declared "
        "NPCs with no current_room/location must still resolve in-scene."
    )


async def test_unified_predicate_current_room_overrides_stale_last_seen() -> None:
    """Gaslighting-doctrine anchor: when structured fields point AWAY
    from the scene id, the prose fallback is GATED OFF and the
    ``last_seen_location`` agreement with the scene cannot rescue
    the NPC. Structured-state writes are higher-trust than
    narrator-prose observations; a stale prose hint must not be fed
    to the narrator as present-scene ground truth.

    Fixture: NPC with ``current_room == "distant_chamber"``,
    ``location == "distant_chamber"``, ``last_seen_location == "main_hall"``.
    Both structured fields are set and disagree with the scene id; the
    union check returns False; the prose-fallback branch does NOT fire
    (gated by ``current_room is None AND location is None``). NPC is
    NOT in scene. Both call sites must agree.

    This is the 61-2 divergence-probe fixture inverted: pre-unification
    the projection (using ``last_seen_location`` only) kept the NPC
    and the tool (using ``current_room`` or ``location``) dropped it.
    Post-unification both drop it via the gaslighting-doctrine gate.
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
        "distant_chamber. The prose-fallback branch in ``is_npc_in_scene`` "
        "must be gated by ``current_room is None AND location is None`` "
        "so structured-state writes overrule stale narrator-prose hints. "
        "If this assertion fails the gate has regressed — the narrator "
        "would be fed a ghost in the present scene (gaslighting-doctrine "
        "violation)."
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
        actors=[EncounterActor(name=n, role="bystander", side="opponent") for n in actor_names],
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
    tool = await _tool_npc_names(snap)

    assert "EncounterParticipant" in proj, (
        "EncounterParticipant dropped from projection despite being "
        "named in the unresolved encounter's actor list. The 61-2 "
        "encounter-actor branch must be preserved through 61-7 "
        "unification — structured encounter participants are in-scene "
        "regardless of where their location fields point."
    )
    assert proj == tool, (
        f"Encounter-branch projection/tool divergence: proj={sorted(proj)} "
        f"tool={sorted(tool)}. AC-3 requires the encounter-actor branch "
        "to propagate to BOTH call sites — the per-call-site assertion "
        "is one half of the contract; this set-equality assertion is the "
        "other half (and the regression-guard against a tool-side "
        "encounter-branch breakage)."
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

    proj = _projection_npc_names(snap)
    tool = await _tool_npc_names(snap)

    assert "EncounterParticipant" in tool, (
        "EncounterParticipant absent from tool result despite being "
        "named in the unresolved encounter's actor list. 61-7 requires "
        "the encounter-actor branch be propagated to the tool path so "
        "both call sites reach identical verdicts. Today the tool only "
        "consults location/current_room — this assertion is the RED "
        "signal that the encounter branch needs to land on the tool side."
    )
    assert proj == tool, (
        f"Encounter-branch projection/tool divergence: proj={sorted(proj)} "
        f"tool={sorted(tool)}. Sibling cross-check to the projection-side "
        "encounter test — together they guard the full AC-3 contract."
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
        f"Resolved-encounter convergence: divergence proj={sorted(proj)} tool={sorted(tool)}."
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
        f"All-None convergence: divergence proj={sorted(proj)} tool={sorted(tool)}."
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
    # has a deterministic expected outcome under the union +
    # prose-fallback + encounter-branch contract:
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
        f"the union + prose-fallback + encounter contract; got "
        f"{sorted(proj)}. Out-of-scope: {sorted(expected_out_of_scene)}."
    )


# ---------------------------------------------------------------------------
# Round-2 RED (review-fix) — empty-string edge cases the simplify-pass missed
#
# Reviewer (edge-hunter) flagged that the TEA-verify simplify-pass
# dropped defensive ``if name:`` truthy guards under the observation
# that the type system guarantees ``npc.core`` non-None. Investigation
# during green-round-2 revealed the bug surface is UNREACHABLE through
# normal pydantic construction: ``CreatureCore`` has a
# ``name_non_blank`` field validator (``sidequest/game/creature_core.py:239``)
# that rejects empty names at construction time. The reviewer's
# edge-hunter agent did not surface this validator and assumed the
# type annotation ``name: str`` carried no runtime guarantee.
#
# Approach (logged as Design Deviation §Dev #2): rather than add
# belt-and-suspenders runtime guards that shadow the upstream invariant
# (and would constitute a silent fallback against malformed data the
# model already rejects), we pin the upstream invariant with a single
# regression-guard test below. If a future change relaxes the validator,
# the test fires loud and the runtime guards can be re-added at that
# time.
# ---------------------------------------------------------------------------


def test_upstream_creaturecore_validator_blocks_empty_npc_names() -> None:
    """Regression guard for the load-bearing model-layer invariant.

    The 61-7 predicate (``is_npc_in_scene`` / ``is_npc_anchored_by_encounter``)
    and the projection's set-add at ``session_helpers.py`` both rely on
    ``npc.core.name`` being non-empty. If this invariant is ever
    relaxed, the encounter-actor branch becomes vulnerable to
    false-positive matches against empty-named ``EncounterActor``
    entries (EncounterActor.name has no field validator), and the
    projection's ``in_scene_names`` set becomes vulnerable to silent
    identity collisions across empty-named NPCs.

    The validator lives at ``sidequest/game/creature_core.py:239`` —
    ``name_non_blank`` rejects empty or whitespace-only names.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        CreatureCore(
            name="",  # blank — must be rejected
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=4, max=4, base_max=4),
        )
    assert "name cannot be blank" in str(excinfo.value), (
        f"Expected ``name_non_blank`` validator to reject empty name with "
        f"'name cannot be blank', got: {excinfo.value}. If this fails, "
        "the upstream invariant the 61-7 predicate relies on has been "
        "weakened; the defensive ``if name:`` guards in "
        "``npc_scene.py:is_npc_anchored_by_encounter`` and "
        "``session_helpers.py:_apply_phase_c_projections`` should be "
        "re-added."
    )

    # Same guard for whitespace-only names (the validator uses .strip()).
    with pytest.raises(ValidationError):
        CreatureCore(
            name="   ",
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=4, max=4, base_max=4),
        )


async def test_tool_with_empty_scene_id_does_not_match_empty_string_locations() -> None:
    """Reviewer MUST-FIX (edge-hunter #2): when a narrator-supplied
    ``scene_id=""`` reaches the tool, it must NOT be treated as a
    literal scene id matching NPCs with empty-string location fields.
    Treat empty scene id as "no scene context" (per the tool's
    existing ``eff is None`` fallback intent).

    Today (post-61-7) ``_resolve_scene_id`` returns ``args.scene_id``
    directly when non-None, so ``eff=""`` is passed to
    ``is_npc_in_scene`` as ``current_room=""``. Inside the predicate,
    ``current_room is None`` is False (empty string is not None), so
    the no-scene-context early-return does NOT fire, and the union
    check ``cr == "" or loc == ""`` matches any NPC whose serialized
    location field is empty.

    Fix options (Dev picks):
    (a) Coerce empty ``scene_id`` to ``None`` inside ``_resolve_scene_id``
        — returns the omniscient/debug full roster (consistent with the
        tool's existing fallback intent).
    (b) Tighten the predicate's no-scene-context check from
        ``current_room is None`` to ``not current_room``.

    The reviewer recommended (a) for consistency with the tool's
    documented omniscient-fallback semantic.

    Fixture: two NPCs, one with all-empty location fields, one with
    ``current_room="main_hall"``. Tool called with ``scene_id=""``.
    Expected: full roster returned (both NPCs), NOT just the
    empty-location one.
    """
    npcs = [
        _npc("EmptyRoomNpc", current_room="", location="", last_seen_location=""),
        _npc("MainHallNpc", current_room="main_hall"),
    ]
    snap = _make_snapshot(npcs=npcs)
    store = _store_with(snap)
    ctx = _tool_ctx(store, perspective_pc="Alice")

    registered = default_registry._tools["list_npcs_in_scene"]
    args = registered.args_model.model_validate({"scene_id": ""})
    result = await registered.handler(args, ctx)
    assert result.status is ToolResultStatus.OK, (
        f"tool call failed: status={result.status} message={result.message!r}"
    )
    payload = cast(dict[str, Any], result.payload)
    names = {entry["name"] for entry in payload.get("npcs", [])}

    assert names == {"EmptyRoomNpc", "MainHallNpc"}, (
        f"Tool returned {sorted(names)} for scene_id=''. Expected the "
        "full roster (both NPCs). Empty scene_id must be treated as "
        "'no scene context' (omniscient fallback), not as a literal "
        "scene id matching empty-string location fields. See "
        "``sidequest/agents/tools/list_npcs_in_scene.py:_resolve_scene_id`` — "
        "coerce empty ``args.scene_id`` to None."
    )


# ---------------------------------------------------------------------------
# Round-2 RED (review-fix) — OTEL coverage for the new encounter branch
#
# Per server CLAUDE.md OTEL Observability Principle, every subsystem
# decision must emit watcher events so the GM panel can verify the fix
# is working. 61-7 propagates the encounter-actor branch to a NEW call
# site (the tool); both the projection's ``prompt.game_state.bytes`` span
# and the tool's ``tool.npcs.count`` need a companion attribute that
# distinguishes "kept by location match" from "kept by encounter
# override". Reviewer rule-checker flagged this as 2 high-confidence
# Rule 17 violations. Test-analyzer #6 independently flagged the same gap.
#
# Attribute name selected: ``encounter_anchored_count`` (projection
# side) and ``tool.npcs.encounter_anchored_count`` (tool side). Dev may
# choose alternate names; update tests in lockstep if so.
# ---------------------------------------------------------------------------


def test_projection_otel_carries_encounter_anchored_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``prompt.game_state.bytes`` span emitted by the projection
    must carry an ``encounter_anchored_count`` attribute distinct from
    ``npcs_dropped``. A GM-panel reader must be able to tell that the
    encounter-actor branch fired (and on how many NPCs).

    Fixture: 3 NPCs — one kept by location match (cr=main_hall), one
    kept by encounter override (all locations distant, named in
    unresolved encounter actors), one dropped (all locations distant,
    not in encounter). Expected attributes: ``npcs_dropped=1``,
    ``encounter_anchored_count=1``.
    """
    from sidequest.telemetry import spans as _spans

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    npcs = [
        _npc("LocationNpc", current_room="main_hall"),
        _npc(
            "EncounterNpc",
            current_room="distant_chamber",
            location="distant_chamber",
            last_seen_location="distant_chamber",
        ),
        _npc(
            "DroppedNpc",
            current_room="distant_chamber",
            location="distant_chamber",
            last_seen_location="distant_chamber",
        ),
    ]
    encounter = _make_encounter(["EncounterNpc"])
    snap = _make_snapshot(npcs=npcs, encounter=encounter)

    # Drive the projection
    _projection_npc_names(snap)

    matching = [s for s in exporter.get_finished_spans() if s.name == "prompt.game_state.bytes"]
    assert len(matching) == 1, (
        f"Expected exactly one prompt.game_state.bytes span; got {len(matching)}."
    )
    attrs = matching[0].attributes or {}
    got = attrs.get("encounter_anchored_count")
    assert got == 1, (
        f"prompt.game_state.bytes span missing or wrong attribute "
        f"'encounter_anchored_count': got {got!r}, expected 1. Per "
        "server CLAUDE.md OTEL Observability Principle, the GM panel "
        "must be able to verify the encounter-actor branch is firing "
        "on the projection side. ``npcs_dropped`` does not distinguish "
        "'kept by location match' from 'kept by encounter override'."
    )


async def test_tool_otel_carries_encounter_anchored_count() -> None:
    """The tool's ``list_npcs_in_scene`` OTEL span must carry an
    ``encounter_anchored_count`` attribute alongside the existing
    ``tool.npcs.count``. The tool's pre-61-7 contract was "Only
    tool.npcs.count is set per the plan spec," but 61-7 propagates the
    encounter branch to the tool, introducing a new decision the GM
    panel needs visibility into.

    Fixture: same shape as the projection OTEL test (1 location-match
    + 1 encounter-anchored + 1 dropped). Expected attribute on the
    tool's span: ``tool.npcs.encounter_anchored_count=1``.
    """
    npcs = [
        _npc("LocationNpc", current_room="main_hall"),
        _npc(
            "EncounterNpc",
            current_room="distant_chamber",
            location="distant_chamber",
            last_seen_location="distant_chamber",
        ),
        _npc(
            "DroppedNpc",
            current_room="distant_chamber",
            location="distant_chamber",
            last_seen_location="distant_chamber",
        ),
    ]
    encounter = _make_encounter(["EncounterNpc"])
    snap = _make_snapshot(npcs=npcs, encounter=encounter)

    # Use a real span mock the tool can call set_attribute on
    span_attrs: dict[str, object] = {}

    class _CapturingSpan:
        def set_attribute(self, key: str, value: object) -> None:
            span_attrs[key] = value

    store = _store_with(snap)
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Alice",
        turn_number=1,
        repository=store,
        otel_span=_CapturingSpan(),  # type: ignore[arg-type]
        perception_filter=NarratorPerceptionFilter(),
    )

    registered = default_registry._tools["list_npcs_in_scene"]
    args = registered.args_model.model_validate({})
    result = await registered.handler(args, ctx)
    assert result.status is ToolResultStatus.OK

    got = span_attrs.get("tool.npcs.encounter_anchored_count")
    assert got == 1, (
        f"Tool OTEL span missing or wrong attribute "
        f"'tool.npcs.encounter_anchored_count': got {got!r}, expected 1. "
        "Per server CLAUDE.md OTEL Observability Principle, the new "
        "encounter-branch propagation to the tool path (61-7 AC-3) "
        "requires per-decision visibility on the tool side too."
    )
