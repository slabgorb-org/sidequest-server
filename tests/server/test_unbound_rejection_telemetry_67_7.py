"""Story 67-7 (AC5) — telemetry must distinguish a genuine session-unbound
rejection from ordinary reconnect churn so the GM panel is a lie-detector.

Background (context-story-67-7.md): a duplicate-socket reconnect loop strands
a solo confrontation in ``_State.AwaitingConnect``; each beat commit fires a
``DICE_THROW`` (and the logs show ``PLAYER_ACTION`` / ``ORBITAL_INTENT`` too)
that the handler hard-rejects. Today the only trace of that rejection is a bare
``logger.info("session.message_rejected_unbound ...")`` — invisible to the GM
panel, and indistinguishable from harmless transport churn.

AC5: *a span/log distinguishes genuine-unbound rejection from reconnect churn.*

Per CLAUDE.md OTEL Observability Principle ("the GM panel is the lie detector;
if a subsystem isn't emitting OTEL spans you can't tell whether it's engaged"),
the rejection must surface as a **watcher event** (``publish_event``), not just a
logger call the dashboard never sees. The event must carry enough structure for
the panel to classify the rejection:

  * the rejected message type (DICE_THROW / PLAYER_ACTION / ORBITAL_INTENT),
  * the session state at rejection time (AwaitingConnect),
  * a discriminator marking this as a *genuine* session-unbound rejection
    (the ``session_unbound`` recovery class) — so the panel can tell it apart
    from reconnect churn, which would not carry that classification.

These tests are RED today: the reject paths
(``handlers/dice_throw.py:51-65``, ``handlers/player_action.py``,
``handlers/orbital_intent.py:37-49``) emit no watcher event at all.

NOTE (test design, see TEA assessment): the *churn-side* of the discriminator
(detecting a spurious reconnect cycle) depends on the root cause that AC1 says
requires a live repro, and on the AC4 angle decision. These tests therefore pin
only the determinable half — that the genuine-unbound rejection emits a
structured, panel-visible event carrying the ``session_unbound`` classification.
The reconnect-loop reproduction / regression coverage (AC2/AC3/AC6) is gated on
that diagnosis and is flagged as a blocking delivery finding rather than
fabricated here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    DiceThrowMessage,
    OrbitalIntent,
    OrbitalIntentMessage,
    PlayerActionMessage,
    PlayerActionPayload,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import _State


def _unbound_session() -> MagicMock:
    """Fresh handler stranded in AwaitingConnect — never (re)bound.

    ``_room`` is set to ``None`` so the ORBITAL_INTENT handler (which guards on
    ``room is None``) also takes its unbound-rejection branch.
    """
    session = MagicMock()
    session._state = _State.AwaitingConnect
    session._session_data = None
    session._room = None
    return session


def _capture_publish_event(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on the watcher hub's ``publish_event`` at the source module.

    Handlers import the watcher entrypoint at call time
    (``from sidequest.telemetry.watcher_hub import publish_event``, the same
    seam the dogfight path uses), so patching the source attribute captures the
    call regardless of which handler emits it.
    """
    calls: list[dict[str, Any]] = []

    def _spy(
        event_type: str,
        fields: dict[str, Any],
        *,
        component: str = "sidequest-server",
        severity: str = "info",
        **kwargs: Any,
    ) -> None:
        calls.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    monkeypatch.setattr("sidequest.telemetry.watcher_hub.publish_event", _spy)
    return calls


def _flatten_values(fields: dict[str, Any]) -> str:
    """Lowercased concatenation of a fields dict's keys+values for token search.

    Lets the assertions check that a discriminator/state/type token is present
    *somewhere* in the published structure without coupling to an exact field
    name the Dev hasn't chosen yet (AC4 is undecided)."""
    return " ".join(f"{k} {v}" for k, v in fields.items()).lower()


def _unbound_rejection_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Watcher events that classify a genuine session-unbound rejection.

    The discriminator that separates a genuine unbound rejection from reconnect
    churn is the ``session_unbound`` recovery class — the same token the
    protocol already uses on the ERROR frame (``code="session_unbound"``)."""
    return [c for c in calls if "session_unbound" in _flatten_values(c["fields"])]


# ---------------------------------------------------------------------------
# AC5 — DICE_THROW (the canonical "beat commit mid-confrontation" frame)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dice_throw_unbound_emits_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.handlers.dice_throw import HANDLER

    calls = _capture_publish_event(monkeypatch)
    session = _unbound_session()
    msg = DiceThrowMessage(
        type=MessageType.DICE_THROW,
        payload=DiceThrowPayload(
            request_id="req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 0.0, 0.0),
                angular=(0.0, 0.0, 0.0),
                position=(0.0, 0.0),
            ),
            face=[6, 6, 6],
            beat_id="attack",
        ),
        player_id="p1",
    )

    outbound = await HANDLER.handle(session, msg)

    # The rejection itself is correct and stays loud.
    assert outbound[0].type == "ERROR"
    assert outbound[0].payload.code == "session_unbound"

    # AC5: it must ALSO surface to the GM panel as a structured watcher event.
    assert calls, (
        "DICE_THROW rejected in AwaitingConnect must emit a watcher event "
        "(publish_event) — a bare logger.info is invisible to the GM panel, "
        "so the panel can't tell a genuine guard from transport noise. "
        "OTEL Observability Principle / AC5."
    )
    rejections = _unbound_rejection_calls(calls)
    assert rejections, (
        "the watcher event must carry the 'session_unbound' classification so "
        "the GM panel can distinguish a genuine unbound rejection from "
        "reconnect churn (AC5)"
    )
    flat = _flatten_values(rejections[0]["fields"])
    assert "dice_throw" in flat, (
        "the rejection event must record which frame type was rejected "
        f"(expected DICE_THROW); fields were {rejections[0]['fields']!r}"
    )
    assert "awaitingconnect" in flat, (
        "the rejection event must record the session state at rejection time "
        f"(expected AwaitingConnect); fields were {rejections[0]['fields']!r}"
    )


# ---------------------------------------------------------------------------
# AC5 — PLAYER_ACTION (the "action frame" half of the story title)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_player_action_unbound_emits_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.handlers.player_action import HANDLER

    calls = _capture_publish_event(monkeypatch)
    session = _unbound_session()
    msg = PlayerActionMessage(
        type=MessageType.PLAYER_ACTION,
        payload=PlayerActionPayload(action=NonBlankString("fire the broadside"), round=0),
        player_id="p1",
    )

    outbound = await HANDLER.handle(session, msg)

    assert outbound[0].type == "ERROR"
    assert outbound[0].payload.code == "session_unbound"

    rejections = _unbound_rejection_calls(calls)
    assert rejections, (
        "PLAYER_ACTION rejected in AwaitingConnect must emit a watcher event "
        "carrying the 'session_unbound' classification (AC5)"
    )
    flat = _flatten_values(rejections[0]["fields"])
    assert "player_action" in flat, (
        "the rejection event must record the rejected frame type "
        f"(expected PLAYER_ACTION); fields were {rejections[0]['fields']!r}"
    )
    assert "awaitingconnect" in flat, (
        f"the rejection event must record state=AwaitingConnect; "
        f"fields were {rejections[0]['fields']!r}"
    )


# ---------------------------------------------------------------------------
# AC5 — ORBITAL_INTENT (the logs showed one rejected the same way)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orbital_intent_unbound_emits_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.handlers.orbital_intent import HANDLER

    calls = _capture_publish_event(monkeypatch)
    session = _unbound_session()
    msg = OrbitalIntentMessage(
        payload=OrbitalIntent.model_validate({"kind": "view_map", "scope": "system_root"}),
        player_id="p1",
    )

    outbound = await HANDLER.handle(session, msg)

    assert outbound[0].type == "ERROR"
    assert outbound[0].payload.code == "session_unbound"

    rejections = _unbound_rejection_calls(calls)
    assert rejections, (
        "ORBITAL_INTENT rejected while unbound must emit a watcher event "
        "carrying the 'session_unbound' classification (AC5)"
    )
    flat = _flatten_values(rejections[0]["fields"])
    assert "orbital_intent" in flat, (
        "the rejection event must record the rejected frame type "
        f"(expected ORBITAL_INTENT); fields were {rejections[0]['fields']!r}"
    )


# ---------------------------------------------------------------------------
# AC5 (negative) — the discriminator must be SPECIFIC: a non-unbound rejection
# (e.g. Creating-state, session-data-missing) must NOT be tagged as a genuine
# session-unbound rejection. Otherwise the GM panel can't trust the signal.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_creating_state_rejection_not_tagged_session_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.handlers.player_action import HANDLER

    calls = _capture_publish_event(monkeypatch)
    session = MagicMock()
    session._state = _State.Creating  # valid chargen-time state, NOT unbound
    session._session_data = None
    session._room = None

    msg = PlayerActionMessage(
        type=MessageType.PLAYER_ACTION,
        payload=PlayerActionPayload(action=NonBlankString("pick a name"), round=0),
        player_id="p1",
    )

    outbound = await HANDLER.handle(session, msg)

    assert outbound[0].type == "ERROR"
    # This rejection is a different problem class — it must not borrow the
    # session_unbound recovery classification on the telemetry side either.
    assert outbound[0].payload.code != "session_unbound"
    assert not _unbound_rejection_calls(calls), (
        "a Creating-state / data-missing rejection must NOT emit a "
        "'session_unbound'-classified watcher event — the discriminator is "
        "reserved for the AwaitingConnect recovery case so the GM panel signal "
        "stays trustworthy (AC5)"
    )
