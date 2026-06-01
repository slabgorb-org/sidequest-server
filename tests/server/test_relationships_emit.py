"""Change-gated RELATIONSHIPS emitter (ADR-136, Task 10).

Mirrors the canonical fixture-driven emit-test shape from
``tests/server/test_location_description_emit.py``: build a synthetic snapshot,
drive the real ``_maybe_emit_relationships`` through a captured ``emit_fn``, and
assert on the emitted typed message. The signature test guards the change-gate
key so a disposition shift re-fires while an unchanged roster is skipped (Cost
Scales with Drama).
"""

from sidequest.game.disposition import Disposition
from sidequest.protocol.messages import RelationshipsMessage
from sidequest.server.websocket_handlers.relationships_emit import (
    _maybe_emit_relationships,
    _relationships_signature,
)
from tests.game.test_disposition_beat import _npc


class _Snap:
    def __init__(self, npcs):
        self.npcs = npcs


class _Handler:
    pass


def test_signature_changes_with_disposition():
    npc = _npc("Tabitha")
    npc.disposition = Disposition(10)
    sig1 = _relationships_signature(_Snap([npc]))
    npc.disposition = Disposition(20)
    sig2 = _relationships_signature(_Snap([npc]))
    assert sig1 != sig2


def test_emit_sends_message_when_changed():
    npc = _npc("Tabitha")
    npc.disposition = Disposition(24)
    npc.record_disposition_beat(turn=6, delta=3, reason="candor", location="parlor")
    handler = _Handler()
    sent = []

    def emit_fn(msg, kind):
        sent.append((msg, kind))

    _maybe_emit_relationships(handler, snapshot=_Snap([npc]), emit_fn=emit_fn)
    assert len(sent) == 1
    msg, kind = sent[0]
    assert kind == "RELATIONSHIPS"
    assert isinstance(msg, RelationshipsMessage)
    assert msg.payload.entries[0].name == "Tabitha"


def test_emit_skipped_when_unchanged():
    npc = _npc("Tabitha")
    npc.disposition = Disposition(24)
    handler = _Handler()
    sent = []

    def emit_fn(msg, kind):
        sent.append((msg, kind))

    _maybe_emit_relationships(handler, snapshot=_Snap([npc]), emit_fn=emit_fn)
    _maybe_emit_relationships(handler, snapshot=_Snap([npc]), emit_fn=emit_fn)
    assert len(sent) == 1  # second call unchanged → skipped


def test_emit_skipped_when_no_npcs():
    handler = _Handler()
    sent = []
    _maybe_emit_relationships(handler, snapshot=_Snap([]), emit_fn=lambda m, k: sent.append(m))
    assert sent == []
