"""expand_visibility_for_companions widens owner-private events to bonded pets,
leaves everything else untouched, and emits companion.routed_as_pet (Plan B T4).

Span capture: the helper imports ``_watcher_publish`` function-locally from
session_handler (the emitters↔session_handler import cycle), re-fetching it from
the session_handler module object on each call. So the one live capture point is
``sidequest.server.session_handler._watcher_publish`` (the back-compat re-export
at session_handler.py:51) — that is the correct, and only, patch target.
"""

from __future__ import annotations

import json

from sidequest.game.persistence import GameMode
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.server.emitters import expand_visibility_for_companions
from sidequest.server.session_room import CompanionRelationship, SessionRoom


def _capture_pet_spans(monkeypatch) -> list[tuple[str, dict]]:
    spans: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "sidequest.server.session_handler._watcher_publish",
        lambda name, fields, **_kw: spans.append((name, fields)),
    )
    return spans


def _gated(kind: str, visible_to) -> MessageEnvelope:
    return MessageEnvelope(
        kind=kind,
        payload_json=json.dumps({"text": "psst", "_visibility": {"visible_to": visible_to}}),
        origin_seq=1,
    )


def _room_with_pet() -> SessionRoom:
    room = SessionRoom(slug="companion-expand", mode=GameMode.SOLO)
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("rex-pid", "alice@home", CompanionRelationship.PET)
    return room


def test_pet_added_to_owner_private_narration_segment(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    out = expand_visibility_for_companions(_gated("NARRATION_SEGMENT", ["owner-pid"]), _room_with_pet())
    visible_to = json.loads(out.payload_json)["_visibility"]["visible_to"]
    assert set(visible_to) == {"owner-pid", "rex-pid"}

    routed = next(f for n, f in spans if n == "companion.routed_as_pet")
    assert routed["pet_player_id"] == "rex-pid"
    assert routed["owner_player_id"] == "owner-pid"
    assert routed["kind"] == "NARRATION_SEGMENT"


def test_pet_added_to_owner_private_secret_note(monkeypatch):
    # SECRET_NOTE is the other owner-private kind named in the AC and is also a
    # VISIBILITY_GATED_KIND — the pet must inherit it too.
    spans = _capture_pet_spans(monkeypatch)
    out = expand_visibility_for_companions(_gated("SECRET_NOTE", ["owner-pid"]), _room_with_pet())
    visible_to = json.loads(out.payload_json)["_visibility"]["visible_to"]
    assert set(visible_to) == {"owner-pid", "rex-pid"}
    assert any(n == "companion.routed_as_pet" for n, _ in spans)


def test_hireling_is_not_widened(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    room = SessionRoom(slug="companion-expand-h", mode=GameMode.SOLO)
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("gus-pid", "alice@home", CompanionRelationship.HIRELING)
    env = _gated("NARRATION_SEGMENT", ["owner-pid"])
    out = expand_visibility_for_companions(env, room)
    assert out.payload_json == env.payload_json  # hireling never inherits
    assert all(n != "companion.routed_as_pet" for n, _ in spans)  # exclusion is quiet at OTEL


def test_peer_is_not_widened(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    room = SessionRoom(slug="companion-expand-p", mode=GameMode.SOLO)
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("kit-pid", "alice@home", CompanionRelationship.PEER)
    env = _gated("NARRATION_SEGMENT", ["owner-pid"])
    out = expand_visibility_for_companions(env, room)
    assert out.payload_json == env.payload_json  # peer never inherits
    assert all(n != "companion.routed_as_pet" for n, _ in spans)


def test_pet_with_unresolved_owner_identity_is_not_widened():
    # Pet bonded by identity, but the owner's player_id has no identity mapping —
    # must fail safe to no widening rather than leak on a half-resolved owner.
    room = SessionRoom(slug="companion-expand-u", mode=GameMode.SOLO)
    room.register_companion_bond("rex-pid", "alice@home", CompanionRelationship.PET)
    env = _gated("NARRATION_SEGMENT", ["owner-pid"])
    out = expand_visibility_for_companions(env, room)
    assert out.payload_json == env.payload_json


def test_no_pet_means_unchanged_envelope():
    room = SessionRoom(slug="companion-expand-n", mode=GameMode.SOLO)
    room.set_player_identity("owner-pid", "alice@home")  # no companion bonds
    env = _gated("NARRATION_SEGMENT", ["owner-pid"])
    out = expand_visibility_for_companions(env, room)
    assert out.payload_json == env.payload_json


def test_pet_already_present_is_not_duplicated(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    env = _gated("NARRATION_SEGMENT", ["owner-pid", "rex-pid"])
    out = expand_visibility_for_companions(env, _room_with_pet())
    visible_to = json.loads(out.payload_json)["_visibility"]["visible_to"]
    assert visible_to.count("rex-pid") == 1  # no duplicate recipient
    assert all(n != "companion.routed_as_pet" for n, _ in spans)  # nothing added -> no span


def test_all_sentinel_is_left_untouched():
    env = _gated("NARRATION_SEGMENT", "all")
    out = expand_visibility_for_companions(env, _room_with_pet())
    assert json.loads(out.payload_json)["_visibility"]["visible_to"] == "all"


def test_non_gated_kind_is_left_untouched():
    env = MessageEnvelope(kind="NARRATION", payload_json=json.dumps({"text": "hi"}), origin_seq=1)
    out = expand_visibility_for_companions(env, _room_with_pet())
    assert out is env


def test_none_room_is_noop():
    env = _gated("NARRATION_SEGMENT", ["owner-pid"])
    assert expand_visibility_for_companions(env, None) is env
