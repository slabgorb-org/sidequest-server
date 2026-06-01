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
