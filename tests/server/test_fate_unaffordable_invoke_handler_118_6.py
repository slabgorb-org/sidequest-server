"""Story 118-6 rework (Reviewer HIGH #2): an unaffordable invoke must return a
TYPED error, not an uncaught exception.

The 118-6 conflict surface makes invoke-bearing FATE_ACTIONs user-reachable. The
client gates the Invoke button on ``canInvoke()``, but the economy is
SERVER-AUTHORITATIVE and the client's FATE_STATE can lag — a player can submit an
invoke they can no longer pay for (their last fate point already spent, the
FATE_STATE update still in flight). ``ruleset.invoke_aspect`` then raises
``FateEconomyError``.

But ``FateEconomyError`` and ``FateConflictError`` are SIBLING ``ValueError``
subclasses (fate.py:43 / fate_conflict.py:61) — so the handler's
``except FateConflictError`` (fate_action.py:120) does NOT catch it. The error
escapes the handler uncaught instead of returning the graceful typed
``fate_dispatch_error`` the FateConflictError path already provides for every
other rejected Fate action.

RED today: ``FateEconomyError`` escapes ``FATE_HANDLER.handle`` → ``asyncio.run``
re-raises (this test errors).
GREEN: the handler catches it and returns one typed ``ERROR`` message with a
fate-domain code — the same graceful shape as a non-Fate-ruleset / bad-target
rejection.

Mirrors the fixture doubles in ``tests/server/test_fate_action_handler_wiring.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot, Npc
from sidequest.handlers.fate_action import HANDLER as FATE_HANDLER
from sidequest.protocol.fate import FateActionPayload
from sidequest.protocol.messages import ErrorMessage, FateActionMessage
from sidequest.server.session_handler import _State


def _broke_hero() -> Character:
    """A hero who CANNOT pay for an invoke: 0 fate points and an aspect with 0 free
    invocations. ``invoke_aspect`` validates the aspect exists, finds no free invoke,
    then ``spend_fate_point`` raises ``FateEconomyError`` at 0 fate points."""
    core = CreatureCore(
        name="Hero",
        description="d",
        personality="p",
        fate_sheet=FateSheet(
            fate_points=0,
            skills={"Fight": 4},
            aspects=[Aspect(text="Cornered", kind="trouble", free_invokes=0)],
        ),
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _fate_session(snap: GameSnapshot) -> SimpleNamespace:
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        genre_slug="fate_test",
        world_slug="test_world",
        player_id="p1",
    )
    return SimpleNamespace(_state=_State.Playing, _session_data=sd)


def test_unaffordable_invoke_returns_typed_error_not_uncaught():
    """RED: an attack invoking an aspect the actor cannot pay for must return a
    typed ``ERROR`` message, not let ``FateEconomyError`` escape the handler. Today
    the handler catches only the sibling ``FateConflictError``, so the economy error
    propagates uncaught — a session-error vector on a stale-FATE_STATE race."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(genre_slug="fate_test", characters=[_broke_hero()], encounter=enc)
    snap.npcs.append(_depleted_thug())
    session = _fate_session(snap)

    msg = FateActionMessage(
        payload=FateActionPayload(
            request_id="r1",
            action="attack",
            skill="Fight",
            target="Thug",
            invoke_aspect="Cornered",  # 0 free invokes + 0 fate points → unaffordable
            invoke_mode="bonus",
        ),
        player_id="p1",
    )

    out = asyncio.run(FATE_HANDLER.handle(session, msg))

    assert len(out) == 1, (
        f"an unaffordable invoke must yield exactly one typed error message; got {out!r}"
    )
    err = out[0]
    assert isinstance(err, ErrorMessage), (
        "an unaffordable invoke must return a typed ErrorMessage, not "
        f"{type(err).__name__} — the handler catches only FateConflictError, so the "
        "sibling FateEconomyError escapes uncaught (Reviewer HIGH #2)"
    )
    assert err.payload.code is not None and "fate" in err.payload.code, (
        "the error must carry a fate-domain code (e.g. 'fate_dispatch_error'), mirroring "
        f"the FateConflictError rejection path; got code={err.payload.code!r}"
    )


def test_affordable_invoke_still_resolves_to_a_roll():
    """GREEN GUARD: a payable invoke (a free invocation available) must STILL drive
    the action to a 4dF roll, not be swept up by the new economy-error handling. Pins
    that the fix narrows to genuine economy failures and does not swallow valid invokes."""
    core = CreatureCore(
        name="Hero",
        description="d",
        personality="p",
        fate_sheet=FateSheet(
            fate_points=0,
            skills={"Fight": 4},
            aspects=[Aspect(text="High Ground", kind="situation", free_invokes=1)],
        ),
    )
    hero = Character(core=core, char_class="Agent", race="Human", backstory="b")
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_depleted_thug())
    session = _fate_session(snap)

    msg = FateActionMessage(
        payload=FateActionPayload(
            request_id="r1",
            action="attack",
            skill="Fight",
            target="Thug",
            invoke_aspect="High Ground",  # 1 free invoke → affordable
            invoke_mode="bonus",
        ),
        player_id="p1",
    )

    out = asyncio.run(FATE_HANDLER.handle(session, msg))

    # A payable invoke reaches the roll and broadcasts a FATE_ROLL (not an error).
    assert len(out) == 1, f"a payable invoke must surface the 4dF roll; got {out!r}"
    assert not isinstance(out[0], ErrorMessage), (
        f"a payable invoke must NOT be reported as an error; got {out[0]!r}"
    )
