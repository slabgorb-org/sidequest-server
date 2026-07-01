"""Story 160-4 (RED): a bonded animal companion must be able to join a SOLO
session.

Bug (sq-playtest 2026-06-27): launching a companion (``companion_of`` set,
role: pet) against a SOLO ``beneath_sunden`` room made ``SessionRoom.connect``
raise ``SoloSlotConflict`` — the SOLO-slot guard (which exists to stop the
2026-04-26 "two parallel solo games on one slug" bug) treated a bonded pet
exactly like a second human and rejected it. The connect never reached
``bind_companion_bond`` / the chargen gate; the companion never seated.

Design fork resolved by Keith 2026-07-01: **path (b) ENGINE** — a bonded
companion is NOT a competing solo player, so ``connect`` exempts a
``companion_of``-bearing connect from the guard while still rejecting a genuine
second human. Per the CLAUDE.md OTEL Observability Principle, the exemption
emits a watcher span so the GM panel can prove a pet was seated rather than
rejected.

Contract note (TDD): the exemption is signalled by a ``companion_of`` keyword on
``connect`` — the same owner-identity the connect handshake already carries in
``SessionEventPayload.companion_of`` and passes to ``bind_companion_bond``. Dev
may rename the OTEL literal, but the span MUST fire on the exemption and MUST
disclose the pet's player_id and the owner it is bonded to.
"""

from __future__ import annotations

import pytest

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import SessionRoom, SoloSlotConflict

# The owner identity a companion connect carries (Cf-Access email / dev Host,
# ADR-119) — the pet cannot know the owner's server-minted player_id.
_OWNER = "player1.local"


def test_solo_room_admits_bonded_companion() -> None:
    """AC1: a companion connect (carrying ``companion_of``) joins a SOLO room
    that already holds its human, WITHOUT ``SoloSlotConflict``. A bonded pet is
    not a competing solo player — it must be admitted so the connect can proceed
    to ``bind_companion_bond`` / the chargen gate."""
    room = SessionRoom(slug="2026-06-27-beneath_sunden-solo", mode=GameMode.SOLO)
    room.connect("curly", socket_id="sock-human")

    # This is the crux of the 160-4 bug: today `connect` has no `companion_of`
    # parameter and this raises SoloSlotConflict (the ERROR frame the companion
    # socket saw). After path (b), the bonded pet is admitted.
    room.connect("owl-pid", socket_id="sock-owl", companion_of=_OWNER)

    assert set(room.connected_player_ids()) == {"curly", "owl-pid"}, (
        "a bonded companion must seat alongside its human in a SOLO room, not be "
        "rejected as a second solo player"
    )


def test_solo_room_still_rejects_second_human() -> None:
    """AC2 (preservation guard): the SOLO-slot guard MUST still reject a genuine
    second human — a connect with no ``companion_of``. This is the 2026-04-26
    "two parallel solo games on one slug" bug; exempting companions must not
    reopen it. Green today AND after the fix."""
    room = SessionRoom(slug="solo-guard", mode=GameMode.SOLO)
    room.connect("curly", socket_id="sock-human")

    with pytest.raises(SoloSlotConflict):
        room.connect("intruder", socket_id="sock-intruder")


@pytest.mark.parametrize("blank", ["", "   "])
def test_solo_room_rejects_connect_with_blank_companion_of(blank: str) -> None:
    """AC2 (paranoid): a blank / whitespace ``companion_of`` is an ORDINARY
    player, not a companion — mirroring the handshake's
    ``(payload.companion_of or "").strip()`` treatment in ``bind_companion_bond``
    (empty => ``return  # ordinary player connect``). The guard MUST still fire.

    This catches a naive ``if companion_of is not None:`` exemption that would
    wrongly admit a second human who merely sent an empty ``companion_of``.
    """
    room = SessionRoom(slug="solo-blank", mode=GameMode.SOLO)
    room.connect("curly", socket_id="sock-human")

    with pytest.raises(SoloSlotConflict):
        room.connect("intruder", socket_id="sock-intruder", companion_of=blank)


def test_companion_solo_exemption_emits_otel_span() -> None:
    """AC3 (OTEL lie-detector): the companion-exemption decision MUST emit a
    watcher span so the GM panel can verify a bonded pet was seated in a SOLO
    room rather than rejected. Without the span, the only evidence the exemption
    engaged is the absence of an ERROR frame — invisible to the panel.

    Contract: event name ``companion.solo_exempt``; the payload discloses the
    pet's player_id and the owner identity it is bonded to.
    """
    captured: list[tuple[str, dict, str]] = []

    def fake_publish(name: str, payload: dict, *, component: str = "", **_kw: object) -> None:
        captured.append((name, payload, component))

    import sidequest.telemetry.watcher_hub as _hub

    original = _hub.publish_event
    _hub.publish_event = fake_publish  # type: ignore[assignment]
    try:
        room = SessionRoom(slug="solo-otel", mode=GameMode.SOLO)
        room.connect("curly", socket_id="sock-human")

        captured.clear()  # only inspect events from the companion connect
        room.connect("owl-pid", socket_id="sock-owl", companion_of=_OWNER)
    finally:
        _hub.publish_event = original  # type: ignore[assignment]

    exempt = [c for c in captured if c[0] == "companion.solo_exempt"]
    assert len(exempt) == 1, (
        f"expected exactly one companion.solo_exempt span from the exemption; got {captured}"
    )
    _, payload, component = exempt[0]
    assert payload.get("player_id") == "owl-pid", (
        "the exemption span must name the pet's player_id so the GM panel can "
        f"correlate it to the seated companion; got {payload}"
    )
    assert _OWNER in payload.values(), (
        "the exemption span must disclose the owner identity the pet is bonded "
        f"to (proves the exemption was for a real bond, not a stray connect); got {payload}"
    )
    assert component == "companion", (
        f"a companion decision belongs to the 'companion' watcher component; got {component!r}"
    )
