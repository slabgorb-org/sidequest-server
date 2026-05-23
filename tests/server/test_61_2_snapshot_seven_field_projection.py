"""Story 61-2 RED — extend snapshot slim to the seven growing fields.

ADR-110 Phase B (story 57-5) shipped a four-field DROP list at
``session_helpers.py:64`` — ``active_tropes``, ``axis_values``,
``genie_wishes``, ``achievement_tracker``. **None of those four grow.**

The 2026-05-23 cost-runaway (~$313/48h) traced to ``snapshot.model_dump()``
at ``session_helpers.py:559`` flowing into the Valley/Recency
``system_blocks`` uncached (``cache=False`` at
``orchestrator.py:3437-3441``), multiplied by up to 8 tool-loop iterations
per turn. The fields the cost actually rides on grow monotonically with
session length.

Per-field validation against live code (full audit in
``sprint/context/context-story-61-2.md``) yields:

* **Top-level on ``GameSnapshot`` AND growing:** ``room_states``, ``npcs``.
* **Nested AND growing:** ``characters[*].known_facts``,
  ``npcs[*].belief_state``, ``scenario_state.discovered_clues``.
* **NOT snapshot fields (regression guards only):** ``journal``,
  ``footnotes``, ``location_descriptions``. These are real growing
  subsystems but ride out-of-band (event log / per-turn NarrationResult
  / LOCATION_DESCRIPTION WebSocket message respectively). They cannot
  drive snapshot-dump cost today, but a future addition WITHOUT a
  drop-list update is precisely the regression ADR-110 §Implementation
  Notes anticipates and 61-5 will enforce by test.

These tests are RED until Dev applies the per-field projections at
``session_helpers.py:559+`` and updates ``_PHASE_B_DROP_FIELDS`` /
adds projection helpers. The tests drive the production code path
(``_build_turn_context``) — no source-text greps (per
``sidequest-server/CLAUDE.md`` "No Source-Text Wiring Tests").
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.belief_state import (
    BeliefFact,
    BeliefSourceWitnessed,
    BeliefState,
)
from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc, RoomState
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixture helpers — mirror tests/server/test_57_5_snapshot_slimming.py
# ---------------------------------------------------------------------------


def _character(
    name: str,
    *,
    known_facts: list[KnownFact] | None = None,
) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            edge=EdgePool(current=8, max=10, base_max=10),
        ),
        backstory="hero",
        char_class="Delver",
        race="Human",
        known_facts=known_facts or [],
    )


def _npc(
    name: str,
    *,
    last_seen_location: str | None = None,
    belief_state: BeliefState | None = None,
) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="grizzled veteran",
            personality="dour",
            inventory=Inventory(),
            edge=EdgePool(current=5, max=5, base_max=5),
        ),
        last_seen_location=last_seen_location,
        belief_state=belief_state or BeliefState(),
    )


def _populated_belief_state(subject: str, contents: list[str]) -> BeliefState:
    """Build a non-trivial BeliefState for the dump-omission assertion."""
    return BeliefState(
        beliefs=[
            BeliefFact(
                subject=subject,
                content=c,
                turn_learned=i + 1,
                source=BeliefSourceWitnessed(),
            )
            for i, c in enumerate(contents)
        ],
    )


def _make_snapshot(
    *,
    rooms: int = 5,
    npcs_in_scene: int = 2,
    npcs_off_stage: int = 3,
    known_facts_per_pc: int = 25,
    clues: int = 30,
) -> GameSnapshot:
    """Build a populated late-session snapshot for the projection tests.

    Default counts mirror AC10 (5 rooms, 5 NPCs total, 25 facts per PC,
    30 clues) — the byte-reduction baseline.
    """
    acting_pc = "Alice"
    in_scene_location = "main_hall"

    # PC with N known_facts so the tail-K projection has something to truncate.
    known = [
        KnownFact(content=f"fact #{i}", learned_turn=i + 1)
        for i in range(known_facts_per_pc)
    ]
    pcs = [_character(acting_pc, known_facts=known)]

    # NPC roster: ``npcs_in_scene`` at the acting PC's location, the rest
    # off-stage. Every NPC carries a populated BeliefState so the dump
    # omission test has something to omit.
    npc_list: list[Npc] = []
    pool: list[NpcPoolMember] = []
    for i in range(npcs_in_scene):
        name = f"InScene_{i + 1}"
        npc_list.append(
            _npc(
                name,
                last_seen_location=in_scene_location,
                belief_state=_populated_belief_state(
                    "victim",
                    [f"in-scene-belief-{i}-{j}" for j in range(4)],
                ),
            )
        )
        pool.append(
            NpcPoolMember(
                name=name,
                role="bystander",
                pronouns="they/them",
                drawn_from="world_authored",
            )
        )
    for i in range(npcs_off_stage):
        name = f"OffStage_{i + 1}"
        npc_list.append(
            _npc(
                name,
                last_seen_location=f"distant_room_{i + 1}",
                belief_state=_populated_belief_state(
                    "victim",
                    [f"off-stage-belief-{i}-{j}" for j in range(4)],
                ),
            )
        )
        pool.append(
            NpcPoolMember(
                name=name,
                role="cast",
                pronouns="they/them",
                drawn_from="world_authored",
            )
        )

    # room_states: ``rooms`` rooms total, only one matches the acting PC's
    # current_room (the ``in_scene_location``). The others should be
    # projected away.
    room_states: dict[str, RoomState] = {
        in_scene_location: RoomState(room_id=in_scene_location),
    }
    for i in range(rooms - 1):
        rid = f"distant_room_{i + 1}"
        room_states[rid] = RoomState(room_id=rid)

    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=50),  # late-session shape
        characters=pcs,
        npcs=npc_list,
        npc_pool=pool,
        quest_log={"main": "Find the lost vault."},
        atmosphere="Damp stone.",
        current_region="upper_caverns",
        room_states=room_states,
    )
    snap.character_locations[acting_pc] = in_scene_location
    snap.player_seats[f"player:{acting_pc.lower()}"] = acting_pc

    # ScenarioState — discovered_clues with the requested cardinality.
    # Importing lazily so the test fails on `clues=0` setups without
    # pulling in scenario_state's transitive deps unless needed.
    if clues:
        from sidequest.game.scenario_state import ScenarioState

        st = ScenarioState()
        for i in range(clues):
            st.discovered_clues.add(f"clue_{i:03d}")
        snap.scenario_state = st

    return snap


def _build_sd(snapshot: GameSnapshot, *, player_name: str = "Alice") -> _SessionData:
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snapshot.genre_slug)
    return _SessionData(
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
        player_name=player_name,
        player_id=f"player:{player_name.lower()}",
        snapshot=snapshot,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )


def _state_summary(snap: GameSnapshot) -> str:
    """Drive the production code path and return the state_summary text."""
    sd = _build_sd(snap)
    sd._room = room_for(snap, slug=snap.world_slug)
    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.state_summary is not None, (
        "state_summary missing from TurnContext — fixture set-up broke "
        "before the snapshot-slimming change could be exercised."
    )
    return ctx.state_summary


def _state_summary_payload(snap: GameSnapshot) -> dict:
    return json.loads(_state_summary(snap))


# ---------------------------------------------------------------------------
# AC1 — room_states: project to current-room only
# ---------------------------------------------------------------------------


def test_room_states_projection_keeps_only_acting_pc_current_room() -> None:
    """The narrator only ever reads container retrieval state for the
    acting PC's current room (see ``session_helpers.py:627``: ``current_room_state
    = snapshot.room_states.get(current_room_id)``). Other rooms' state is
    dungeon-graph territory (ADR-055) and addressable via RAG / future
    tool. Drop every other room id from the serialized payload.

    Fixture: 5 rooms in ``room_states``; acting PC at ``main_hall``.
    Expected: ``state_summary["room_states"]`` has exactly one key,
    ``"main_hall"``.
    """
    snap = _make_snapshot(rooms=5)
    payload = _state_summary_payload(snap)

    room_states = payload.get("room_states")
    assert room_states is not None, (
        "room_states absent from state_summary entirely. The projection "
        "should keep the acting PC's current room — an empty/missing "
        "key strips the in-scene container state too. Anti-confabulation "
        "anchor violated."
    )
    assert set(room_states.keys()) == {"main_hall"}, (
        f"room_states projection failed: state_summary['room_states'] "
        f"keys={sorted(room_states.keys())} — expected exactly "
        f"{{'main_hall'}} (the acting PC's current_room). "
        "Other rooms must be dropped; the narrator does not need "
        "off-scene container state in <game_state>."
    )


# ---------------------------------------------------------------------------
# AC2 — npcs: in-scene-only projection
# ---------------------------------------------------------------------------


def test_npcs_projection_keeps_only_in_scene() -> None:
    """The narrator's NPC roster section already reads ``ctx.npcs`` for
    in-scene framing; the snapshot dump should not re-ship every NPC the
    party has ever met. Filter ``state_summary["npcs"]`` to NPCs whose
    ``last_seen_location`` matches the acting PC's current location (or
    who are participants in an unresolved encounter, if applicable).

    Fixture: 5 NPCs (2 in-scene at ``main_hall``, 3 off-stage at
    ``distant_room_*``).
    Expected: ``state_summary["npcs"]`` has exactly 2 entries — the in-scene
    pair. Off-stage names remain in ``npc_pool`` for citation.
    """
    snap = _make_snapshot(npcs_in_scene=2, npcs_off_stage=3)
    payload = _state_summary_payload(snap)

    npcs = payload.get("npcs")
    assert isinstance(npcs, list), (
        f"state_summary['npcs'] not a list (got {type(npcs).__name__}). "
        "The in-scene projection must produce a list (possibly empty if "
        "the scene is solo) — never None or a dict."
    )
    names = sorted(
        (
            entry.get("core", {}).get("name", "")
            if isinstance(entry.get("core"), dict)
            else entry.get("name", "")
        )
        for entry in npcs
    )
    expected = {"InScene_1", "InScene_2"}
    assert set(names) == expected, (
        f"npcs in-scene projection failed: names={names}, "
        f"expected={sorted(expected)}. Off-stage NPCs "
        "(last_seen_location != acting PC's location) should be dropped "
        "from the dump — they ride into the prompt via npc_pool + RAG, "
        "not via <game_state>."
    )


def test_npcs_projection_preserves_off_stage_names_in_npc_pool() -> None:
    """Gaslighting-doctrine safety net: dropping NPC bodies from
    ``state_summary["npcs"]`` is only safe if the narrator can still
    cite the off-stage names. ``npc_pool`` is that anchor. Assert the
    off-stage names are still present in ``state_summary["npc_pool"]``
    (or the equivalent identity-only roster) after the npcs projection.
    """
    snap = _make_snapshot(npcs_in_scene=2, npcs_off_stage=3)
    payload = _state_summary_payload(snap)

    pool = payload.get("npc_pool") or []
    pool_names = {entry.get("name") for entry in pool if isinstance(entry, dict)}
    off_stage = {"OffStage_1", "OffStage_2", "OffStage_3"}
    missing = off_stage - pool_names
    assert not missing, (
        f"npc_pool anchor missing names {sorted(missing)} after the "
        "npcs projection. Without these in the pool the narrator loses "
        "the ability to cite off-stage NPCs and will confabulate names. "
        "If the projection drops the body from `npcs`, the identity "
        "MUST survive in `npc_pool`."
    )


def test_npcs_projection_drops_belief_state_from_in_scene_entries() -> None:
    """Even for in-scene NPCs, ``belief_state`` is dispatch-side state
    (gossip propagation, ADR-053) — the narrator does not name belief
    atoms in prose. Strip the nested ``belief_state`` from every
    surviving entry in ``state_summary["npcs"]``.

    Fixture: 2 in-scene NPCs each with 4 BeliefFacts.
    Expected: each surviving npc entry has no ``belief_state`` key (or
    `belief_state == {}` / `{"beliefs": [], "credibility_scores": {}}`).
    """
    snap = _make_snapshot(npcs_in_scene=2, npcs_off_stage=0)
    payload = _state_summary_payload(snap)

    npcs = payload.get("npcs", [])
    assert npcs, "fixture broken: no in-scene NPCs in the dump"
    for entry in npcs:
        belief = entry.get("belief_state")
        # ``exclude_defaults=True`` makes a default BeliefState absent;
        # any non-empty belief_state surviving in the dump is a leak.
        assert not belief or (
            isinstance(belief, dict) and not belief.get("beliefs")
        ), (
            f"npc entry retains belief_state in state_summary: "
            f"name={entry.get('core', {}).get('name') or entry.get('name')!r} "
            f"belief_state={belief!r}. Strip belief_state from each surviving "
            "npc entry — it's dispatch-side state, not prompt-side. The "
            "narrator gets disposition/personality via dedicated sections."
        )


# ---------------------------------------------------------------------------
# AC3 — known_facts: per-PC tail-K projection
# ---------------------------------------------------------------------------


def test_known_facts_truncated_to_tail_eight_per_pc() -> None:
    """``Character.known_facts`` grows monotonically (every clue / discovered
    fact is appended). ``persistence.py:889`` already uses a tail-of-8
    pattern for journal renders — mirror it here. The narrator gets recent
    facts for continuity; older facts ride via RAG (61-1 wiring).

    Fixture: 1 PC with 25 known_facts in insertion order ("fact #0" .. "fact #24").
    Expected: ``state_summary["characters"][0]["known_facts"]`` length ≤ 8
    AND contains the tail (facts #17..#24 — last 8 by insertion order).
    """
    snap = _make_snapshot(known_facts_per_pc=25)
    payload = _state_summary_payload(snap)

    chars = payload.get("characters")
    assert isinstance(chars, list) and chars, (
        "fixture broken: no characters in state_summary"
    )
    facts = chars[0].get("known_facts") or []
    assert len(facts) <= 8, (
        f"known_facts tail-K projection missing: PC has {len(facts)} "
        f"facts in state_summary (expected ≤ 8). The full 25-fact list "
        "rides into the Valley every turn — exactly the monotonic-growth "
        "pattern this story exists to cut."
    )
    # Content check: the tail must be the most recent insertions.
    contents = [f.get("content") for f in facts]
    expected_tail = [f"fact #{i}" for i in range(17, 25)]
    assert contents == expected_tail, (
        f"known_facts tail-K projection kept the wrong slice: "
        f"contents={contents}, expected last-8={expected_tail}. "
        "Recency-bias the projection so continuity is preserved; older "
        "facts route through RAG (query_known_facts)."
    )


# ---------------------------------------------------------------------------
# AC4 — scenario_state.discovered_clues: size cap
# ---------------------------------------------------------------------------


def test_scenario_state_discovered_clues_capped() -> None:
    """``scenario_state.discovered_clues`` grows with the clue graph
    traversal. ADR-053 belief-and-clue graph is RAG-shaped — the
    narrator does not need the full discovered set in <game_state>.

    Fixture: scenario_state with 30 discovered_clues.
    Expected: ``state_summary["scenario_state"]["discovered_clues"]`` length ≤ 12.
    Note: discovered_clues is a SET — ordering is not preserved, so this
    test asserts size only (see open question #3 in story context).
    """
    snap = _make_snapshot(clues=30)
    payload = _state_summary_payload(snap)

    sc = payload.get("scenario_state")
    assert sc is not None, (
        "scenario_state absent from state_summary entirely — the projection "
        "should cap discovered_clues, not strip the whole scenario block. "
        "Without scenario_state the narrator loses guilty-NPC / clue-graph "
        "anchors."
    )
    clues = sc.get("discovered_clues") or []
    assert len(clues) <= 12, (
        f"discovered_clues cap missing: state_summary carries "
        f"{len(clues)} entries (fixture seeded 30). Cap to ≤ 12 — the "
        "rest are RAG-retrievable per ADR-053."
    )


# ---------------------------------------------------------------------------
# AC5 — regression guards for not-in-snapshot fields
#
# These fields don't ride into snapshot.model_dump() today, but the epic
# named them as "growing fields" because their subsystems grow. The
# regression guard ensures a future developer who adds e.g. a
# location_descriptions dict onto GameSnapshot can't sneak past Phase B
# (which is exactly the ADR-110 §Implementation Notes warning that
# 61-5's architecture gate will enforce).
# ---------------------------------------------------------------------------


def test_journal_absent_from_state_summary() -> None:
    """ADR-100: journal is event-log-derived (``JournalRequestHandler``
    + ``commit_known_fact``); it is NOT a snapshot field. If a future PR
    materializes it onto ``GameSnapshot``, this guard fails."""
    snap = _make_snapshot()
    payload = _state_summary_payload(snap)
    assert "journal" not in payload, (
        "Regression guard tripped: ``journal`` appeared in state_summary. "
        "Journal is event-log-derived per ADR-100 — adding it to "
        "GameSnapshot AND letting it ride into <game_state> recreates "
        "the cost-runaway pattern. Either drop it from the dump or "
        "route it through the journal pipeline."
    )


def test_footnotes_absent_from_state_summary() -> None:
    """``footnotes`` is a per-turn NarrationResult field (``orchestrator.py:452``),
    not a snapshot field. Guard against a future regression that
    materializes them onto the snapshot."""
    snap = _make_snapshot()
    payload = _state_summary_payload(snap)
    assert "footnotes" not in payload, (
        "Regression guard tripped: ``footnotes`` appeared in state_summary. "
        "footnotes are per-turn NarrationResult emissions, not durable "
        "snapshot state. They MUST NOT ride into <game_state>."
    )


def test_location_descriptions_absent_from_state_summary() -> None:
    """ADR-109's PROXIMATE TRIGGER for the cost-runaway. ``LOCATION_DESCRIPTION``
    rides as a WebSocket message (out-of-band); the manifest is loaded
    fresh from ``cookbook/assemble.py`` at room change. The field MUST
    NOT be in the snapshot dump. If a future ADR-109 follow-up
    materializes it onto GameSnapshot WITHOUT updating the drop list,
    this guard fails — and 61-5's architecture gate makes it a
    pre-commit failure.
    """
    snap = _make_snapshot()
    payload = _state_summary_payload(snap)
    for key in ("location_descriptions", "location_description", "location_entities"):
        assert key not in payload, (
            f"Regression guard tripped: ``{key}`` appeared in state_summary. "
            "ADR-109 location descriptions ride out-of-band via the "
            "LOCATION_DESCRIPTION WebSocket message. Adding them to the "
            "snapshot dump is the exact 2026-05-19 -> 2026-05-23 cost-"
            "runaway pattern this story (and 61-5) exist to prevent."
        )


# ---------------------------------------------------------------------------
# AC6 — anti-confabulation anchors preserved (gaslighting doctrine)
# ---------------------------------------------------------------------------


def test_anchor_preserved_characters_after_projections() -> None:
    """The PC roster must survive the new projections — ``characters``
    stays present and non-empty. (The 57-5 anchor test catches this in
    isolation; this test catches it AFTER 61-2's nested-field projections
    run.)"""
    snap = _make_snapshot()
    payload = _state_summary_payload(snap)
    chars = payload.get("characters")
    assert isinstance(chars, list) and chars, (
        "Gaslighting-doctrine anchor stripped by 61-2 projections: "
        "``characters`` is absent or empty from state_summary. The "
        "known_facts tail-K projection must operate INSIDE each PC "
        "entry, not by dropping the entry."
    )


def test_anchor_preserved_quest_log_after_projections() -> None:
    """``quest_log`` (mission anchor) survives all 61-2 projections."""
    snap = _make_snapshot()
    payload = _state_summary_payload(snap)
    assert "quest_log" in payload, (
        "Mission anchor stripped by 61-2 projections: ``quest_log`` "
        "is absent from state_summary."
    )


def test_anchor_preserved_npc_pool_after_projections() -> None:
    """``npc_pool`` is the load-bearing identity-only roster that lets
    the narrator cite off-stage NPCs the in-scene projection dropped.
    If the pool is stripped too, the narrator confabulates names."""
    snap = _make_snapshot()
    payload = _state_summary_payload(snap)
    pool = payload.get("npc_pool")
    assert isinstance(pool, list) and pool, (
        "npc_pool anchor stripped by 61-2 projections: state_summary "
        "lacks the npc_pool identity roster. Without it the off-stage "
        "names dropped by the npcs projection have no fallback citation "
        "source, and the narrator will confabulate."
    )


# ---------------------------------------------------------------------------
# AC7 — wiring: byte reduction on populated late-session fixture
# ---------------------------------------------------------------------------


def test_61_2_extra_byte_reduction_on_late_session_fixture() -> None:
    """On a populated late-session fixture, the 61-2 projections must
    further reduce the dump beyond the Phase-A+B baseline already
    achieved by 57-5. Without this gate the per-field decisions are
    cosmetic.

    Baseline = the post-Phase-A+B encoding (today's behavior at
    ``session_helpers.py:559``: model_dump + the existing four-field
    drop list + narrative_log pop).
    New = the same path with 61-2 projections applied.

    Gate: new_bytes / baseline_bytes <= 0.65 (i.e., ≥35% extra reduction
    on top of Phase B). Late-session fixture: 5 rooms, 5 NPCs each with
    4-belief BeliefStates, 25 known_facts per PC, 30 discovered_clues.
    """
    snap = _make_snapshot()

    # Baseline: the existing post-Phase-A+B encoding — emulated by
    # running the same model_dump + existing pops without the 61-2
    # projections. We compute this directly off the snapshot rather
    # than mutating the production code path.
    baseline_payload = snap.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )
    for f in ("active_tropes", "axis_values", "genie_wishes", "achievement_tracker", "narrative_log"):
        baseline_payload.pop(f, None)
    baseline_text = json.dumps(baseline_payload, separators=(",", ":"))
    bytes_before = len(baseline_text.encode("utf-8"))

    new_text = _state_summary(snap)
    bytes_after = len(new_text.encode("utf-8"))

    ratio = bytes_after / bytes_before if bytes_before else 1.0
    assert ratio <= 0.65, (
        f"61-2 extra reduction below acceptance gate: "
        f"bytes_before(Phase A+B baseline)={bytes_before} "
        f"bytes_after(+61-2 projections)={bytes_after} "
        f"ratio={ratio:.3f} (gate: <=0.65, i.e., ≥35% additional cut). "
        "Either the projections aren't applied, or the fixture has "
        "growth-class fields the audit missed — re-run the per-field "
        "validation in sprint/context/context-story-61-2.md."
    )


# ---------------------------------------------------------------------------
# AC8 — OTEL: projection counts ride on prompt.game_state.bytes
# ---------------------------------------------------------------------------


def test_prompt_game_state_bytes_span_carries_projection_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sebastien's lie-detector requirement (CLAUDE.md OTEL Observability
    Principle, ADR-031 / ADR-090 / ADR-103): the GM panel must be able
    to see WHAT got projected, not just total bytes. Extend the
    existing ``prompt.game_state.bytes`` span (57-5) with four count
    attributes:

      - ``npcs_dropped``: count of NPCs filtered out by the in-scene
        projection
      - ``room_states_dropped``: count of room ids removed
      - ``known_facts_truncated_total``: sum across all PCs of facts
        truncated past the tail-K window
      - ``clues_truncated``: count of discovered_clues over the cap

    Fixture: 5 rooms, 5 NPCs (2 in scene), 25 facts per PC (1 PC), 30
    clues. Expected counts: rooms_dropped=4, npcs_dropped=3,
    known_facts_truncated_total=17, clues_truncated=18.
    """
    from sidequest.telemetry import spans as _spans

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    snap = _make_snapshot()
    _state_summary(snap)

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == "prompt.game_state.bytes"]
    assert len(matching) == 1, (
        f"Expected exactly one prompt.game_state.bytes span; "
        f"got {len(matching)}. Finished spans: "
        f"{sorted({s.name for s in finished})}"
    )
    attrs = matching[0].attributes or {}
    expected = {
        "room_states_dropped": 4,
        "npcs_dropped": 3,
        "known_facts_truncated_total": 17,
        "clues_truncated": 18,
    }
    for key, want in expected.items():
        got = attrs.get(key)
        assert got == want, (
            f"prompt.game_state.bytes span missing or wrong "
            f"attribute {key!r}: got {got!r}, expected {want}. "
            "GM panel cannot see what was projected away — Sebastien's "
            "lie-detector requirement (CLAUDE.md OTEL Observability "
            "Principle) requires these counts so the human can verify "
            "the cut is engaging the right fields."
        )
