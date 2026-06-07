"""Failing tests for Story 97-1 — pool-member relationship projection + promotion.

Design spec (approved 2026-06-07):
``docs/superpowers/specs/2026-06-07-pool-relationship-projection-promotion-design.md``
(orchestrator repo). Hybrid model:

* **Leg 1 — Projection:** ``build_relationship_entries`` projects engaged
  ``npc_pool`` members alongside ``snapshot.npcs``. Seen-gate: scene-present
  AND >=1 deduped interaction (or any disposition beat). Dialogue-only mints
  (the blackthorn "Captain Hale" shape) and pregen latents never card.
* **Leg 2 — Promotion:** a pool member becomes a real ``Npc`` when EITHER the
  ADR-128 ladder crosses ``acquaintance`` (3 deduped interactions) OR the
  narrator writes a valenced beat via ``update_npc_disposition``. Promotion
  carries interaction count, disposition, beat log, and ``invented_from``;
  the pool entry is removed; exactly one relationship card per identity.
* **Invariants:** #742 hostile-context gate applies (live opponent never
  ticks/promotes; resolved encounter releases); no spurious Npc minting for
  the projection leg (``test_disposition_does_not_drift_without_a_stateful_npc``
  must stay green).
* **OTEL (AC 3):** every decision emits, both branches —
  ``npc.pool_projected`` / ``npc.pool_projection_skipped`` /
  ``npc.promoted_from_pool`` / ``npc.promotion_skipped``.

Measured repro this pins (perseus MP 2026-06-07): Rifenna Muse — handshake,
struck deal, three scenes, pool entry — absent from the Relationships tab
while a roster combat opponent got a card.

Tests drive the REAL production seams per the no-source-text-wiring rule:
``_apply_npc_mentions`` (engagement), ``build_relationship_entries``
(projection), ``_maybe_emit_relationships`` (emit + change-gate), and the
registered ``update_npc_disposition`` tool handler (valence trigger).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.orchestrator import NpcMention
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import (
    update_npc_disposition as _update_npc_disposition_module,  # noqa: F401  (registry side-effect)
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Disposition
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.npc_development import ACQUAINTANCE_AT
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.projection.relationships import build_relationship_entries
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.protocol.messages import RelationshipsMessage
from sidequest.server.narration_apply import _apply_npc_mentions
from sidequest.server.websocket_handlers.relationships_emit import (
    _maybe_emit_relationships,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core(name: str) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=4, max=4, base_max=4),
    )


def _pc(name: str) -> Character:
    return Character(
        core=_core(name),
        backstory="A wanderer.",
        char_class="adventurer",
        race="human",
    )


def _npc(name: str, *, last_seen_turn: int = 1) -> Npc:
    npc = Npc(core=_core(name))
    npc.last_seen_turn = last_seen_turn
    return npc


def _member(name: str, **kw: Any) -> NpcPoolMember:
    kw.setdefault("drawn_from", "narrator_invented")
    return NpcPoolMember(name=name, **kw)


def _snap(
    *,
    npcs: list[Npc] | None = None,
    pool: list[NpcPoolMember] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="perseus_cloud",
        turn_manager=TurnManager(interaction=1),
        characters=[_pc("Groucho")],
        npcs=npcs or [],
        npc_pool=pool or [],
    )
    snap.character_locations["Groucho"] = "New Kowloon Docks"
    if encounter is not None:
        snap.encounter = encounter
    return snap


def _cite(snapshot: GameSnapshot, name: str, *, turn: int, side: str = "neutral") -> None:
    """Drive one narrator cite through the production engagement seam."""
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[NpcMention(name=name, side=side)],
        turn_num=turn,
        acting_character_name="Groucho",
    )


def _engage(snapshot: GameSnapshot, name: str, *, turns: int, start: int = 1) -> None:
    """Cite ``name`` once per turn for ``turns`` consecutive turns."""
    for t in range(start, start + turns):
        _cite(snapshot, name, turn=t)


def _live_combat_against(name: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="hp", threshold=10),
        opponent_metric=EncounterMetric(name="hp", threshold=10),
        actors=[
            EncounterActor(name="Groucho", role="pilot", side="player"),
            EncounterActor(name=name, role="hostile", side="opponent"),
        ],
    )


def _entry_names(snapshot: GameSnapshot) -> list[str]:
    return [e.name for e in build_relationship_entries(snapshot)]


def _spans_named(otel_capture: Any, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# Tool-handler harness (mirrors tests/agents/tools/test_update_npc_disposition.py)


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store: Any) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s-97-1",
        perspective_pc="Groucho",
        turn_number=3,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call_disposition_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["update_npc_disposition"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


# ---------------------------------------------------------------------------
# Leg 1 — projection seen-gate (AC 1 + AC 2)
# ---------------------------------------------------------------------------


def test_cited_pool_member_projects_relationship_entry() -> None:
    """AC 1 (the Rifenna repro): an engaged pool member gets a card.

    One scene-present cite through the production seam is the minimum
    engagement; the member must appear in the projection.
    """
    snap = _snap(pool=[_member("Rifenna Muse", role="quest_giver")])
    _cite(snap, "Rifenna Muse", turn=1)

    assert "Rifenna Muse" in _entry_names(snap)


def test_latent_pool_member_not_projected() -> None:
    """AC 2: a pregen-seeded member never cited in play stays off the tab."""
    snap = _snap(pool=[_member("Background Stevedore", drawn_from="name_generator")])

    assert _entry_names(snap) == []


def test_dialogue_only_mint_not_projected() -> None:
    """AC 2 (the blackthorn "Captain Hale" shape): a member minted from a
    dialogue mention of an off-screen man, never scene-present, never cards."""
    snap = _snap(
        pool=[_member("Captain Hale", drawn_from="dialogue_extraction", pronouns="it/its")]
    )

    assert _entry_names(snap) == []


def test_unratified_pool_member_not_projected() -> None:
    """ADR-138 ratification is THE shared projection-eligibility gate
    (``is_projectable``): an observation-pending auto-mint is a phantom the
    49-6 gate may purge next turn — it must not card even if cited."""
    snap = _snap(pool=[_member("Maybe Phantom", observation_pending=True)])
    _cite(snap, "Maybe Phantom", turn=1)

    assert "Maybe Phantom" not in _entry_names(snap)


def test_hostile_cited_pool_member_not_projected() -> None:
    """#742 hostile-context gate extends to the pool: cites of a live seated
    opponent are combat attention, not interest — no tick, no card. Pins the
    'Unknown hostile — Warm trend-up' failure closed for pool members."""
    snap = _snap(
        pool=[_member("Unknown hostile", role="hostile")],
        encounter=_live_combat_against("Unknown hostile"),
    )
    _cite(snap, "Unknown hostile", turn=1)
    _cite(snap, "Unknown hostile", turn=2)

    assert "Unknown hostile" not in _entry_names(snap)


def test_resolved_encounter_releases_pool_gate() -> None:
    """A beaten foe can become a contact: once the encounter resolves, a cite
    engages normally and the member cards."""
    enc = _live_combat_against("Thari Captain")
    enc.resolved = True
    enc.outcome = "player_victory"
    snap = _snap(pool=[_member("Thari Captain", role="hostile")], encounter=enc)
    _cite(snap, "Thari Captain", turn=2)

    assert "Thari Captain" in _entry_names(snap)


def test_roster_npcs_still_project_unchanged() -> None:
    """Regression guard: adding the pool source must not disturb the existing
    roster projection (met NPCs in, latent NPCs out)."""
    snap = _snap(
        npcs=[_npc("Met Roster", last_seen_turn=2), _npc("Latent Roster", last_seen_turn=0)],
        pool=[_member("Latent Pool", drawn_from="name_generator")],
    )

    names = _entry_names(snap)
    assert "Met Roster" in names
    assert "Latent Roster" not in names
    assert "Latent Pool" not in names


# ---------------------------------------------------------------------------
# Leg 2 — promotion triggers (AC 1)
# ---------------------------------------------------------------------------


def test_third_deduped_interaction_promotes_to_npc() -> None:
    """Tier trigger: crossing ``acquaintance`` (ACQUAINTANCE_AT deduped
    interactions) promotes the pool member to a real Npc on snapshot.npcs."""
    snap = _snap(pool=[_member("Rifenna Muse")])
    _engage(snap, "Rifenna Muse", turns=ACQUAINTANCE_AT)

    promoted = [n for n in snap.npcs if n.core.name == "Rifenna Muse"]
    assert len(promoted) == 1, "3rd deduped interaction must promote the pool member"
    assert promoted[0].resolution_tier == "acquaintance"
    assert promoted[0].non_transactional_interactions == ACQUAINTANCE_AT


def test_below_threshold_does_not_promote() -> None:
    """Two deduped interactions stay pool-tier — no premature roster minting."""
    snap = _snap(pool=[_member("Slow Burn")])
    _engage(snap, "Slow Burn", turns=ACQUAINTANCE_AT - 1)

    assert all(n.core.name != "Slow Burn" for n in snap.npcs)
    assert any(m.name == "Slow Burn" for m in snap.npc_pool)


def test_same_turn_and_same_call_cites_dedupe() -> None:
    """Interaction counting is deduped per TURN, including across apply calls
    (the 97-5 double-apply shape must not double-tick toward promotion)."""
    snap = _snap(pool=[_member("Once Per Turn")])
    # Three cites within turn 1: twice in one call, once in a second call
    # (the 97-5 double-apply shape), then one cite on turn 2.
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Once Per Turn"), NpcMention(name="Once Per Turn")],
        turn_num=1,
        acting_character_name="Groucho",
    )
    _cite(snap, "Once Per Turn", turn=1)
    _cite(snap, "Once Per Turn", turn=2)

    # 2 deduped interactions (< ACQUAINTANCE_AT) — must NOT have promoted.
    assert all(n.core.name != "Once Per Turn" for n in snap.npcs)


def test_promotion_carries_state() -> None:
    """Promotion carries disposition, beat log, interaction count, and the
    #738 ``invented_from`` alias; the pool entry is removed (single identity)."""
    snap = _snap(
        pool=[
            _member(
                "Rifenna Muse",
                disposition=Disposition(18),
                invented_from="Varra",
            )
        ]
    )
    _engage(snap, "Rifenna Muse", turns=ACQUAINTANCE_AT)

    promoted = next(n for n in snap.npcs if n.core.name == "Rifenna Muse")
    # Milestone drift may warm the carried value; it must never reset to 0.
    assert int(promoted.disposition) >= 18
    assert promoted.invented_from == "Varra"
    assert promoted.non_transactional_interactions == ACQUAINTANCE_AT
    # The acquaintance milestone beat must survive the promotion.
    assert any("acquaintance" in b.reason for b in promoted.disposition_log)
    # Spec: the pool entry is removed at promotion — one identity, one source.
    assert all(m.name != "Rifenna Muse" for m in snap.npc_pool)


def test_promoted_member_yields_exactly_one_card() -> None:
    """No duplicate cards across the promotion boundary."""
    snap = _snap(pool=[_member("Rifenna Muse")])
    _engage(snap, "Rifenna Muse", turns=ACQUAINTANCE_AT)

    names = _entry_names(snap)
    assert names.count("Rifenna Muse") == 1


def test_promoted_member_recite_reconciles_to_npc() -> None:
    """Post-promotion cites resolve to the Npc (npcs_hit), never re-mint at
    the pool tier — the #738 alias contract must hold across promotion."""
    snap = _snap(pool=[_member("Rifenna Muse", invented_from="Varra")])
    _engage(snap, "Rifenna Muse", turns=ACQUAINTANCE_AT)
    # Narrator keeps using the ORIGINAL invented name post-promotion.
    _cite(snap, "Varra", turn=ACQUAINTANCE_AT + 1)

    rifennas = [n for n in snap.npcs if n.core.name == "Rifenna Muse"]
    assert len(rifennas) == 1
    assert rifennas[0].last_seen_turn == ACQUAINTANCE_AT + 1
    assert all(m.name != "Varra" for m in snap.npc_pool), "re-cite must not re-mint"


def test_hostile_member_never_promotes_mid_fight() -> None:
    """Live opponent-seated member cannot reach the tier trigger — the gate
    suppresses every tick while the encounter is live."""
    snap = _snap(
        pool=[_member("Unknown hostile")],
        encounter=_live_combat_against("Unknown hostile"),
    )
    _engage(snap, "Unknown hostile", turns=ACQUAINTANCE_AT + 2)

    assert all(n.core.name != "Unknown hostile" for n in snap.npcs)


# ---------------------------------------------------------------------------
# Leg 2 — valence-beat trigger via the real update_npc_disposition tool
# ---------------------------------------------------------------------------


def test_valence_beat_promotes_cited_pool_member(otel_capture: Any) -> None:
    """The Rifenna case proper: a narrator valence beat (deal struck) on an
    engaged pool member promotes it immediately — no waiting for 3 cites.

    Today the tool returns not_found for pool members (it searches
    snapshot.npcs only) — this is the sharpest failing test in the story.
    """
    snap = _snap(pool=[_member("Rifenna Muse")])
    _cite(snap, "Rifenna Muse", turn=1)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    result = asyncio.run(
        _call_disposition_tool(
            {"npc_id": "Rifenna Muse", "delta": 10, "reason": "deal struck — handshake"},
            ctx,
        )
    )

    assert result.status is ToolResultStatus.OK, (
        f"valence beat on an engaged pool member must succeed, got {result.status}"
    )
    persisted = store.load().snapshot
    promoted = [n for n in persisted.npcs if n.core.name == "Rifenna Muse"]
    assert len(promoted) == 1, "valence beat must promote the pool member"
    assert any("deal struck" in b.reason for b in promoted[0].disposition_log)
    spans = _spans_named(otel_capture, "npc.promoted_from_pool")
    assert spans, "promotion decision must emit npc.promoted_from_pool"
    assert spans[0].attributes.get("trigger") == "valence_beat"


def test_valence_beat_on_live_opponent_does_not_promote(otel_capture: Any) -> None:
    """Hostile gate beats the valence trigger mid-fight: no promotion, and the
    decline is span-visible (npc.promotion_skipped reason=hostile_context)."""
    snap = _snap(
        pool=[_member("Unknown hostile")],
        encounter=_live_combat_against("Unknown hostile"),
    )
    _cite(snap, "Unknown hostile", turn=1)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    asyncio.run(
        _call_disposition_tool(
            {"npc_id": "Unknown hostile", "delta": 5, "reason": "spared a shot"},
            ctx,
        )
    )

    persisted = store.load().snapshot
    assert all(n.core.name != "Unknown hostile" for n in persisted.npcs)
    skips = _spans_named(otel_capture, "npc.promotion_skipped")
    assert skips, "the decline must emit npc.promotion_skipped (silence = invisible feature)"
    assert skips[0].attributes.get("reason") == "hostile_context"


# ---------------------------------------------------------------------------
# OTEL lie-detector (AC 3) — both branches of every decision emit
# ---------------------------------------------------------------------------


def test_projection_emits_pool_projected_span(otel_capture: Any) -> None:
    snap = _snap(pool=[_member("Rifenna Muse")])
    _cite(snap, "Rifenna Muse", turn=1)
    otel_capture.clear()

    build_relationship_entries(snap)

    spans = _spans_named(otel_capture, "npc.pool_projected")
    assert spans, "projection decision must emit npc.pool_projected"
    assert spans[0].attributes.get("npc_name") == "Rifenna Muse"


def test_projection_skip_emits_span_with_reason(otel_capture: Any) -> None:
    """The decline branch must emit — 'deliberately not carded' must be
    distinguishable from 'projection did not run' on the GM panel."""
    snap = _snap(pool=[_member("Background Stevedore", drawn_from="name_generator")])
    otel_capture.clear()

    build_relationship_entries(snap)

    skips = _spans_named(otel_capture, "npc.pool_projection_skipped")
    assert skips, "the skip decision must emit npc.pool_projection_skipped"
    reason = skips[0].attributes.get("reason")
    assert reason in {"not_present", "no_interactions"}, f"unexpected skip reason {reason!r}"


def test_tier_promotion_emits_span_with_trigger(otel_capture: Any) -> None:
    snap = _snap(pool=[_member("Rifenna Muse")])
    otel_capture.clear()

    _engage(snap, "Rifenna Muse", turns=ACQUAINTANCE_AT)

    spans = _spans_named(otel_capture, "npc.promoted_from_pool")
    assert spans, "tier promotion must emit npc.promoted_from_pool"
    assert spans[0].attributes.get("trigger") == "tier"
    assert spans[0].attributes.get("npc_name") == "Rifenna Muse"


# ---------------------------------------------------------------------------
# Wiring — the real emitter path + change-gate (the mandatory integration leg)
# ---------------------------------------------------------------------------


class _Handler:
    pass


def test_emit_includes_engaged_pool_member_via_real_emitter() -> None:
    """The perseus shape through the production emit path: an engaged pool
    member must reach the RELATIONSHIPS message the client renders."""
    snap = _snap(
        npcs=[_npc("Roster Contact", last_seen_turn=1)],
        pool=[_member("Rifenna Muse", role="quest_giver")],
    )
    _cite(snap, "Rifenna Muse", turn=1)

    handler = _Handler()
    sent: list[tuple[Any, str]] = []
    _maybe_emit_relationships(
        handler,
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((msg, kind)),
    )

    assert len(sent) == 1
    msg, kind = sent[0]
    assert kind == "RELATIONSHIPS"
    assert isinstance(msg, RelationshipsMessage)
    names = [e.name for e in msg.payload.entries]
    assert "Rifenna Muse" in names, "engaged pool member missing from the emitted payload"
    assert "Roster Contact" in names


def test_change_gate_fires_on_new_pool_engagement() -> None:
    """#742 residual 2 (the change-gate half this story owns): the roster
    signature must incorporate pool-derived entries, or a newly-engaged pool
    member never fans out — the card exists but the client never hears."""
    snap = _snap(npcs=[_npc("Roster Contact", last_seen_turn=1)])
    handler = _Handler()
    sent: list[tuple[Any, str]] = []
    emit = lambda msg, kind: sent.append((msg, kind))  # noqa: E731

    _maybe_emit_relationships(handler, snapshot=snap, emit_fn=emit)
    assert len(sent) == 1, "baseline emit"

    # Same snapshot, no change → gate must hold (existing behavior).
    _maybe_emit_relationships(handler, snapshot=snap, emit_fn=emit)
    assert len(sent) == 1, "unchanged signature must not re-emit"

    # A pool member engages → the signature MUST change → re-emit with the card.
    snap.npc_pool.append(_member("Rifenna Muse"))
    _cite(snap, "Rifenna Muse", turn=2)
    _maybe_emit_relationships(handler, snapshot=snap, emit_fn=emit)

    assert len(sent) == 2, "pool engagement must change the relationships signature"
    names = [e.name for e in sent[1][0].payload.entries]
    assert "Rifenna Muse" in names
