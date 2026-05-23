"""ADR-107 — mandatory MP wiring test (RED, story 50-25). THE CENTERPIECE.

Plan: docs/superpowers/plans/2026-05-17-aside-channel.md Task 5.
Spec: docs/superpowers/specs/2026-05-17-aside-channel-design.md §7.

Drives the REAL ``PlayerActionHandler.handle()`` against a real
``SessionRoom`` (MULTIPLAYER) + real ``GameSnapshot`` + ``TurnManager``
+ ``SqliteStore`` + loaded genre pack, asserting ALL SEVEN out-of-band
guarantees — the wiring test that proves the aside channel is connected
end-to-end (CLAUDE.md "Every Test Suite Needs a Wiring Test"), not
merely that the resolver works in isolation.

The aside path is exercised 100% real. Only the orthogonal narrator is
stubbed: ``tests/handlers/_harness.py``'s ``_StubSession`` skips the LLM
prose but performs the *real* ``turn_manager.record_interaction()`` the
production narrator does after a barrier fires, so round-advance stays
faithful. The plan's Task 5 "factor from a sibling MP fixture" premise
did not hold (every handler test is ``MagicMock``-based); the harness
builds the real objects from scratch instead — see its module docstring.
"""

import pytest

from sidequest.protocol.enums import MessageType
from tests.handlers._harness import (
    fake_aside_llm,
    make_mp_room,
    raising_aside_llm,
    submit,
)


@pytest.mark.asyncio
async def test_aside_is_out_of_band_in_mp():
    room = make_mp_room(
        players=["Carl", "Donut", "Katia"],
        llm_aside=fake_aside_llm(
            '{"answer":"Knee-deep — wade, no carry.","outcome":"answered",'
            '"grounded_on":["character.size","region.water_depth"]}'
        ),
    )

    nlog_before = room.narrative_log_count()
    scrap_before = room.scrapbook_count()
    turn_before = room.turn_round()

    # Carl submits a real action; Katia fires an aside mid-round; Donut pending.
    await submit(room, "Carl", "I open the door", aside=False)
    aside_out = await submit(room, "Katia", "can I wade or must I be carried?", aside=True)

    # (1)(2)(3)(4) no turn record / no world advance
    assert room.narrative_log_count() == nlog_before
    assert room.scrapbook_count() == scrap_before
    assert room.turn_round() == turn_before
    assert room.world_patch_count() == 0

    # (5) barrier still waiting on Katia's real action + Donut unaffected
    assert not room.barrier_fired()
    assert room.pending_player_ids() == {"Katia", "Donut"}  # Carl submitted

    # (6) ASIDE_ANSWER broadcast to ALL seats (table-visible, spec §5)
    assert aside_out and aside_out[0].type == MessageType.ASIDE_ANSWER
    assert room.last_broadcast_recipients() == {"Carl", "Donut", "Katia"}

    # (7) the aside.resolve span fired (the lie-detector, CLAUDE.md OTEL)
    assert room.spans_named("aside.resolve")

    # Katia now submits her real action -> barrier fires normally; the aside
    # did not pay her turn debt.
    await submit(room, "Katia", "I wade in", aside=False)
    await submit(room, "Donut", "I follow", aside=False)
    assert room.barrier_fired()
    assert room.turn_round() == turn_before + 1

    room.teardown()


@pytest.mark.asyncio
async def test_empty_aside_after_combat_strip_is_rejected_no_resolver_no_span():
    """Spec §6: empty/whitespace aside text -> typed ERROR, no resolver,
    no span. (TEA Delivery Finding Gap — empty-aside path was untested.)

    ``PlayerActionPayload.action`` is ``NonBlankString`` so the empty
    case is reached the only way production can: combat-bracket-only
    aside text that strips to "" (``"[combat]"`` -> "").
    """
    room = make_mp_room(
        players=["Carl", "Donut", "Katia"],
        llm_aside=fake_aside_llm('{"answer":"x","outcome":"answered","grounded_on":["a"]}'),
    )
    nlog_before = room.narrative_log_count()
    turn_before = room.turn_round()

    out = await submit(room, "Katia", "[combat]", aside=True)

    # Typed ERROR back to the asker; resolver/LLM never invoked, no span,
    # no broadcast, no turn record.
    assert out and out[0].type == MessageType.ERROR
    assert not room.spans_named("aside.resolve")
    assert room.last_broadcast_recipients() == set()
    assert room.narrative_log_count() == nlog_before
    assert room.turn_round() == turn_before  # unchanged (no advance)
    assert not room.barrier_fired()

    room.teardown()


@pytest.mark.asyncio
async def test_aside_llm_failure_does_not_crash_handler_no_turn_lost():
    """Reviewer HIGH + spec §6 end-to-end: an LLM call failure/timeout must
    NOT propagate out of the real PLAYER_ACTION handler. The aside degrades
    to a graceful resolver_error answer, the span still fires, and — the
    §6 promise — no turn is lost (the asker still owes their real action).

    RED: today the raise escapes `AsideResolver.resolve()` and `submit()`
    raises. Green once Dev catches the call-failure classes.
    """
    room = make_mp_room(
        players=["Carl", "Donut", "Katia"],
        llm_aside=raising_aside_llm(TimeoutError("anthropic timed out")),
    )
    nlog_before = room.narrative_log_count()
    turn_before = room.turn_round()

    # Must not raise — the handler has to contain the LLM failure.
    out = await submit(room, "Katia", "can I wade or must I be carried?", aside=True)

    # Graceful degradation: a table-visible ASIDE_ANSWER carrying the loud
    # "ask again" resolver_error answer (NOT a crash, NOT silent).
    assert out and out[0].type == MessageType.ASIDE_ANSWER
    assert out[0].payload.answer  # non-empty loud message
    assert room.spans_named("aside.resolve")  # the lie-detector still fires
    # §6: no turn lost — world/turn untouched, barrier still owed.
    assert room.narrative_log_count() == nlog_before
    assert room.turn_round() == turn_before
    assert not room.barrier_fired()
    assert "Katia" in room.pending_player_ids()  # still owes a real action

    room.teardown()
