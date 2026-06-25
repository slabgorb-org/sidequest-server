"""bind_companion_bond registers a pet bond and emits companion.bond_resolved;
an unknown relationship fails closed (no bond) but still emits a span so the GM
panel sees the rejection rather than a silent grant (Plan B Task 3).

The watcher span is captured by patching the module-level ``_watcher_publish``
alias in connect.py (imported at connect.py:69) — patch where USED, per the
project's monkeypatch convention.
"""

from __future__ import annotations

from sidequest.handlers.connect import bind_companion_bond
from sidequest.protocol.messages import SessionEventPayload
from sidequest.server.session_room import SessionRoom
from sidequest.game.persistence import GameMode


def _payload(**kw) -> SessionEventPayload:
    return SessionEventPayload(event="connect", game_slug="abc", **kw)


def _room() -> SessionRoom:
    return SessionRoom(slug="companion-connect", mode=GameMode.SOLO)


def _capture_spans(monkeypatch) -> list[tuple[str, dict]]:
    spans: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "sidequest.handlers.connect._watcher_publish",
        lambda name, fields, **_kw: spans.append((name, fields)),
    )
    return spans


def test_pet_bond_registered_and_span_emitted(monkeypatch):
    spans = _capture_spans(monkeypatch)
    room = _room()
    room.set_player_identity("rex-pid", "donut.local")

    bind_companion_bond(
        room,
        "rex-pid",
        _payload(player_name="Donut", companion_of="alice@home", relationship="pet"),
    )

    assert room.companion_owner_identity("rex-pid") == "alice@home"
    fields = next(f for n, f in spans if n == "companion.bond_resolved")
    assert fields["relationship"] == "pet"
    assert fields["resolved"] is True


def test_unknown_relationship_fails_closed_and_emits_span(monkeypatch):
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "x-pid",
        _payload(player_name="X", companion_of="alice@home", relationship="overlord"),
    )

    assert room.companion_owner_identity("x-pid") is None  # no pet bond — fail closed
    fields = next(f for n, f in spans if n == "companion.bond_resolved")
    assert fields["resolved"] is False  # loud rejection, not a silent grant


def test_empty_relationship_fails_closed_and_emits_span(monkeypatch):
    # An empty relationship string is just as unknown as a garbage one — it must
    # not silently widen, and the rejection must still be observable.
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "y-pid",
        _payload(player_name="Y", companion_of="alice@home", relationship=""),
    )

    assert room.companion_owner_identity("y-pid") is None
    fields = next(f for n, f in spans if n == "companion.bond_resolved")
    assert fields["resolved"] is False


def test_peer_bond_registered_but_grants_no_owner_view(monkeypatch):
    # A PEER is a known, resolved relationship (span resolved=True) but it is NOT
    # a pet, so it gains no owner-private view.
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "kit-pid",
        _payload(player_name="Kit", companion_of="alice@home", relationship="peer"),
    )

    assert room.companion_owner_identity("kit-pid") is None  # peer != pet
    fields = next(f for n, f in spans if n == "companion.bond_resolved")
    assert fields["resolved"] is True


def test_non_companion_connect_is_a_noop(monkeypatch):
    spans = _capture_spans(monkeypatch)
    room = _room()
    bind_companion_bond(room, "alice-pid", _payload(player_name="Alice"))  # no companion fields
    assert spans == []  # ordinary player connect emits no companion span


def test_blank_companion_of_is_a_noop(monkeypatch):
    # companion_of present but blank is an ordinary connect, not a malformed
    # companion — no bond, no span (distinct from an unknown *relationship*).
    spans = _capture_spans(monkeypatch)
    room = _room()
    bind_companion_bond(
        room, "bob-pid", _payload(player_name="Bob", companion_of="   ", relationship="pet")
    )
    assert room.companion_owner_identity("bob-pid") is None
    assert spans == []
