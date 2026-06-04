"""RED-phase WIRING test for Story 76-6 — stateful NPCs reach the index and the
GM panel through the PRODUCTION sync seam.

The unit tests in ``tests/game/test_entity_sync_stateful_npcs.py`` pin the pure
``sync_entity_cards`` behavior. This test proves the stateful path is reachable
from the production dispatch entry that actually runs every turn —
``sidequest.server.dispatch.entity_sync.sync_for_turn`` (called from
``_execute_narration_turn``). Per the project rule "Every Test Suite Needs a
Wiring Test" and "No Source-Text Wiring Tests", this drives the real function
and asserts on behavior + the emitted watcher event, not on source text.

The lie-detector angle (CLAUDE.md OTEL principle): if the stateful sync works
but ``entity_sync.npc_count`` doesn't *count* the stateful NPC, the GM panel
would under-report the live index. The published ``npc_count`` MUST reflect the
stateful cast.

INTENTIONALLY RED until 76-6 lands: ``sync_entity_cards`` ignores
``snapshot.npcs``, so the production sweep neither stores the stateful card nor
counts it.
"""

from __future__ import annotations

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.session import Npc
from sidequest.server.dispatch import entity_sync as dispatch_entity_sync


class _TurnManager:
    interaction = 1


class _Snapshot:
    """Minimal snapshot exposing exactly what ``sync_for_turn`` reads."""

    def __init__(self, npcs: list[Npc]) -> None:
        self.npc_pool: list = []
        self.npcs = npcs
        self.turn_manager = _TurnManager()


class _SessionData:
    """Duck-typed ``_SessionData`` — ``sync_for_turn`` only touches
    ``.snapshot`` and ``.entity_store``."""

    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot
        self.entity_store = EntityStore()


def _stateful_npc(name: str) -> Npc:
    return Npc(
        core=CreatureCore(name=name, description="A wandering smith.", personality="Gruff."),
        disposition=Disposition(0),
        pronouns="they/them",
        pool_origin=None,
    )


def test_sync_for_turn_indexes_a_stateful_npc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production per-turn sweep projects a scene-stateful NPC into the live
    ``entity_store`` so the narrator's retrieval (75-5) can recall it."""
    captured: list[dict] = []

    def _record(event_type: str, payload: dict, **kwargs: object) -> None:
        captured.append(payload)

    # Patch where used, not where defined (lang-review #6).
    monkeypatch.setattr(dispatch_entity_sync, "_watcher_publish", _record)

    sd = _SessionData(_Snapshot([_stateful_npc("Borin")]))

    dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

    npc_ids = {c.id for c in sd.entity_store.query_by_type(EntityType.NPC)}
    assert "npc:borin" in npc_ids


def test_sync_for_turn_npc_count_reflects_stateful_cast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lie-detector: the published ``npc_count`` must include the stateful NPC,
    or the GM panel under-reports the index that retrieval actually reads."""
    captured: list[dict] = []

    def _record(event_type: str, payload: dict, **kwargs: object) -> None:
        captured.append(payload)

    monkeypatch.setattr(dispatch_entity_sync, "_watcher_publish", _record)

    sd = _SessionData(_Snapshot([_stateful_npc("Borin")]))

    dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

    synced = [p for p in captured if p.get("op") == "synced"]
    assert len(synced) == 1, f"expected one 'synced' event, got {captured!r}"
    assert synced[0]["npc_count"] == 1
