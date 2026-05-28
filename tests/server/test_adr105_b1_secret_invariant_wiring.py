"""ADR-105 B1 — secret-routing CoreInvariant: production-path wiring test.

CLAUDE.md "Every Test Suite Needs a Wiring Test": the unit tests in
``tests/game/projection/test_core_invariants.py`` prove the invariant
branch in isolation. This proves the *chain* used in production:

    build_secret_note_events (production builder)
        → ComposedFilter (production filter, NO genre rules — the
          firewall must not depend on a pack's projection.yaml)
        → _project_frames (the exact per-recipient fan-out helper
          emit_event calls)

It is the test that would have caught the 2026-05-16 caverns_sunden
leak: a non-recipient's projected frame for a redacted dispatch must be
``include=False`` with ``rule.source == "invariant:visibility_gated"``,
and the ``invariant.secret_routed`` watcher event (the firewall's
lie-detector) must fire once per distinct recipient.

Regression anchor: before B1, ``TARGETED_KINDS["SECRET_NOTE"]="to"``
read a ``to`` field ``SecretNotePayload`` never carries, so EVERY
non-GM player resolved ``include=False`` — the channel was dead for
players (the legitimate recipient could not receive it either).
"""

from __future__ import annotations

import json

import pytest

from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag
from sidequest.server.session_handler import (
    _project_frames,
    build_secret_note_events,
)


def _view() -> SessionGameStateView:
    return SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={
            "player:Alice": "alice_char",
            "player:Bob": "bob_char",
        },
    )


def _redacted_dispatch(actor: str) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="arcane_probe",
        params={"reading": "no ward-heat"},
        idempotency_key="k1",
        confidence=1.0,
        visibility=VisibilityTag(
            visible_to=[actor],
            perception_fidelity={},
            secrets_for=[actor],
            redact_from_narrator_canonical=True,
        ),
    )


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture watcher_hub.publish_event payloads deterministically.

    ``_publish_secret_routed`` imports ``publish_event`` from
    ``watcher_hub`` at call time, so patching the module attribute
    intercepts it without needing a bound event loop.
    """
    events: list[dict] = []

    def _fake_publish(event_type: str, fields: dict, **kwargs: object) -> None:
        events.append({"event_type": event_type, "fields": fields, "kwargs": kwargs})

    import sidequest.telemetry.watcher_hub as wh

    monkeypatch.setattr(wh, "publish_event", _fake_publish)
    return events


def test_redacted_dispatch_excludes_non_recipient_through_production_path(
    captured_watcher_events: list[dict],
) -> None:
    # Production builder turns the redacted dispatch into a SECRET_NOTE
    # envelope carrying _visibility.visible_to (NO `to` field).
    [envelope] = build_secret_note_events([_redacted_dispatch("player:Alice")], turn_id="g:w:p:7")
    assert envelope.kind == "SECRET_NOTE"
    assert "to" not in json.loads(envelope.payload_json)

    # Pack-independent: no genre rules at all. The firewall is structural.
    filt = ComposedFilter(rules=load_rules_from_yaml_str("rules: []"))

    decisions = dict(
        _project_frames(
            envelope=envelope,
            projection_filter=filt,
            connected_players=["player:Alice", "player:Bob"],
            view=_view(),
        )
    )

    # Recipient receives the canonical note.
    assert decisions["player:Alice"].include is True
    assert json.loads(decisions["player:Alice"].payload_json)["subsystem"] == "arcane_probe"

    # Non-recipient is firewalled — empty payload, not the leaked note.
    assert decisions["player:Bob"].include is False
    assert decisions["player:Bob"].payload_json == ""

    # Lie-detector fired once per distinct recipient with the structural
    # source — the GM panel can prove Bob was excluded.
    secret_routed = [
        e for e in captured_watcher_events if e["fields"].get("field") == "invariant.secret_routed"
    ]
    assert len(secret_routed) == 2
    by_player = {e["fields"]["player_id"]: e["fields"] for e in secret_routed}
    assert by_player["player:Alice"]["included"] is True
    assert by_player["player:Bob"]["included"] is False
    assert all(f["source"] == "invariant:visibility_gated" for f in by_player.values())
    assert all(f["malformed"] is False for f in by_player.values())
    assert all(e["kwargs"].get("component") == "projection" for e in secret_routed)


def test_gm_sees_redacted_dispatch_through_production_path() -> None:
    """The GM (lie-detector) must see every secret canonically — the GM
    short-circuit precedes the visibility gate.
    """
    [envelope] = build_secret_note_events([_redacted_dispatch("player:Alice")], turn_id="g:w:p:7")
    filt = ComposedFilter(rules=load_rules_from_yaml_str("rules: []"))
    [(pid, decision)] = _project_frames(
        envelope=envelope,
        projection_filter=filt,
        connected_players=["gm"],
        view=_view(),
    )
    assert pid == "gm"
    assert decision.include is True
    assert json.loads(decision.payload_json)["subsystem"] == "arcane_probe"
