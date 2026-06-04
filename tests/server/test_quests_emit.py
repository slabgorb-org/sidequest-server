"""Change-gated QUESTS emitter + projection builder (Story 77-8, ADR-137).

RED-phase contract (TEA). Mirrors ``tests/server/test_relationships_emit.py``:
build a synthetic snapshot, drive the real ``_maybe_emit_quests`` through a
captured ``emit_fn``, and assert on the emitted typed message. The change-gate
fires on a quest/anchor/stakes change and skips an unchanged spine (Cost Scales
with Drama). The builder turns the stored spine (quest_log: dict[str, QuestEntry],
quest_anchors: list[str], active_stakes: str) into the wire payload.

Imports symbols that do not exist yet — FAIL until Dev (77-8 GREEN) adds:
  - ``sidequest.game.projection.quests.build_quests_payload``
  - ``sidequest.server.websocket_handlers.quests_emit._maybe_emit_quests``
  - ``sidequest.server.websocket_handlers.quests_emit._quests_signature``
"""

from __future__ import annotations

from sidequest.game.projection.quests import build_quests_payload
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.protocol.messages import QuestsMessage
from sidequest.server.websocket_handlers.quests_emit import (
    _maybe_emit_quests,
    _quests_signature,
)


class _Handler:
    pass


def _spine(
    *,
    quests: dict[str, QuestEntry] | None = None,
    anchors: list[str] | None = None,
    stakes: str = "",
) -> GameSnapshot:
    return GameSnapshot(
        quest_log=quests or {},
        quest_anchors=anchors or [],
        active_stakes=stakes,
    )


def _seeded() -> GameSnapshot:
    """The minimum every session now starts with (77-1 seed): one quest, one
    anchor, one stakes line."""
    return _spine(
        quests={
            "q1": QuestEntry(
                title="Go Home",
                objective="Return to Kansas",
                status="active",
                anchor_id="emerald_city",
            )
        },
        anchors=["emerald_city"],
        stakes="The witch hunts you",
    )


# --- builder (AC1, AC3) -----------------------------------------------------


def test_build_payload_from_seeded_spine() -> None:
    """AC1: the builder projects log + anchors + stakes from the stored spine,
    associating each anchor to its owning quest via QuestEntry.anchor_id."""
    payload = build_quests_payload(_seeded())
    assert payload.active_stakes == "The witch hunts you"
    assert len(payload.quest_log) == 1
    entry = payload.quest_log[0]
    assert entry.quest_id == "q1"
    assert entry.title == "Go Home"
    assert entry.objective == "Return to Kansas"
    assert entry.status == "active"
    assert entry.anchor_id == "emerald_city"
    assert len(payload.quest_anchors) == 1
    assert payload.quest_anchors[0].anchor_id == "emerald_city"
    assert payload.quest_anchors[0].quest_id == "q1"


def test_build_payload_empty_spine_is_well_formed() -> None:
    """AC3 / No Silent Fallbacks: empty spine → clean empty payload, no throw."""
    payload = build_quests_payload(_spine())
    assert payload.quest_log == []
    assert payload.quest_anchors == []
    assert payload.active_stakes == ""


def test_build_payload_unowned_anchor_keeps_anchor_quest_id_none() -> None:
    """An anchor not claimed by any quest is still projected, with quest_id None
    (surfaced explicitly, not silently dropped)."""
    payload = build_quests_payload(_spine(anchors=["lone_beacon"], stakes="x"))
    assert len(payload.quest_anchors) == 1
    assert payload.quest_anchors[0].anchor_id == "lone_beacon"
    assert payload.quest_anchors[0].quest_id is None


# --- signature / change-gate (AC2) -----------------------------------------


def test_signature_changes_with_stakes() -> None:
    sig1 = _quests_signature(_seeded())
    bumped = _seeded()
    bumped.active_stakes = "The witch is dead; her sister wants blood"
    assert _quests_signature(bumped) != sig1


def test_signature_changes_with_new_quest() -> None:
    base = _seeded()
    sig1 = _quests_signature(base)
    base.quest_log["q2"] = QuestEntry(title="Free the Winkies", status="active")
    assert _quests_signature(base) != sig1


def test_signature_changes_with_status_update() -> None:
    base = _seeded()
    sig1 = _quests_signature(base)
    base.quest_log["q1"].status = "complete"
    assert _quests_signature(base) != sig1


# --- emitter (AC1, AC2, AC3) ------------------------------------------------


def test_emit_sends_message_when_populated() -> None:
    handler = _Handler()
    sent: list[tuple[object, str]] = []
    _maybe_emit_quests(
        handler, snapshot=_seeded(), emit_fn=lambda m, k: sent.append((m, k))
    )
    assert len(sent) == 1
    msg, kind = sent[0]
    assert kind == "QUESTS"
    assert isinstance(msg, QuestsMessage)
    assert msg.payload.quest_log[0].title == "Go Home"
    assert msg.payload.active_stakes == "The witch hunts you"


def test_emit_skipped_when_unchanged() -> None:
    """AC2: an unchanged spine is a no-op on the second call (change-gated)."""
    handler = _Handler()
    sent: list[object] = []
    snap = _seeded()
    _maybe_emit_quests(handler, snapshot=snap, emit_fn=lambda m, k: sent.append(m))
    _maybe_emit_quests(handler, snapshot=snap, emit_fn=lambda m, k: sent.append(m))
    assert len(sent) == 1


def test_emit_refires_when_stakes_change() -> None:
    """AC2: set_stakes lands → the projection re-broadcasts."""
    handler = _Handler()
    sent: list[object] = []
    snap = _seeded()
    _maybe_emit_quests(handler, snapshot=snap, emit_fn=lambda m, k: sent.append(m))
    snap.active_stakes = "The flying monkeys have your dog"
    _maybe_emit_quests(handler, snapshot=snap, emit_fn=lambda m, k: sent.append(m))
    assert len(sent) == 2
    assert sent[1].payload.active_stakes == "The flying monkeys have your dog"


def test_emit_refires_when_quest_added() -> None:
    """AC2: record_quest lands → the projection re-broadcasts with the new quest."""
    handler = _Handler()
    sent: list[object] = []
    snap = _seeded()
    _maybe_emit_quests(handler, snapshot=snap, emit_fn=lambda m, k: sent.append(m))
    snap.quest_log["q2"] = QuestEntry(title="Free the Winkies", status="active")
    _maybe_emit_quests(handler, snapshot=snap, emit_fn=lambda m, k: sent.append(m))
    assert len(sent) == 2
    titles = {e.title for e in sent[1].payload.quest_log}
    assert "Free the Winkies" in titles


def test_emit_skipped_when_spine_empty() -> None:
    """AC3 + AC5: a wholly empty spine shows nothing (mirrors relationships'
    'no NPCs → nothing'; preserves the wire-parity omission contract). The seed
    arrives at session start, so this only guards the pre-seed window."""
    handler = _Handler()
    sent: list[object] = []
    _maybe_emit_quests(handler, snapshot=_spine(), emit_fn=lambda m, k: sent.append(m))
    assert sent == []
