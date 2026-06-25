"""bind_companion_bond registers a pet bond and emits companion.bond_resolved;
an unknown relationship fails closed (no bond) but still emits a span so the GM
panel sees the rejection rather than a silent grant (Plan B Task 3).

The watcher span is captured by patching the module-level ``_watcher_publish``
alias in connect.py (imported at connect.py:69) — patch where USED, per the
project's monkeypatch convention. The capture keeps the kwargs (incl. severity)
so the fail-closed ``warning`` signal is assertable. The bond EFFECT is verified
through ``pets_of`` — the SAME read path the production fan-out uses.
"""

from __future__ import annotations

from sidequest.game.persistence import GameMode
from sidequest.handlers.connect import bind_companion_bond
from sidequest.protocol.messages import SessionEventPayload
from sidequest.server.session_room import SessionRoom

_OWNER_PID = "owner-pid"
_OWNER_IDENTITY = "alice@home"


def _payload(**kw) -> SessionEventPayload:
    return SessionEventPayload(event="connect", game_slug="abc", **kw)


def _room() -> SessionRoom:
    # The owner's player_id -> identity mapping must exist for pets_of to resolve
    # the owner's bonded pets (the production fan-out read path).
    room = SessionRoom(slug="companion-connect", mode=GameMode.SOLO)
    room.set_player_identity(_OWNER_PID, _OWNER_IDENTITY)
    return room


def _capture_spans(monkeypatch) -> list[tuple[str, dict, dict]]:
    """Capture (name, fields, kwargs) so severity (a kwarg) is assertable."""
    spans: list[tuple[str, dict, dict]] = []
    monkeypatch.setattr(
        "sidequest.handlers.connect._watcher_publish",
        lambda name, fields, **kw: spans.append((name, fields, kw)),
    )
    return spans


def _bond_span(spans) -> tuple[dict, dict]:
    return next((f, kw) for n, f, kw in spans if n == "companion.bond_resolved")


def test_pet_bond_registered_and_span_emitted(monkeypatch):
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "rex-pid",
        _payload(player_name="Donut", companion_of=_OWNER_IDENTITY, relationship="pet"),
    )

    assert room.pets_of(_OWNER_PID) == ["rex-pid"]  # pet widens via the production read path
    fields, kw = _bond_span(spans)
    assert fields["relationship"] == "pet"
    assert fields["resolved"] is True
    assert kw.get("severity") == "info"
    assert "owner_identity" not in fields  # PII (email) must NOT be in telemetry


def test_unknown_relationship_fails_closed_and_emits_span(monkeypatch):
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "x-pid",
        _payload(player_name="X", companion_of=_OWNER_IDENTITY, relationship="overlord"),
    )

    assert room.pets_of(_OWNER_PID) == []  # no pet bond — fail closed
    fields, kw = _bond_span(spans)
    assert fields["resolved"] is False  # loud rejection, not a silent grant
    assert kw.get("severity") == "warning"  # the GM-panel security signal


def test_empty_relationship_fails_closed_and_emits_span(monkeypatch):
    # An empty relationship string is just as unknown as a garbage one — it must
    # not silently widen, and the rejection must still be observable.
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "y-pid",
        _payload(player_name="Y", companion_of=_OWNER_IDENTITY, relationship=""),
    )

    assert room.pets_of(_OWNER_PID) == []
    fields, kw = _bond_span(spans)
    assert fields["resolved"] is False
    assert kw.get("severity") == "warning"


def test_peer_bond_registered_but_grants_no_owner_view(monkeypatch):
    # A PEER is a known, resolved relationship (span resolved=True) but it is NOT
    # a pet, so it gains no owner-private view.
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "kit-pid",
        _payload(player_name="Kit", companion_of=_OWNER_IDENTITY, relationship="peer"),
    )

    assert room.pets_of(_OWNER_PID) == []  # peer != pet — no widening
    fields, kw = _bond_span(spans)
    assert fields["resolved"] is True
    assert kw.get("severity") == "info"


def test_hireling_bond_registered_but_grants_no_owner_view(monkeypatch):
    # A HIRELING is a known, resolved relationship (registered in the bond
    # registry, span resolved=True) but must NEVER widen perception. This is the
    # security-relevant case: a regression handling hirelings as pets in
    # bind_companion_bond would otherwise go undetected.
    spans = _capture_spans(monkeypatch)
    room = _room()

    bind_companion_bond(
        room,
        "gus-pid",
        _payload(player_name="Gus", companion_of=_OWNER_IDENTITY, relationship="hireling"),
    )

    assert room.pets_of(_OWNER_PID) == []  # hireling != pet — no widening
    fields, kw = _bond_span(spans)
    assert fields["resolved"] is True
    assert kw.get("severity") == "info"


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
    assert room.pets_of(_OWNER_PID) == []
    assert spans == []
