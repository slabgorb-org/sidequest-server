"""Auth-bypass regression net for DICE_THROW (Story 118-9, AC1).

The 118-8 review (The Merovingian, 2026-06-15) flagged ``DiceThrowHandler`` as the
explicit handler ``FateActionHandler`` was modeled on ("mirrors DiceThrowHandler"
docstring) and very likely carrying the SAME seat-spoof idiom 118-8 fixed for Fate.
The audit confirms it — ``handlers/dice_throw.py`` line ~78:

    rolling_player_id = getattr(msg, "player_id", "") or sd.player_id

``msg.player_id`` is inbound, client-controlled, and trusted whenever non-empty.
``rolling_player_id`` then drives the seat-map lookup
(``snapshot.player_seats.get(rolling_player_id)``) that picks WHICH PC the roll
resolves against — so a client can send a DICE_THROW whose ``player_id`` points at
*another seated PC* and the server rolls AS that PC (their stats, their opposed-check
pending actor, their dice result attribution). Same class as the 118-8 HIGH (which
was p1).

The fix (ADR-119, mirroring 118-8): seat resolution is driven by ``sd.player_id`` —
the server-authenticated, Cf-Access identity bound at connect — as the SOLE source;
a non-empty inbound ``msg.player_id`` that DISAGREES is a spoof attempt, surfaced to
the GM panel (the OTEL lie detector) with a ``...player_id_spoof_rejected`` event,
and the roll proceeds as the authenticated PC.

These tests drive the REAL registered ``DiceThrowHandler`` (the production WS entry —
server CLAUDE.md "Every Test Suite Needs a Wiring Test" / "No Source-Text Wiring
Tests") and assert on the values the handler hands to ``dispatch_dice_throw`` (the
seat-resolution boundary) plus the emitted spoof watcher event. ``dispatch_dice_throw``
is monkeypatched to capture its kwargs and short-circuit BEFORE the narration turn —
seat resolution is fully decided by the time it is called, so the capture is the
honest boundary to assert against.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.handlers.dice_throw import HANDLER as DICE_HANDLER
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.messages import DiceThrowMessage
from sidequest.server.dispatch.dice import DiceDispatchError
from sidequest.server.session_handler import _State


def _pc(name: str, stats: dict[str, int]) -> Character:
    core = CreatureCore(name=name, description="d", personality="p")
    return Character(core=core, char_class="Fighter", race="Human", backstory="b", stats=stats)


def _two_pc_dice_session(*, authenticated_player_id: str) -> SimpleNamespace:
    """A fake session seating two player PCs — Donut at seat ``p1`` and Carl at
    seat ``p2`` (the real 2026-05-12 caverns_sunden MP roster the seat-map fix was
    written for). ``player_id`` is the SERVER-authenticated identity bound at
    connect; the wire message carries the spoofable annotation."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Donut", role="lead", side="player"),
            EncounterActor(name="Carl", role="lead", side="player"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="dice_test",
        characters=[_pc("Donut", {"WIS": 14}), _pc("Carl", {"STR": 16})],
        encounter=enc,
    )
    snap.player_seats = {"p1": "Donut", "p2": "Carl"}
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="dial")),
        genre_slug="dice_test",
        world_slug="test_world",
        player_id=authenticated_player_id,
        _room=None,
    )
    # session._room None → the handler builds no room_broadcast; dispatch is
    # captured before that matters anyway.
    return SimpleNamespace(_state=_State.Playing, _session_data=sd, _room=None)


def _dice_throw(player_id: str) -> DiceThrowMessage:
    """A minimal DICE_THROW. ``player_id`` is the inbound, spoofable wire
    annotation; ``face`` / ``throw_params`` only need to construct (the dispatch
    that would consume them is captured)."""
    return DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id="r1",
            throw_params=ThrowParams(
                velocity=(0.0, 4.0, -1.0), angular=(0.5, 0.5, 0.5), position=(0.5, 0.5)
            ),
            face=[15],
        ),
        player_id=player_id,
    )


@pytest.fixture
def captured_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``dispatch_dice_throw`` with a capture that records its kwargs and
    short-circuits via ``DiceDispatchError`` (a message WITHOUT "active encounter"
    so the handler returns the plain error and never reaches the stale-encounter
    resync or the inline narration turn). Seat resolution is already fully decided
    when ``dispatch_dice_throw`` is called — its ``rolling_player_id`` /
    ``character_name`` kwargs ARE the boundary under test."""
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)
        raise DiceDispatchError("captured — short-circuit before narration")

    from sidequest.server.dispatch import dice as dice_mod

    monkeypatch.setattr(dice_mod, "dispatch_dice_throw", _capture)
    return captured


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture watcher events. The spoof-rejection event is emitted via a
    function-local ``from sidequest.telemetry.watcher_hub import publish_event``
    (mirroring fate_action.py), so patching the source-module attribute catches it
    at call time."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.telemetry import watcher_hub as hub_mod

    monkeypatch.setattr(hub_mod, "publish_event", _capture)
    yield captured


def _spoof_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spoof-rejection events, identified by a ``spoof`` token in the ``op`` field
    (decoupled from the exact op spelling the Dev chooses, but tied to the
    fate_action-style shape the AC mandates)."""
    return [e for e in events if "spoof" in str(e["fields"].get("op", ""))]


# --------------------------------------------------------------------------- #
# AC1 — seat resolution is driven by the authenticated identity (sole source)
# --------------------------------------------------------------------------- #


def test_spoofed_player_id_rolls_as_authenticated_pc_not_victim(
    captured_dispatch: dict[str, Any],
) -> None:
    """Authenticated as Donut (seat p1) but the wire message spoofs player_id=p2
    (Carl's seat). Per ADR-119 the roll MUST resolve against Donut — the
    authenticated PC — because ``sd.player_id`` is the sole identity source. RED
    today: the handler trusts the non-empty ``msg.player_id`` and resolves Carl."""
    session = _two_pc_dice_session(authenticated_player_id="p1")

    asyncio.run(DICE_HANDLER.handle(session, _dice_throw("p2")))

    assert captured_dispatch["character_name"] == "Donut"
    assert captured_dispatch["rolling_player_id"] == "p1"


def test_spoofed_player_id_never_rolls_as_victim(captured_dispatch: dict[str, Any]) -> None:
    """Security invariant, robust to either fix shape (act-as-authenticated OR
    reject-loud): a spoofed inbound player_id must NEVER cause the roll to resolve
    against the victim's seat. RED today (the roll resolves as Carl)."""
    session = _two_pc_dice_session(authenticated_player_id="p1")

    asyncio.run(DICE_HANDLER.handle(session, _dice_throw("p2")))

    assert captured_dispatch["character_name"] != "Carl"
    assert captured_dispatch["rolling_player_id"] != "p2"


def test_spoofed_player_id_emits_spoof_watcher_event(
    captured_dispatch: dict[str, Any], captured_events: list[dict[str, Any]]
) -> None:
    """OTEL lie-detector (server CLAUDE.md OTEL Observability Principle / AC1: "a
    fate_action-style spoof watcher event"). A disagreeing inbound player_id must
    surface to the GM panel carrying both the inbound (spoofed) and authenticated
    identities, at warning severity. RED today: no spoof detection, no event."""
    session = _two_pc_dice_session(authenticated_player_id="p1")

    asyncio.run(DICE_HANDLER.handle(session, _dice_throw("p2")))

    spoof = _spoof_events(captured_events)
    assert len(spoof) == 1, "exactly one spoof-rejection event expected"
    assert spoof[0]["fields"]["inbound_player_id"] == "p2"
    assert spoof[0]["fields"]["authenticated_player_id"] == "p1"
    assert spoof[0]["severity"] == "warning"


# --------------------------------------------------------------------------- #
# Regression guards — the fix must NOT break legitimate seat attribution
# --------------------------------------------------------------------------- #


def test_authenticated_player_rolls_as_own_seat_not_characters_zero(
    captured_dispatch: dict[str, Any],
) -> None:
    """Legitimately authenticated as Carl (seat p2). The roll must resolve against
    Carl — NOT silently fall back to characters[0] (Donut). Guards against a fix
    that over-corrects by ignoring the seat map entirely. GREEN now and after."""
    session = _two_pc_dice_session(authenticated_player_id="p2")

    asyncio.run(DICE_HANDLER.handle(session, _dice_throw("p2")))

    assert captured_dispatch["character_name"] == "Carl"
    assert captured_dispatch["rolling_player_id"] == "p2"


def test_empty_inbound_player_id_resolves_to_authenticated_seat(
    captured_dispatch: dict[str, Any],
) -> None:
    """A wire message with an empty player_id (the common case — the client does
    not self-identify) must resolve to the authenticated seat (p1 → Donut), not
    error or mis-seat. Guards the fallback path the fix must preserve. GREEN now
    and after."""
    session = _two_pc_dice_session(authenticated_player_id="p1")

    asyncio.run(DICE_HANDLER.handle(session, _dice_throw("")))

    assert captured_dispatch["character_name"] == "Donut"
    assert captured_dispatch["rolling_player_id"] == "p1"


def test_matching_inbound_player_id_emits_no_spoof_event(
    captured_dispatch: dict[str, Any], captured_events: list[dict[str, Any]]
) -> None:
    """A client that self-identifies with its OWN authenticated seat (inbound p1 ==
    authenticated p1) is NOT a spoof — no warning event may fire. Guards against a
    fix that flags every self-identified roll. GREEN now and after."""
    session = _two_pc_dice_session(authenticated_player_id="p1")

    asyncio.run(DICE_HANDLER.handle(session, _dice_throw("p1")))

    assert _spoof_events(captured_events) == []
