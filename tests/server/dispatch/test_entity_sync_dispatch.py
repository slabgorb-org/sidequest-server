"""RED-phase wiring tests for the per-turn entity-sync dispatch (story 75-6).

Sibling of ``tests/server/dispatch/test_lore_accretion_dispatch.py``. Story 75-6
hooks the universal-retrieval reproject onto the same per-turn seam 75-1 uses
for lore accretion: ``_execute_narration_turn`` accretes lore, then (75-6) syncs
entity cards, then dispatches the embed worker. These tests prove the seam is
actually connected end-to-end, not just that the pure sweep works in isolation:

1. The dispatch module exposes ``sync_for_turn`` (import guard).
2. ``sync_for_turn`` projects the snapshot's NPC pool into ``sd.entity_store``
   and emits a watcher event (GM-panel observability — the lie-detector).
3. ``WebSocketSessionHandler._sync_entity_cards_for_turn`` delegates to the
   module function (refactor-stable wiring guard).
4. A real narration turn drives the sync — the entity_store goes empty →
   populated through the production path (the test that fails if the dev adds
   the function but never calls it from the turn).
5. Across two turns, a mutated NPC's card is refreshed in the store (the
   mutation-loop payoff: retrieval sees the current cast, not a stale snapshot).
6. A no-change re-sync reports ``outcome='skipped'`` (zero-byte-leak).
7. A sync failure is ISOLATED — logged, surfaced as ``op='failed'``, and
   swallowed — never crashing the player's turn (inherited from 75-1).

INTENTIONALLY RED until 75-6 lands — ``sidequest.server.dispatch.entity_sync``
and ``_sync_entity_cards_for_turn`` do not exist yet.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType
from sidequest.game.npc_pool import NpcPoolMember


def _seed_pool_member(sd, name: str, *, disposition: int = 0) -> NpcPoolMember:
    """Attach a pool member to the factory snapshot so the sync has a source."""
    member = NpcPoolMember(
        name=name,
        role="smith",
        pronouns="they/them",
        drawn_from="world_authored",
        disposition=Disposition(disposition),
    )
    sd.snapshot.npc_pool.append(member)
    return member


# ---------------------------------------------------------------------------
# 1. Import guard
# ---------------------------------------------------------------------------


def test_entity_sync_dispatch_exposes_required_function() -> None:
    from sidequest.server.dispatch import entity_sync

    assert hasattr(entity_sync, "sync_for_turn")


# ---------------------------------------------------------------------------
# 2. sync_for_turn behavior + GM-panel observability
# ---------------------------------------------------------------------------


def test_sync_for_turn_projects_pool_into_entity_store(session_handler_factory) -> None:
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_pool_member(sd, "Borin")

    entity_sync.sync_for_turn(handler, sd)

    npc_ids = {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}
    assert "npc:borin" in npc_ids
    assert sd.entity_store.cards["npc:borin"].embedding_pending is True


def test_sync_for_turn_emits_watcher_event(session_handler_factory, monkeypatch) -> None:
    """Observable on the GM panel: the sync emits a state_transition carrying
    the reproject count and outcome (the ADR-118 §D5 ``accretion.entity_sync``
    contract, surfaced through the watcher stream)."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_pool_member(sd, "Borin")

    captured: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured.append((event_kind, payload, component, severity))

    monkeypatch.setattr(entity_sync, "_watcher_publish", _capture)

    entity_sync.sync_for_turn(handler, sd)

    events = [c for c in captured if c[1].get("field") == "entity_sync"]
    assert len(events) == 1
    kind, payload, component, _severity = events[0]
    assert kind == "state_transition"
    assert payload["reprojected"] == 1
    assert payload["outcome"] == "success"
    assert component == "retrieval"


def test_resync_unchanged_roster_reports_skipped(session_handler_factory, monkeypatch) -> None:
    """Zero-byte-leak at the dispatch layer: a second sync with no change emits
    ``outcome='skipped'`` and reprojects nothing."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_pool_member(sd, "Borin")
    entity_sync.sync_for_turn(handler, sd)
    # Pretend the embed worker ran so the card is fully settled.
    sd.entity_store.cards["npc:borin"].embedding = [1.0, 0.0]
    sd.entity_store.cards["npc:borin"].embedding_pending = False

    captured: list[tuple] = []
    monkeypatch.setattr(
        entity_sync,
        "_watcher_publish",
        lambda k, p, component=None, severity=None: captured.append((k, p)),
    )

    entity_sync.sync_for_turn(handler, sd)

    events = [c for c in captured if c[1].get("field") == "entity_sync"]
    assert len(events) == 1
    assert events[0][1]["op"] == "synced"
    assert events[0][1]["outcome"] == "skipped"
    assert events[0][1]["reprojected"] == 0
    assert sd.entity_store.cards["npc:borin"].embedding_pending is False


# ---------------------------------------------------------------------------
# 3. Handler delegate wiring guard (mirror test_lore_accretion_dispatch.py)
# ---------------------------------------------------------------------------


def test_handler_delegate_calls_sync_for_turn(session_handler_factory, monkeypatch) -> None:
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    captured: list[tuple] = []
    monkeypatch.setattr(entity_sync, "sync_for_turn", lambda h, s: captured.append((h, s)))

    handler._sync_entity_cards_for_turn(sd)

    assert captured == [(handler, sd)]


# ---------------------------------------------------------------------------
# 4. Production-path proof — a real turn drives the sync (empty → populated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_turn_syncs_entity_cards(session_handler_factory) -> None:
    """The strongest wiring guard: drive the real narration turn (mocked LLM)
    with a pool member present, and assert the entity_store was populated.
    Fails if the sync exists but is never called from the turn pipeline."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _build_turn_context

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_pool_member(sd, "Borin")
    assert len(sd.entity_store) == 0  # the store is inert before 75-6
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You survey the room.")
    )

    await handler._execute_narration_turn(sd, "look around", _build_turn_context(sd))

    assert "npc:borin" in {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}


@pytest.mark.asyncio
async def test_second_turn_refreshes_mutated_card(session_handler_factory) -> None:
    """The mutation-loop payoff (ADR-118 §D2): turn 1 seeds the card; the NPC's
    attitude crosses neutral → friendly; turn 2 refreshes the stored card so
    retrieval keys on the *current* relationship, not the stale one."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _build_turn_context

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    member = _seed_pool_member(sd, "Borin", disposition=0)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="...")
    )

    await handler._execute_narration_turn(sd, "greet borin", _build_turn_context(sd))
    assert sd.entity_store.cards["npc:borin"].content.endswith("neutral")

    member.disposition = Disposition(50)  # neutral -> friendly
    await handler._execute_narration_turn(sd, "share a drink", _build_turn_context(sd))

    assert sd.entity_store.cards["npc:borin"].content.endswith("friendly")


# ---------------------------------------------------------------------------
# 5. Failure isolation — sync must never crash the turn (inherited from 75-1)
# ---------------------------------------------------------------------------


def test_sync_for_turn_does_not_propagate_exception(session_handler_factory, monkeypatch) -> None:
    """Like ``accrete_for_turn``, the entity sync runs BEFORE narration is
    delivered. A sync failure must be isolated: logged, surfaced as an
    ``op='failed'`` watcher event, and SWALLOWED — never re-raised — so the
    player still gets their narration."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_pool_member(sd, "Borin")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated entity-sync failure")

    monkeypatch.setattr(entity_sync, "sync_entity_cards", _boom)

    captured: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured.append((event_kind, payload, component, severity))

    monkeypatch.setattr(entity_sync, "_watcher_publish", _capture)

    # MUST NOT raise — the turn survives.
    entity_sync.sync_for_turn(handler, sd)

    failed = [c for c in captured if c[1].get("op") == "failed"]
    assert len(failed) == 1, "sync failure must emit an op='failed' watcher event"
    kind, payload, component, severity = failed[0]
    assert kind == "state_transition"
    assert payload["field"] == "entity_sync"
    assert payload["error"] == "RuntimeError"
    assert component == "retrieval"
    assert severity == "error"
