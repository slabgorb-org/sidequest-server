"""WIRING: a bonded pet receives its owner's private NARRATION_SEGMENT and
SECRET_NOTE through the REAL projection pipeline; a hireling, a peer, and an
unbonded stranger do not. Proves the production widening helper and the
CoreInvariantStage firewall compose correctly (Plan B Task 5).

Fixture-driven behavior + OTEL-span assertion per the server's 'No Source-Text
Wiring Tests' rule (CLAUDE.md). The firewall in CoreInvariantStage is exercised
unmodified — the helper only adds an authorized recipient.
"""

from __future__ import annotations

import json

from sidequest.game.persistence import GameMode
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import SessionGameStateView
from sidequest.server.emitters import expand_visibility_for_companions
from sidequest.server.session_room import CompanionRelationship, SessionRoom


def _capture_pet_spans(monkeypatch) -> list[str]:
    spans: list[str] = []
    sink = lambda name, fields, **_kw: spans.append(name)  # noqa: E731
    monkeypatch.setattr("sidequest.server.emitters._watcher_publish", sink, raising=False)
    monkeypatch.setattr(
        "sidequest.server.session_handler._watcher_publish", sink, raising=False
    )
    return spans


def _owner_private(kind: str) -> MessageEnvelope:
    return MessageEnvelope(
        kind=kind,
        payload_json=json.dumps(
            {"text": "Only Alice senses the trap.", "_visibility": {"visible_to": ["owner-pid"]}}
        ),
        origin_seq=7,
    )


def _included(envelope: MessageEnvelope, player_id: str) -> bool:
    # SessionGameStateView (the concrete view) — GameStateView is a Protocol and
    # cannot be instantiated. with_no_genre_rules() isolates the security firewall.
    decision = ComposedFilter.with_no_genre_rules().project(
        envelope=envelope, view=SessionGameStateView(), player_id=player_id
    )
    return decision.include


def _room() -> SessionRoom:
    room = SessionRoom(slug="companion-wiring", mode=GameMode.SOLO)
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("rex-pid", "alice@home", CompanionRelationship.PET)
    room.register_companion_bond("gus-pid", "alice@home", CompanionRelationship.HIRELING)
    room.register_companion_bond("kit-pid", "alice@home", CompanionRelationship.PEER)
    return room


def test_pet_receives_owner_private_narration_through_real_firewall(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    widened = expand_visibility_for_companions(_owner_private("NARRATION_SEGMENT"), _room())

    assert _included(widened, "owner-pid") is True  # the human still sees it
    assert _included(widened, "rex-pid") is True  # the PET shares the owner's view
    assert _included(widened, "gus-pid") is False  # the HIRELING is excluded
    assert _included(widened, "kit-pid") is False  # the PEER is excluded
    assert _included(widened, "stranger-pid") is False  # an unbonded seat is excluded
    assert "companion.routed_as_pet" in spans  # the firewall decision is observable


def test_pet_receives_owner_private_secret_note_through_real_firewall(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    widened = expand_visibility_for_companions(_owner_private("SECRET_NOTE"), _room())

    assert _included(widened, "rex-pid") is True  # PET shares the owner's secret note
    assert _included(widened, "gus-pid") is False  # hireling excluded
    assert _included(widened, "stranger-pid") is False
    assert "companion.routed_as_pet" in spans
