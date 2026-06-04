"""RED-phase wiring + observability tests for Story 75-12 — the ratification
gate on the ADR-118 NPC projection (ADR-138 §D2/§D5/§D6).

Two threads, both end-to-end through the production dispatch path:

1. **§D6 observability (the lie-detector).** When ``sync_for_turn`` withholds an
   unratified pool member from the index, the GM panel must be able to SEE it.
   The per-turn watcher event must carry the ``skipped_unratified`` count — the
   skip is never silent (CLAUDE.md OTEL Observability Principle). Without this,
   you cannot tell "the narrator never mentioned the phantom" from "the gate
   silently swallowed a real NPC".

2. **§D2 invariant — gate the FILL, not the FLOOR.** The same scene-present
   pending member that is WITHHELD from the semantic index must STILL appear in
   the working-set floor (75-2). A player talking to a freshly-minted NPC sees it
   at full detail this turn; ratification only governs whether it gets *recalled*
   semantically later. This is the wiring test that fails if a dev gates the
   wrong layer and accidentally drops the member from the narrator's view.

Sibling of ``tests/server/dispatch/test_entity_sync_dispatch.py`` (75-6), reusing
its ``session_handler_factory`` + ``_watcher_publish`` monkeypatch capture.

INTENTIONALLY RED until 75-12 lands — ``sync_for_turn`` does not yet gate on
``is_projectable`` and the watcher payload has no ``skipped_unratified`` field.
"""

from __future__ import annotations

from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType
from sidequest.game.npc_pool import NpcPoolMember


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


def _seed_pending(sd, name: str) -> NpcPoolMember:
    """An auto-minted phantom the 49-6 gate has not ratified yet."""
    member = NpcPoolMember(
        name=name,
        role=None,
        pronouns="they/them",
        drawn_from="dialogue_extraction",
        observation_pending=True,
    )
    sd.snapshot.npc_pool.append(member)
    return member


# ---------------------------------------------------------------------------
# §D6 — the skip is observable on the GM-panel watcher stream
# ---------------------------------------------------------------------------


def test_sync_for_turn_reports_skipped_unratified_to_watcher(
    session_handler_factory, monkeypatch
) -> None:
    """The GM-panel lie-detector: when the sync withholds a pending member, the
    ``entity_sync`` state_transition event carries ``skipped_unratified``. A
    ratified sibling is still reprojected, so the same event shows the honest
    split (1 indexed, 1 withheld)."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_ratified(sd, "Borin")
    _seed_pending(sd, "Mira")

    captured: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured.append((event_kind, payload, component, severity))

    monkeypatch.setattr(entity_sync, "_watcher_publish", _capture)

    entity_sync.sync_for_turn(handler, sd)

    events = [c for c in captured if c[1].get("field") == "entity_sync"]
    assert len(events) == 1
    _kind, payload, _component, _severity = events[0]
    assert payload["skipped_unratified"] == 1
    assert payload["reprojected"] == 1


def test_sync_for_turn_does_not_index_pending_member(session_handler_factory) -> None:
    """End-to-end through the dispatch seam: a pending member never lands in
    ``sd.entity_store``, while its ratified scene-mate does. Fails today — the
    production sync projects every pool member."""
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_ratified(sd, "Borin")
    _seed_pending(sd, "Mira")

    entity_sync.sync_for_turn(handler, sd)

    npc_ids = {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}
    assert "npc:borin" in npc_ids
    assert "npc:mira" not in npc_ids


# ---------------------------------------------------------------------------
# §D2 — gate the FILL, not the FLOOR: the withheld member is still in the floor
# ---------------------------------------------------------------------------


def test_pending_member_withheld_from_index_but_present_in_floor(
    session_handler_factory,
) -> None:
    """The load-bearing §D2 invariant, proven end-to-end on one snapshot:

    - FILL (index): the pending member is NOT in ``sd.entity_store`` after sync.
    - FLOOR (working set): the SAME pending member still surfaces in
      ``build_npc_working_set`` — the narrator sees it this turn.

    Ratification governs semantic *recall*, never scene-present *visibility*.
    If a dev gates the floor by mistake, the member vanishes from the working
    set and this fails."""
    from sidequest.agents.npc_context import build_npc_working_set
    from sidequest.server.dispatch import entity_sync

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_pending(sd, "Mira")

    entity_sync.sync_for_turn(handler, sd)

    # FILL: withheld from the semantic index.
    assert "npc:mira" not in {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}

    # FLOOR: still in the budgeted working set the narrator reads.
    floor = build_npc_working_set(
        sd.snapshot,
        current_turn=sd.snapshot.turn_manager.interaction,
    )
    floor_names = set(floor.compact_names) | {_name(entry) for entry in floor.brief_entries}
    assert "Mira" in floor_names


def _name(entry) -> str:
    """Floor entries are ``Npc`` (name at ``.core.name``) or ``NpcPoolMember``
    (name at ``.name``)."""
    core = getattr(entry, "core", None)
    return core.name if core is not None else entry.name
