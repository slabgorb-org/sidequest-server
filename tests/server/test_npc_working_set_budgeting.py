"""Story 75-2 — budgeted NPC working-set selection (RED phase).

Port of the Rust origin ``npc_context.rs:11-86``
(``build_npc_registry_context_budgeted``) and the deterministic *floor* of
ADR-118's universal-retrieval layer. The narrator currently loads
``snapshot.npc_pool`` VERBATIM every turn (session_handler ``_build_turn_context``
→ orchestrator ``register_npc_roster_section``), so prompt cost grows without
bound as the cast accretes. This story bounds the cost by *selection, not
eviction*:

  * **scene-present** stateful NPCs (``last_seen_turn >= current_turn - window``)
    → FULL profiles, ALWAYS (the floor — never dropped, even when the player
    referenced no NPC this turn; Operator ruling 2026-05-31, ADR-118 D4);
  * **off-stage** stateful NPCs + ALL pool members → BRIEF (name+role) when the
    player referenced any NPC, else COMPACT (name only);
  * nothing is ever evicted — the full roster persists in the snapshot.

Test discipline (server CLAUDE.md "No Source-Text Wiring Tests"): the wiring
test drives the real ``_build_turn_context`` and asserts on the populated
``TurnContext`` field + the emitted OTEL span, never on source-code patterns.

ALL of the following symbols are NET-NEW and do not exist yet — these tests
fail RED until Agent Smith (Dev) implements them:
  * ``sidequest.agents.npc_context.build_npc_working_set`` / ``NpcWorkingSet``
  * ``sidequest.telemetry.spans.SPAN_NPC_WORKING_SET`` + its emitter
  * ``TurnContext.npc_working_set`` field, populated by ``_build_turn_context``
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixtures — synthetic roster with controllable recency
# ---------------------------------------------------------------------------


def _npc(name: str, last_seen_turn: int) -> Npc:
    """A stateful NPC with an explicit recency stamp."""
    return Npc(
        core=CreatureCore(
            name=name,
            description=f"{name} is a test NPC.",
            personality="stoic",
        ),
        last_seen_turn=last_seen_turn,
    )


def _pool_member(name: str) -> NpcPoolMember:
    """An identity-only pool member — carries NO recency (``last_seen_turn``).

    Per the model (``npc_pool.py``) pool members have no recency field, so they
    can never be 'scene-present' and must always land in brief/compact.
    """
    return NpcPoolMember(name=name, role="guard", drawn_from="test")


def _names(entries: list) -> set[str]:
    """Extract names from a tier holding ``Npc`` and/or ``NpcPoolMember``.

    ``Npc`` exposes ``.core.name``; ``NpcPoolMember`` exposes a ``.name`` str.
    """
    out: set[str] = set()
    for e in entries:
        core = getattr(e, "core", None)
        out.add(core.name if core is not None else e.name)
    return out


def _snap(*, current_turn: int, npcs: list[Npc], pool: list[NpcPoolMember]) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=npcs,
        npc_pool=pool,
    )


# ===========================================================================
# AC-1 — Budgeting function classifies by recency
# ===========================================================================


def test_scene_present_npcs_get_full_profiles_when_referenced() -> None:
    """AC-1: with the player referencing an NPC, scene-present stateful NPCs
    (``last_seen_turn >= current_turn - window``) render FULL; off-stage NPCs
    render BRIEF. This is the canonical port of ``npc_context.rs:11-86``.

    current_turn=10, window=2 → scene-present threshold is last_seen >= 8.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    present = [_npc("Borin", 10), _npc("Sable", 9), _npc("Wren", 8)]  # >= 8
    offstage = [_npc("Cassian", 7), _npc("Doyle", 4)]  # < 8
    snap = _snap(current_turn=10, npcs=present + offstage, pool=[])

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs={"Borin"}, recency_window=2
    )

    assert _names(ws.full_profiles) == {"Borin", "Sable", "Wren"}, (
        "scene-present stateful NPCs (last_seen >= current_turn - window) must "
        f"render as full profiles; got {_names(ws.full_profiles)}"
    )
    assert _names(ws.brief_entries) == {"Cassian", "Doyle"}, (
        "off-stage stateful NPCs must render brief when a reference was made; "
        f"got {_names(ws.brief_entries)}"
    )
    assert ws.compact_names == [], (
        f"compact tier must be empty when the player referenced an NPC; got {ws.compact_names}"
    )


def test_recency_boundary_is_inclusive_at_window_edge() -> None:
    """AC-1 boundary (test-paranoia): the threshold is ``>= current_turn -
    window``. An NPC seen EXACTLY ``current_turn - window`` ago is scene-present
    (full); one turn older is off-stage. Off-by-one here corrupts the floor that
    the whole ADR-118 retrieval layer rests on.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    edge = _npc("Edge", 8)  # exactly current_turn - window = 8 → present
    over = _npc("Over", 7)  # one older → off-stage
    snap = _snap(current_turn=10, npcs=[edge, over], pool=[])

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs={"Edge"}, recency_window=2
    )

    assert "Edge" in _names(ws.full_profiles), (
        "NPC at last_seen_turn == current_turn - window must be scene-present (full)"
    )
    assert "Over" not in _names(ws.full_profiles), (
        "NPC at last_seen_turn == current_turn - window - 1 must NOT be full"
    )
    assert "Over" in _names(ws.brief_entries)


def test_never_seen_npc_classified_off_stage_not_scene_present() -> None:
    """No Silent Fallbacks: an NPC with the default ``last_seen_turn == 0``
    (never seen this session) must NOT be silently treated as present. At
    current_turn=10/window=2 it is off-stage.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    fresh = _npc("Newcomer", 0)  # pydantic default — never stamped
    snap = _snap(current_turn=10, npcs=[fresh], pool=[])

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs={"Newcomer"}, recency_window=2
    )

    assert "Newcomer" not in _names(ws.full_profiles), (
        "a never-seen NPC (last_seen_turn=0) must not be classified scene-present"
    )
    assert "Newcomer" in _names(ws.brief_entries)


# ===========================================================================
# ADR-118 floor (Operator ruling 2026-05-31) — scene-present ALWAYS full
# ===========================================================================


def test_floor_holds_scene_present_full_even_with_no_reference() -> None:
    """THE crux of the story. When the player references NO NPC this turn, the
    scene-present floor STILL renders full — only off-stage entities collapse to
    compact. The present scene is never dropped (ADR-118 D4 'floor = ALWAYS
    included, full detail'; SOUL Living World / Guitar Solo).

    This OVERRIDES the session file's literal test-plan bullet (which said
    no-reference → 0 full / 0 brief / all compact, a faithful-but-undesired Rust
    port). Operator chose the floor interpretation on 2026-05-31. See the
    Design Deviation logged by TEA.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    present = [_npc("Borin", 10), _npc("Sable", 9)]  # >= 8 → floor
    offstage = [_npc("Cassian", 3)]  # < 8
    snap = _snap(current_turn=10, npcs=present + offstage, pool=[])

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs=set(), recency_window=2
    )

    assert _names(ws.full_profiles) == {"Borin", "Sable"}, (
        "scene-present NPCs must remain FULL even when no NPC was referenced — "
        f"the floor must hold; got {_names(ws.full_profiles)}"
    )
    assert ws.brief_entries == [], (
        "brief tier is unused in no-reference mode (off-stage go compact); "
        f"got {_names(ws.brief_entries)}"
    )
    assert set(ws.compact_names) == {"Cassian"}, (
        "off-stage NPCs collapse to compact names when no NPC was referenced; "
        f"got {ws.compact_names}"
    )


def test_none_reference_signal_treated_as_no_reference() -> None:
    """``player_referenced_npcs=None`` (signal absent) behaves identically to an
    empty set — no-reference mode, floor still holds. Guards against a None-vs-
    empty divergence.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    present = [_npc("Borin", 10)]
    offstage = [_npc("Cassian", 1)]
    snap = _snap(current_turn=10, npcs=present + offstage, pool=[])

    ws = build_npc_working_set(snap, current_turn=10, player_referenced_npcs=None, recency_window=2)

    assert _names(ws.full_profiles) == {"Borin"}
    assert set(ws.compact_names) == {"Cassian"}
    assert ws.brief_entries == []


# ===========================================================================
# AC-2 — No eviction; pool members never full
# ===========================================================================


def test_no_eviction_every_npc_appears_and_snapshot_unchanged() -> None:
    """AC-2: nothing is evicted. Every input NPC name appears in exactly one
    tier (full ∪ brief ∪ compact == all names), and the underlying snapshot
    roster is not mutated (Diamonds-and-Coal / Living World: bounding is by
    prompt-selection, never deletion).
    """
    from sidequest.agents.npc_context import build_npc_working_set

    present = [_npc("Borin", 10), _npc("Sable", 9)]
    offstage = [_npc("Cassian", 2)]
    pool = [_pool_member("Reeve"), _pool_member("Tally")]
    snap = _snap(current_turn=10, npcs=present + offstage, pool=pool)

    npcs_before = len(snap.npcs)
    pool_before = len(snap.npc_pool)

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs={"Borin"}, recency_window=2
    )

    covered = _names(ws.full_profiles) | _names(ws.brief_entries) | set(ws.compact_names)
    assert covered == {"Borin", "Sable", "Cassian", "Reeve", "Tally"}, (
        f"every roster member must surface in some tier (no eviction); got {covered}"
    )
    assert len(snap.npcs) == npcs_before, "snapshot.npcs must not be mutated by selection"
    assert len(snap.npc_pool) == pool_before, "snapshot.npc_pool must not be mutated by selection"


def test_pool_members_never_full_referenced_mode() -> None:
    """AC-2/scope: pool members carry no recency, so they can never be
    scene-present. With a reference present they render brief, never full.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    stateful = [_npc("Borin", 10)]  # scene-present
    pool = [_pool_member("Reeve"), _pool_member("Tally"), _pool_member("Vance")]
    snap = _snap(current_turn=10, npcs=stateful, pool=pool)

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs={"Borin"}, recency_window=2
    )

    assert _names(ws.full_profiles) == {"Borin"}, "only the stateful scene-present NPC is full"
    assert _names(ws.brief_entries) == {"Reeve", "Tally", "Vance"}, (
        "pool members render brief in referenced mode, never full"
    )


def test_pool_members_compact_when_no_reference() -> None:
    """AC-2/scope: with no reference, pool members collapse to compact names —
    still never full (no recency to qualify them for the floor).
    """
    from sidequest.agents.npc_context import build_npc_working_set

    stateful = [_npc("Borin", 10)]  # scene-present → floor stays full
    pool = [_pool_member("Reeve"), _pool_member("Tally")]
    snap = _snap(current_turn=10, npcs=stateful, pool=pool)

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs=set(), recency_window=2
    )

    assert _names(ws.full_profiles) == {"Borin"}
    assert set(ws.compact_names) == {"Reeve", "Tally"}
    assert "Reeve" not in _names(ws.full_profiles)


def test_empty_roster_produces_empty_tiers() -> None:
    """Edge: an empty roster yields three empty tiers and does not raise."""
    from sidequest.agents.npc_context import build_npc_working_set

    snap = _snap(current_turn=10, npcs=[], pool=[])

    ws = build_npc_working_set(
        snap, current_turn=10, player_referenced_npcs=set(), recency_window=2
    )

    assert ws.full_profiles == []
    assert ws.brief_entries == []
    assert ws.compact_names == []


# ===========================================================================
# AC-3 — OTEL observability (the GM-panel lie detector)
# ===========================================================================


def test_working_set_span_emits_tier_counts(otel_capture) -> None:
    """AC-3: ``build_npc_working_set`` emits ONE ``SPAN_NPC_WORKING_SET`` span
    recording considered-vs-selected with per-tier counts, so the GM panel can
    verify the budgeting fired rather than the narrator improvising the roster.

    Referenced mode: 3 scene-present full, 2 off-stage brief, 5 total.
    The span name is imported (not string-matched) to avoid drift.
    """
    from sidequest.agents.npc_context import build_npc_working_set
    from sidequest.telemetry.spans import SPAN_NPC_WORKING_SET

    present = [_npc("Borin", 10), _npc("Sable", 9), _npc("Wren", 8)]
    offstage = [_npc("Cassian", 3), _npc("Doyle", 1)]
    snap = _snap(current_turn=10, npcs=present + offstage, pool=[])

    build_npc_working_set(snap, current_turn=10, player_referenced_npcs={"Borin"}, recency_window=2)

    spans = otel_capture.get_finished_spans()
    fired = [s for s in spans if s.name == SPAN_NPC_WORKING_SET]
    assert len(fired) == 1, (
        f"expected exactly one {SPAN_NPC_WORKING_SET!r} span; got {len(fired)}. "
        f"Spans: {[(s.name, dict(s.attributes or {})) for s in spans]}"
    )
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("full_count") == 3, f"full_count should be 3; got {attrs.get('full_count')}"
    assert attrs.get("brief_count") == 2, f"brief_count should be 2; got {attrs.get('brief_count')}"
    assert attrs.get("compact_count") == 0, (
        f"compact_count should be 0; got {attrs.get('compact_count')}"
    )
    assert attrs.get("total_pool") == 5, (
        f"total_pool (considered roster size) should be 5; got {attrs.get('total_pool')}"
    )
    assert attrs.get("references_present") is True, (
        "span must record that a reference was made (brief mode)"
    )


def test_working_set_span_reports_compact_mode_when_no_reference(otel_capture) -> None:
    """AC-3 + floor: the span records compact-mode and the held floor when no
    NPC was referenced — full_count reflects the floor, compact_count the
    collapsed off-stage tier, references_present is False.
    """
    from sidequest.agents.npc_context import build_npc_working_set
    from sidequest.telemetry.spans import SPAN_NPC_WORKING_SET

    present = [_npc("Borin", 10), _npc("Sable", 9)]
    offstage = [_npc("Cassian", 2)]
    snap = _snap(current_turn=10, npcs=present + offstage, pool=[])

    build_npc_working_set(snap, current_turn=10, player_referenced_npcs=set(), recency_window=2)

    spans = otel_capture.get_finished_spans()
    fired = [s for s in spans if s.name == SPAN_NPC_WORKING_SET]
    assert len(fired) == 1, f"expected one {SPAN_NPC_WORKING_SET!r} span; got {len(fired)}"
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("full_count") == 2, "floor (scene-present) still counted full in no-ref mode"
    assert attrs.get("brief_count") == 0
    assert attrs.get("compact_count") == 1
    assert attrs.get("references_present") is False


# ===========================================================================
# AC-4 — Wiring: budgeting reachable from the production turn-build path
# ===========================================================================


def test_budgeting_wired_into_build_turn_context(otel_capture) -> None:
    """AC-4 (the mandated wiring test): the budgeted selection is reachable from
    the real ``_build_turn_context`` path, not merely unit-correct in isolation.

    Per server CLAUDE.md 'No Source-Text Wiring Tests', we prove wiring two
    refactor-stable ways: (1) the production ``_build_turn_context`` populates
    the NET-NEW ``TurnContext.npc_working_set`` with the scene-present floor;
    (2) the ``SPAN_NPC_WORKING_SET`` span fired during that real path.

    The assertion targets the floor (scene-present → full), which holds
    regardless of how the brief-vs-compact reference signal is wired — so this
    test is robust to that still-open question (see Delivery Findings).
    """
    from sidequest.genre.loader import load_genre_pack
    from sidequest.server.session_handler import _build_turn_context, _SessionData
    from sidequest.telemetry.spans import SPAN_NPC_WORKING_SET
    from tests._helpers.session_room import room_for

    present = _npc("Borin", 7)  # current_turn=7 → present (>= 7-2=5)
    offstage = _npc("Cassian", 1)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=7),
        npcs=[present, offstage],
    )
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snap.genre_slug)
    sd = _SessionData(
        genre_slug=snap.genre_slug,
        world_slug=snap.world_slug,
        player_name="Alice",
        player_id="player:alice",
        snapshot=snap,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.repository.recent_narrative.return_value = []
    sd.game_slug = "2026-05-31-caverns_mawdeep-1"
    sd._room = room_for(snap, slug="mawdeep")

    context = _build_turn_context(sd)

    assert context.npc_working_set is not None, (
        "_build_turn_context must populate the NET-NEW TurnContext.npc_working_set "
        "field — the budgeted selection must reach the production turn-build path"
    )
    assert "Borin" in _names(context.npc_working_set.full_profiles), (
        "the scene-present NPC must land in the full-profile floor via the real path"
    )

    spans = otel_capture.get_finished_spans()
    assert any(s.name == SPAN_NPC_WORKING_SET for s in spans), (
        f"{SPAN_NPC_WORKING_SET!r} must fire during the real _build_turn_context "
        "path (OTEL wiring proof, not a source-text grep)"
    )


# ===========================================================================
# Rework (Reviewer HIGH finding 2026-06-01) — early-turn never-seen boundary
# ===========================================================================
#
# interaction starts at 1 (TurnManager.interaction default), so for turns 1-2
# the recency threshold (current_turn - window) is <= 0. The buggy condition
# `last_seen_turn >= threshold` then misclassifies a never-seen NPC
# (last_seen_turn=0, the unset sentinel) as scene-present and injects it at
# full detail — defeating the budgeting at session start and violating the
# never-seen-> off-stage invariant (No Silent Fallbacks). These pin the fix.


def test_never_seen_npc_off_stage_at_session_start() -> None:
    """current_turn=1 → threshold=-1. A never-seen NPC (last_seen_turn=0) must
    still be off-stage (compact), NOT floored full. Fails against the
    `last_seen_turn >= threshold` condition until the never-seen sentinel is
    guarded.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    fresh = _npc("Newcomer", 0)  # never cited — unset sentinel
    snap = _snap(current_turn=1, npcs=[fresh], pool=[])

    ws = build_npc_working_set(snap, current_turn=1, player_referenced_npcs=set(), recency_window=2)

    assert "Newcomer" not in _names(ws.full_profiles), (
        "a never-seen NPC (last_seen_turn=0) must be off-stage even at turn 1 "
        f"(threshold <= 0); got full={_names(ws.full_profiles)}"
    )
    assert "Newcomer" in set(ws.compact_names), (
        "never-seen NPC should collapse to a compact name in no-reference mode"
    )


def test_never_seen_npc_off_stage_at_turn_two() -> None:
    """current_turn=2 → threshold=0. The never-seen sentinel (0) satisfies
    `0 >= 0` in the buggy impl. It must be off-stage, not full.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    fresh = _npc("Newcomer", 0)
    snap = _snap(current_turn=2, npcs=[fresh], pool=[])

    ws = build_npc_working_set(snap, current_turn=2, player_referenced_npcs=set(), recency_window=2)

    assert "Newcomer" not in _names(ws.full_profiles), (
        f"never-seen NPC must be off-stage at turn 2 (threshold=0); got full={_names(ws.full_profiles)}"
    )
    assert "Newcomer" in set(ws.compact_names)


def test_genuinely_recent_npc_still_full_at_early_turn() -> None:
    """Guard against over-correction: at an early turn, NPCs actually seen on a
    recent turn must STAY full — only the never-seen sentinel drops off-stage.

    current_turn=2, window=2 (threshold=0): an NPC seen this turn (2) and one
    seen last turn (1) are both scene-present (full); the never-seen NPC (0) is
    off-stage. This fails against the buggy impl (which floors the never-seen
    NPC too) AND would fail an over-aggressive fix that drops legitimately
    recent early-turn NPCs.
    """
    from sidequest.agents.npc_context import build_npc_working_set

    seen_now = _npc("Now", 2)  # seen this turn
    seen_prev = _npc("Prev", 1)  # seen last turn — still within the window
    fresh = _npc("Fresh", 0)  # never seen
    snap = _snap(current_turn=2, npcs=[seen_now, seen_prev, fresh], pool=[])

    ws = build_npc_working_set(
        snap, current_turn=2, player_referenced_npcs={"Now"}, recency_window=2
    )

    assert _names(ws.full_profiles) == {"Now", "Prev"}, (
        "genuinely-recent NPCs must stay full at early turns; the never-seen NPC "
        f"must not; got full={_names(ws.full_profiles)}"
    )
    assert "Fresh" in _names(ws.brief_entries), (
        "the never-seen NPC must drop to the off-stage (brief, referenced-mode) tier"
    )


# ===========================================================================
# Co-location floor (sq-playtest 2026-06-13 — "everyone is hanging out with me")
#
# The recency floor alone kept an NPC cited in region A "scene-present" in
# region B for the whole window, so the narrator kept it on stage and re-cited
# it — a self-perpetuating entourage that never shed on movement. The floor now
# requires *recent AND co-located*: an NPC last seen in a scene that is not the
# party's current scene is demoted off-stage even within the recency window.
# ===========================================================================


def _located_npc(name: str, last_seen_turn: int, last_seen_location: str | None) -> Npc:
    """A stateful NPC with both a recency stamp and a last-seen location."""
    return Npc(
        core=CreatureCore(
            name=name,
            description=f"{name} is a test NPC.",
            personality="stoic",
        ),
        last_seen_turn=last_seen_turn,
        last_seen_location=last_seen_location,
    )


def _located_snap(
    *,
    current_turn: int,
    npcs: list[Npc],
    party_location: str,
) -> GameSnapshot:
    """A snapshot with a single seated PC at ``party_location`` so
    ``party_location()`` resolves a consensus (activating the co-location
    filter)."""
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=npcs,
        npc_pool=[],
        player_seats={"p1": "Pipster"},
        character_locations={"Pipster": party_location},
    )


def test_recent_npc_in_another_scene_is_demoted_off_stage() -> None:
    """The headline entourage fix: an NPC last cited in region A is NOT
    scene-present once the party is in region B, even inside the recency
    window. It drops to the off-stage tier; only the co-located NPC is full."""
    from sidequest.agents.npc_context import build_npc_working_set

    left_behind = _located_npc(
        "Good Witch", last_seen_turn=5, last_seen_location="Munchkin Country"
    )
    here = _located_npc("Toto", last_seen_turn=5, last_seen_location="The Yellow Brick Road")
    snap = _located_snap(
        current_turn=6, npcs=[left_behind, here], party_location="The Yellow Brick Road"
    )

    ws = build_npc_working_set(snap, current_turn=6, player_referenced_npcs=set(), recency_window=2)

    assert _names(ws.full_profiles) == {"Toto"}, (
        "only the co-located NPC may be scene-present; the region-A NPC must be "
        f"demoted off-stage; got full={_names(ws.full_profiles)}"
    )
    assert "Good Witch" in set(ws.compact_names), (
        "the left-behind NPC must surface in the off-stage tier (not evicted)"
    )


def test_colocation_filter_inactive_without_party_location() -> None:
    """No Silent Fallbacks: when no party location resolves (seatless / split
    snapshot), the co-location filter is INACTIVE and the floor falls back to
    recency-only — we never prune on an unknown location."""
    from sidequest.agents.npc_context import build_npc_working_set

    # _snap seats no PC, so party_location() returns None.
    a = _located_npc("Alfa", last_seen_turn=5, last_seen_location="Somewhere Else")
    b = _located_npc("Bravo", last_seen_turn=5, last_seen_location="Here")
    snap = _snap(current_turn=6, npcs=[a, b], pool=[])

    ws = build_npc_working_set(snap, current_turn=6, player_referenced_npcs=set(), recency_window=2)

    assert _names(ws.full_profiles) == {"Alfa", "Bravo"}, (
        "with no resolvable party location the filter must be inactive (both "
        f"recent NPCs stay full); got full={_names(ws.full_profiles)}"
    )


def test_colocation_keeps_npc_with_unknown_last_seen_location() -> None:
    """An NPC that is recent but carries no last_seen_location (legacy / pre-bind)
    is kept full — we fail toward keeping a possibly-present NPC rather than
    pruning on missing data."""
    from sidequest.agents.npc_context import build_npc_working_set

    unknown = _located_npc("Legacy", last_seen_turn=5, last_seen_location=None)
    snap = _located_snap(current_turn=6, npcs=[unknown], party_location="The Yellow Brick Road")

    ws = build_npc_working_set(snap, current_turn=6, player_referenced_npcs=set(), recency_window=2)

    assert _names(ws.full_profiles) == {"Legacy"}, (
        "a recent NPC with unknown last_seen_location must stay full (fail-keep); "
        f"got full={_names(ws.full_profiles)}"
    )


def test_colocation_tolerates_scene_string_phrasing_drift() -> None:
    """Co-location matches with the Monster Manual's substring tolerance so
    "the Yellow Brick Road" co-locates with "Yellow Brick Road" — phrasing drift
    in character_locations must not falsely strand an NPC off-stage."""
    from sidequest.agents.npc_context import build_npc_working_set

    drifted = _located_npc("Scarecrow", last_seen_turn=5, last_seen_location="Yellow Brick Road")
    snap = _located_snap(current_turn=6, npcs=[drifted], party_location="the Yellow Brick Road")

    ws = build_npc_working_set(snap, current_turn=6, player_referenced_npcs=set(), recency_window=2)

    assert _names(ws.full_profiles) == {"Scarecrow"}, (
        f"substring-overlapping scene strings must co-locate; got full={_names(ws.full_profiles)}"
    )


def test_colocation_explicit_param_overrides_snapshot_location() -> None:
    """The ``current_location`` param wins over ``snapshot.party_location()`` —
    lets a caller supply a per-perspective location without re-deriving."""
    from sidequest.agents.npc_context import build_npc_working_set

    here = _located_npc("Tinman", last_seen_turn=5, last_seen_location="Emerald City")
    snap = _located_snap(current_turn=6, npcs=[here], party_location="The Yellow Brick Road")

    ws = build_npc_working_set(
        snap,
        current_turn=6,
        player_referenced_npcs=set(),
        recency_window=2,
        current_location="Emerald City",
    )

    assert _names(ws.full_profiles) == {"Tinman"}, (
        "explicit current_location must override the snapshot consensus; "
        f"got full={_names(ws.full_profiles)}"
    )


def test_colocation_span_reports_pruned_count(otel_capture) -> None:
    """The GM-panel lie detector: the working-set span records how many
    recent-but-elsewhere NPCs were pruned by the co-location filter and that the
    filter was active."""
    from sidequest.agents.npc_context import build_npc_working_set
    from sidequest.telemetry.spans import SPAN_NPC_WORKING_SET

    here = _located_npc("Toto", last_seen_turn=5, last_seen_location="The Yellow Brick Road")
    gone1 = _located_npc("Witch", last_seen_turn=5, last_seen_location="Munchkin Country")
    gone2 = _located_npc("Marbleby", last_seen_turn=5, last_seen_location="Munchkin Country")
    snap = _located_snap(
        current_turn=6, npcs=[here, gone1, gone2], party_location="The Yellow Brick Road"
    )

    build_npc_working_set(snap, current_turn=6, player_referenced_npcs=set(), recency_window=2)

    fired = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_NPC_WORKING_SET]
    assert len(fired) == 1, f"expected one {SPAN_NPC_WORKING_SET!r} span; got {len(fired)}"
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("location_pruned") == 2, (
        f"two region-A NPCs must be reported pruned; got {attrs.get('location_pruned')}"
    )
    assert attrs.get("location_filter_active") is True
    assert attrs.get("full_count") == 1, "only the co-located NPC is full"
