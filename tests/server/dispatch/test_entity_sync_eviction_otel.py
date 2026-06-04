"""Story 75-14 — observability of defensive evictions (ADR-138 §D5/§D6).

The game-tier sweep removes a stranded card; this test proves the removal is
*observable* end-to-end through the production dispatch path. §D6 mandates the
eviction is never a silent drop: each eviction fires an ``entity_card.evicted``
span carrying ``reason=unprojectable`` and the card id, and the per-turn
``entity_sync`` watcher event carries the ``evicted`` count so the GM-panel
lie-detector can see an invariant violation rather than infer it.

Stranding scenario (the only way a card outlives projection-eligibility): a
member is ratified and indexed on one sweep, then re-marked ``observation_pending``
(the mutable 49-6 gate flipping back over the durable store) and re-synced.

Sibling of ``test_entity_sync_ratification_otel.py`` (75-12), reusing its
``session_handler_factory`` and the shared ``otel_capture`` / ``span_attrs_by_name``
helpers from ``tests/server/conftest.py``.
"""

from __future__ import annotations

from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType
from sidequest.game.npc_pool import NpcPoolMember
from tests.server.conftest import span_attrs_by_name


def _seed_ratified(sd, name: str, *, disposition: int = 0) -> NpcPoolMember:
    member = NpcPoolMember(
        name=name,
        role="smith",
        pronouns="they/them",
        drawn_from="world_authored",
        disposition=Disposition(disposition),
    )
    sd.snapshot.npc_pool.append(member)
    return member


def test_eviction_emits_entity_card_evicted_span(session_handler_factory, otel_capture) -> None:
    """§D6: when the sweep evicts a stranded card, an ``entity_card.evicted`` span
    fires with ``reason=unprojectable`` and the evicted card id. The span is the
    GM-panel signal that the defensive gate engaged on an invariant violation."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    member = _seed_ratified(sd, "Borin")

    # Turn 1 — ratified → indexed.
    entity_sync.sync_for_turn(handler, sd)
    assert "npc:borin" in {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}

    # Re-mark pending (the durable card now strands) and re-sync.
    member.observation_pending = True
    entity_sync.sync_for_turn(handler, sd)

    assert "npc:borin" not in {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}

    evict_spans = span_attrs_by_name(otel_capture, "entity_card.evicted")
    assert len(evict_spans) == 1
    attrs = evict_spans[0]
    assert attrs["reason"] == "unprojectable"
    assert attrs["entity_card.id"] == "npc:borin"


def test_no_eviction_emits_no_span(session_handler_factory, otel_capture) -> None:
    """The common case fires nothing: a freshly pending member was never indexed,
    so there is no card to evict and no ``entity_card.evicted`` span — the gate
    does not fabricate phantom eviction events (No Silent Fallbacks, both ways)."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    pending = NpcPoolMember(
        name="Mira",
        pronouns="they/them",
        drawn_from="dialogue_extraction",
        observation_pending=True,
    )
    sd.snapshot.npc_pool.append(pending)

    entity_sync.sync_for_turn(handler, sd)

    assert span_attrs_by_name(otel_capture, "entity_card.evicted") == []


def test_eviction_count_in_watcher_payload(session_handler_factory, monkeypatch) -> None:
    """§D6 on the watcher stream: the per-turn ``entity_sync`` event carries the
    ``evicted`` count so the GM panel sees the defensive eviction without reading
    spans."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    member = _seed_ratified(sd, "Borin")
    entity_sync.sync_for_turn(handler, sd)  # index it (turn 1)

    member.observation_pending = True

    captured: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured.append((event_kind, payload, component, severity))

    monkeypatch.setattr(entity_sync, "_watcher_publish", _capture)

    entity_sync.sync_for_turn(handler, sd)

    events = [c for c in captured if c[1].get("field") == "entity_sync"]
    assert len(events) == 1
    _kind, payload, _component, _severity = events[0]
    assert payload["evicted"] == 1
