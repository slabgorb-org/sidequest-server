"""ADR-136 seen-gate: roster must exclude NPCs the PC hasn't met (last_seen_turn == 0).

Drives the real production path:
  _maybe_emit_relationships → build_relationship_entries (seen-gate) → RelationshipsMessage

A PC who has last_seen_turn > 0 is in-scope; one at last_seen_turn == 0 is latent cast
and must be suppressed.  The OTEL span records the filter count so the GM panel can act
as a lie-detector on the gate.
"""

from __future__ import annotations

from sidequest.game.disposition import Disposition
from sidequest.game.projection.relationships import build_relationship_entries
from sidequest.protocol.messages import RelationshipsMessage
from sidequest.server.websocket_handlers.relationships_emit import _maybe_emit_relationships
from tests.game.test_disposition_beat import _npc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Snap:
    def __init__(self, npcs):
        self.npcs = npcs


class _Handler:
    pass


def _met_npc(name: str, turn: int = 1):
    """NPC the PC has encountered (last_seen_turn > 0)."""
    npc = _npc(name)
    npc.last_seen_turn = turn
    npc.disposition = Disposition(15)
    return npc


def _unmet_npc(name: str):
    """Authored but never encountered NPC (last_seen_turn == 0, spawn default)."""
    npc = _npc(name)
    npc.last_seen_turn = 0
    npc.disposition = Disposition(-30)  # ground-truth spoiler that must not leak
    return npc


# ---------------------------------------------------------------------------
# Unit: build_relationship_entries seen-gate
# ---------------------------------------------------------------------------


def test_unmet_npc_excluded_from_entries():
    """last_seen_turn == 0 → excluded from roster."""
    glinda = _met_npc("Glinda", turn=1)
    wicked_witch = _unmet_npc("Wicked Witch")

    entries = build_relationship_entries(_Snap([glinda, wicked_witch]))

    names = [e.name for e in entries]
    assert "Glinda" in names
    assert "Wicked Witch" not in names


def test_met_npc_included_in_entries():
    """last_seen_turn > 0 → included in roster."""
    glinda = _met_npc("Glinda", turn=3)

    entries = build_relationship_entries(_Snap([glinda]))

    assert len(entries) == 1
    assert entries[0].name == "Glinda"


def test_all_unmet_returns_empty():
    """All latent cast → empty roster (no entries)."""
    snap = _Snap([_unmet_npc("Wicked Witch"), _unmet_npc("Flying Monkey")])
    entries = build_relationship_entries(snap)
    assert entries == []


def test_mixed_roster_only_met_entries():
    """3 NPCs, 2 met, 1 latent → 2 entries, correct names."""
    snap = _Snap(
        [
            _met_npc("Dorothy", turn=1),
            _unmet_npc("Wizard"),
            _met_npc("Scarecrow", turn=2),
        ]
    )
    entries = build_relationship_entries(snap)
    names = [e.name for e in entries]
    assert names == ["Dorothy", "Scarecrow"]
    assert "Wizard" not in names


# ---------------------------------------------------------------------------
# Integration: _maybe_emit_relationships wiring
# ---------------------------------------------------------------------------


def test_emit_excludes_unmet_npcs_via_real_emitter():
    """Drive the full _maybe_emit_relationships → build_relationship_entries path.

    Only met NPCs (last_seen_turn > 0) must appear in the emitted message.
    This is the wiring test: if build_relationship_entries is not gated, the
    unmet NPC leaks its ground-truth disposition (-30 → Cool) to the roster.
    """
    glinda = _met_npc("Glinda", turn=1)
    wicked_witch = _unmet_npc("Wicked Witch")

    handler = _Handler()
    sent = []

    _maybe_emit_relationships(
        handler,
        snapshot=_Snap([glinda, wicked_witch]),
        emit_fn=lambda msg, kind: sent.append((msg, kind)),
    )

    assert len(sent) == 1
    msg, kind = sent[0]
    assert kind == "RELATIONSHIPS"
    assert isinstance(msg, RelationshipsMessage)

    names = [e.name for e in msg.payload.entries]
    assert "Glinda" in names
    assert "Wicked Witch" not in names


def test_emit_with_all_unmet_skips_message():
    """If all NPCs are unmet, emit nothing (no roster to show)."""
    handler = _Handler()
    sent = []

    _maybe_emit_relationships(
        handler,
        snapshot=_Snap([_unmet_npc("Wicked Witch"), _unmet_npc("Flying Monkey")]),
        emit_fn=lambda msg, kind: sent.append((msg, kind)),
    )

    # No met NPCs → empty entry list → message is skipped (no entries to show)
    assert sent == []
