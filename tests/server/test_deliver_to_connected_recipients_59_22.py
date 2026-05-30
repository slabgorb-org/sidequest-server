"""Story 59-22 — Shared ``_deliver_to_connected_recipients`` dispatch helper.

Follow-up to 59-16 (simplify-reuse, not auto-applied). ``emitters.py`` had TWO
recipient-delivery loops that each re-implemented the same dispatch sequence:

    socket_for_player(pid) -> None  => _emit_recipient_dropped(kind, pid, "socket_gone")
    queue_for_socket(sid)  -> None  => _emit_recipient_dropped(kind, pid, "queue_detached")
    queue.put_nowait(msg)

    * ``_deliver_fanout``                       (the projection fan-out path)
    * the ``per_recipient_payload`` loop in ``emit_event``  (the CONFRONTATION supplier path)

This story extracts ONE helper that both call sites delegate to. The two paths
differ only in MESSAGE CONSTRUCTION, which is abstracted behind a builder
callable: ``message_builder(pid) -> message | None`` where ``None`` means "send
nothing to this socket" (the single skip rule — each caller folds its own gate,
include=False or supplier-None, into the builder).

These are characterization tests for a pure refactor:
  * The helper-direct tests pin the shared dispatch contract (happy path + BOTH
    drop reasons + builder-None skip). They reference
    ``emitters._deliver_to_connected_recipients``, which does not exist yet → RED.
  * The wiring tests prove BOTH production call sites route through the one helper
    (a spy that is never invoked in RED because the call sites still inline the
    loop) — guarding against a half-applied extraction.

Behaviour is unchanged; the existing ``test_emit_fanout_recipient_drop.py`` and
``test_confrontation_single_delivery.py`` suites are the regression safety net.
Pure engine behaviour — binds to nothing under ``sidequest-content``.
"""

from __future__ import annotations

import asyncio

import pytest

from sidequest.game.persistence import GameMode
from sidequest.game.projection_filter import FilterDecision
from sidequest.protocol.messages import ConfrontationPayload
from sidequest.server import emitters
from sidequest.server.session_room import RoomRegistry


class _FakeMsg:
    """Minimal stand-in for a typed GameMessage — captures its payload."""

    def __init__(self, payload):
        self.payload = payload


def _spy_recipient_dropped(monkeypatch) -> list[tuple[str, str, str]]:
    """Spy ``_emit_recipient_dropped`` so drop surfacing is assertable without
    standing up the watcher. Returns the recording list of (kind, pid, reason)."""
    recorded: list[tuple[str, str, str]] = []

    def _record(kind: str, player_id: str, reason: str) -> None:
        recorded.append((kind, player_id, reason))

    monkeypatch.setattr(emitters, "_emit_recipient_dropped", _record)
    return recorded


# ---------------------------------------------------------------------------
# Helper-direct contract (AC1 + AC3): one dispatch sequence, all branches
# ---------------------------------------------------------------------------


def test_helper_delivers_to_live_recipients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: every recipient whose builder yields a message and whose
    socket/queue are live gets exactly that message enqueued."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="dtcr-live", mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.connect("p_donut", socket_id="sock-donut")
    room.attach_outbound("sock-carl", asyncio.Queue())
    room.attach_outbound("sock-donut", asyncio.Queue())
    dropped = _spy_recipient_dropped(monkeypatch)

    def builder(pid: str):
        return _FakeMsg({"to": pid})

    emitters._deliver_to_connected_recipients(
        room,
        ["p_carl", "p_donut"],
        message_builder=builder,
        kind="NARRATION",
    )

    carl_q = room.queue_for_socket("sock-carl")
    donut_q = room.queue_for_socket("sock-donut")
    assert carl_q is not None and donut_q is not None
    assert carl_q.qsize() == 1, "carl (live queue) must receive the frame"
    assert donut_q.qsize() == 1, "donut (live queue) must receive the frame"
    assert carl_q.get_nowait().payload == {"to": "p_carl"}
    assert not dropped, "no drops when every recipient is live"


def test_helper_surfaces_socket_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recipient with no registered socket (``socket_for_player`` → None) is
    surfaced as a ``socket_gone`` drop — not silently skipped."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="dtcr-sockgone", mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.attach_outbound("sock-carl", asyncio.Queue())
    # p_ghost is in the recipient list but never connected → socket_for_player None.
    dropped = _spy_recipient_dropped(monkeypatch)

    emitters._deliver_to_connected_recipients(
        room,
        ["p_carl", "p_ghost"],
        message_builder=lambda pid: _FakeMsg({"to": pid}),
        kind="CONFRONTATION",
    )

    assert ("CONFRONTATION", "p_ghost", "socket_gone") in dropped, (
        "a recipient with no socket must surface a socket_gone drop"
    )
    # The live recipient still got served.
    assert room.queue_for_socket("sock-carl").qsize() == 1


def test_helper_surfaces_queue_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recipient whose socket exists but whose outbound queue was detached
    (``queue_for_socket`` → None, exactly what ``detach_outbound`` leaves) is
    surfaced as a ``queue_detached`` drop."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="dtcr-qdetach", mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.connect("p_katia", socket_id="sock-katia")
    room.attach_outbound("sock-carl", asyncio.Queue())
    # NOTE: deliberately NO attach_outbound for sock-katia.
    dropped = _spy_recipient_dropped(monkeypatch)

    emitters._deliver_to_connected_recipients(
        room,
        ["p_carl", "p_katia"],
        message_builder=lambda pid: _FakeMsg({"to": pid}),
        kind="NARRATION",
    )

    assert ("NARRATION", "p_katia", "queue_detached") in dropped, (
        "a recipient with a detached queue must surface a queue_detached drop"
    )
    assert room.queue_for_socket("sock-carl").qsize() == 1


def test_helper_skips_when_builder_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single skip rule: a builder that returns ``None`` for a recipient
    means "send nothing" — no delivery AND no drop (it is not a transport
    failure). This is how each caller folds its own gate (include=False for the
    fan-out path; supplier-None for the CONFRONTATION path) into the helper."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="dtcr-none", mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.connect("p_donut", socket_id="sock-donut")
    room.attach_outbound("sock-carl", asyncio.Queue())
    room.attach_outbound("sock-donut", asyncio.Queue())
    dropped = _spy_recipient_dropped(monkeypatch)

    def builder(pid: str):
        return None if pid == "p_donut" else _FakeMsg({"to": pid})

    emitters._deliver_to_connected_recipients(
        room,
        ["p_carl", "p_donut"],
        message_builder=builder,
        kind="NARRATION",
    )

    assert room.queue_for_socket("sock-carl").qsize() == 1, "carl still served"
    assert room.queue_for_socket("sock-donut").qsize() == 0, "donut deliberately skipped"
    assert not dropped, "a builder-None skip is not a transport drop"


# ---------------------------------------------------------------------------
# Wiring (AC1): BOTH production call sites delegate to the one helper
# ---------------------------------------------------------------------------


def _spy_helper(monkeypatch) -> list[tuple[tuple, dict]]:
    """Replace the shared helper with a recorder. ``raising=False`` so the spy
    installs even in RED (before the helper exists). Returns None like the
    proposed contract — the supplier caller captures the emitter frame via its
    builder closure, so a no-op helper degrades to emit_event's 59-20 fallback
    rather than crashing."""
    calls: list[tuple[tuple, dict]] = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(emitters, "_deliver_to_connected_recipients", _record, raising=False)
    return calls


def test_deliver_fanout_routes_through_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The projection fan-out path must dispatch via the shared helper."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="dtcr-wire-fanout", mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.attach_outbound("sock-carl", asyncio.Queue())
    calls = _spy_helper(monkeypatch)

    fanout = [("p_carl", FilterDecision(include=True, payload_json="{}"), {})]
    emitters._deliver_fanout(
        room,
        fanout,
        message_cls=_FakeMsg,
        payload_cls=None,
        kind="NARRATION",
        seq=1,
    )

    assert calls, (
        "_deliver_fanout must route delivery through _deliver_to_connected_recipients "
        "— the extraction is incomplete if the fan-out path still inlines the loop"
    )


def test_supplier_path_routes_through_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CONFRONTATION ``per_recipient_payload`` path in ``emit_event`` must
    dispatch via the shared helper too — otherwise the dedup is half-done.

    Drives the REAL ``emit_event`` supplier branch (signature
    ``emit_event(handler, kind, payload_model, *, per_recipient_payload=...)``)
    through a minimal fake handler — a real ``SessionRoom`` for transport plus a
    fake EventLog so the branch's ``repo.transaction()`` runs without Postgres.
    The branch is reached because ``per_recipient_payload`` is supplied and the
    room exposes ``connected_player_ids``. In RED the loop is still inlined, so
    the spy is never called → AssertionError (the correct reason). In GREEN the
    branch delegates to the helper → the spy records the call.
    """
    registry = RoomRegistry()
    room = registry.get_or_create(slug="dtcr-wire-supplier", mode=GameMode.MULTIPLAYER)
    room.connect("p_ace", socket_id="sock-ace")
    room.attach_outbound("sock-ace", asyncio.Queue())
    handler = _FakeHandler(room, player_id="p_ace")
    calls = _spy_helper(monkeypatch)

    union = _confrontation_payload(["attack", "defend"])
    emitters.emit_event(
        handler,
        "CONFRONTATION",
        union,
        per_recipient_payload=lambda pid: _confrontation_payload([f"{pid}_only"]),
    )

    assert calls, (
        "emit_event's per_recipient_payload path must route delivery through "
        "_deliver_to_connected_recipients — the supplier loop must not keep its "
        "own copy of the socket/queue/drop dispatch"
    )


# --- supplier-path fixtures: a real payload + a no-PG fake handler -----------


def _confrontation_payload(beats: list[str]) -> ConfrontationPayload:
    """A real ConfrontationPayload so ``_message_cls_for`` resolves to
    ConfrontationMessage and ``_payload_to_json`` serialises cleanly."""
    return ConfrontationPayload(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        genre_slug="caverns_and_claudes",
        beats=[{"id": b, "label": b.title(), "kind": "strike"} for b in beats],
    )


class _FakeEventLog:
    """Minimal EventLog: ``repository.transaction()`` yields a txn whose
    ``append_event`` returns an incrementing seq row — enough for the supplier
    branch's C2 block to run without a database."""

    def __init__(self) -> None:
        self._seq = 0

    class _Row:
        def __init__(self, seq: int) -> None:
            self.seq = seq

    class _Txn:
        def __init__(self, outer: "_FakeEventLog") -> None:
            self._outer = outer

        def __enter__(self) -> "_FakeEventLog._Txn":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def append_event(self, *, kind: str, payload_json: str) -> "_FakeEventLog._Row":
            self._outer._seq += 1
            return _FakeEventLog._Row(self._outer._seq)

    @property
    def repository(self) -> "_FakeEventLog":
        return self

    def transaction(self) -> "_FakeEventLog._Txn":
        return self._Txn(self)


class _FakeHandler:
    """Just the attributes ``emit_event`` reads on the CONFRONTATION supplier
    path: ``_room`` (real transport), ``_event_log`` (fake), ``_player_id``
    (the emitter). The rest are read elsewhere and default to None/absent."""

    def __init__(self, room: object, *, player_id: str) -> None:
        self._room = room
        self._event_log = _FakeEventLog()
        self._player_id = player_id
        self._session_player_id = None
        self._session_data = None
        self._projection_filter = None
        self._socket_id = None
