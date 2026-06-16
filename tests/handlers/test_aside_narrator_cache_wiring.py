"""Aside-rides-the-narrator-cache — handler wiring (playtest 2026-06-07).

The real ``PlayerActionHandler`` aside branch must route by what the
session's orchestrator exposes:

* stash + tooling client present → ``resolve_aside_on_narrator_cache``
  against the narrator's exact cached artifacts; the ``aside.resolve``
  span carries ``path="narrator_cache"``, the ``cache_hit`` lie-detector,
  ``cache_read_tokens``, and the stash's model — and the legacy
  ``build_aside_llm`` factory is NEVER constructed;
* no stash (no SDK turn yet / non-tooling backend) → the legacy thin
  read-view path, logged ``aside.legacy_read_view``, span
  ``path="legacy_read_view"``.

Both legs stay out-of-band: no turn record, no world advance, broadcast
to the whole table — re-asserted here so the new branch can't regress
the ADR-107 guarantees the centerpiece test pins for the legacy leg.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import sidequest.agents.llm_factory as _llm_factory
from sidequest.agents.aside_resolver import AsidePromptStash
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    ToolDefinition,
    ToolingResult,
)
from sidequest.protocol.enums import MessageType
from tests.handlers._harness import (
    StubOrchestrator,
    fake_aside_llm,
    make_mp_room,
    submit,
)

_WORLD_SLUG_MP = "test_world-mp"
_ANSWER_JSON = (
    '{"answer":"Mid-afternoon, the 18th.","outcome":"answered","grounded_on":["calendar"]}'
)
_LEGACY_JSON = '{"answer":"Knee-deep.","outcome":"answered","grounded_on":["region.water_depth"]}'


class _RecordingToolingClient:
    """ToolingLlmClient-shaped fake the narrator-cache path rides."""

    def __init__(self, *, cache_read: int) -> None:
        self._cache_read = cache_read
        self.calls: list[dict[str, Any]] = []

    async def complete_with_tools(self, *args: Any, **kwargs: Any) -> ToolingResult:
        self.calls.append({"args": args, **kwargs})
        return ToolingResult(
            text=_ANSWER_JSON,
            stop_reason="end_turn",
            input_tokens=27_400,
            output_tokens=40,
            cached_input_read_tokens=self._cache_read,
            cached_input_write_tokens=0,
            model="claude-sonnet-4-6",
        )


def _stash() -> AsidePromptStash:
    return AsidePromptStash(
        system_blocks=[CacheableBlock(text="stable narrator prefix", cache=True)],
        tools=[
            ToolDefinition(
                name="roll_dice",
                description="Roll dice.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        model="claude-sonnet-4-6",
    )


@pytest.mark.asyncio
async def test_stash_present_routes_aside_through_narrator_cache() -> None:
    client = _RecordingToolingClient(cache_read=27_000)
    stash = _stash()
    room = make_mp_room(
        players=["Carl", "Donut", "Katia"],
        llm_aside=fake_aside_llm(_LEGACY_JSON),
        orchestrator=StubOrchestrator(aside_prompt_stash=stash, aside_cache_client=client),
    )
    try:
        # Tripwire: the narrator-cache leg must never construct the legacy
        # Haiku adapter (that would be a second, dark spender).
        def _forbidden_build(**_kwargs: Any) -> Any:
            raise AssertionError(
                "narrator-cache aside path must not construct the legacy aside LLM"
            )

        _llm_factory.build_aside_llm = _forbidden_build  # type: ignore[assignment]

        nlog_before = room.narrative_log_count()
        turn_before = room.turn_round()

        out = await submit(room, "Katia", "what day is it?", aside=True)

        # The table saw the narrator-cache answer.
        assert out and out[0].type == MessageType.ASIDE_ANSWER
        assert out[0].payload.answer == "Mid-afternoon, the 18th."
        assert room.last_broadcast_recipients() == {"Carl", "Donut", "Katia"}

        # The tooling client was hit with the stash's exact artifacts and
        # the per-session id (ADR-134 same-pot keying).
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["args"][0] is stash.system_blocks
        assert call["args"][2] is stash.tools
        assert call["tool_choice"] == {"type": "none"}
        assert call["session_id"] == _WORLD_SLUG_MP

        # Span lie-detector: path, cache_hit, model.
        attrs = room.span_attributes("aside.resolve")
        assert attrs["path"] == "narrator_cache"
        assert attrs["cache_hit"] is True
        assert attrs["cache_read_tokens"] == 27_000
        assert attrs["model"] == "claude-sonnet-4-6"
        assert attrs["outcome"] == "answered"

        # Out-of-band guarantees hold on this leg too.
        assert room.narrative_log_count() == nlog_before
        assert room.turn_round() == turn_before
        assert room.world_patch_count() == 0
        assert not room.barrier_fired()
    finally:
        room.teardown()


@pytest.mark.asyncio
async def test_no_stash_falls_back_to_legacy_read_view(
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = make_mp_room(
        players=["Carl", "Donut", "Katia"],
        llm_aside=fake_aside_llm(_LEGACY_JSON),
        # Default StubOrchestrator: stash None, client None — pre-first-SDK-turn.
    )
    try:
        with caplog.at_level(logging.INFO, logger="sidequest.handlers.player_action"):
            out = await submit(room, "Katia", "can I wade across?", aside=True)

        assert out and out[0].type == MessageType.ASIDE_ANSWER
        assert out[0].payload.answer == "Knee-deep."

        attrs = room.span_attributes("aside.resolve")
        assert attrs["path"] == "legacy_read_view"
        assert attrs["model"] == "haiku"
        assert "cache_hit" not in attrs  # narrator-cache-only attribute

        assert any(
            "aside.legacy_read_view" in r.getMessage()
            and "no_narrator_prompt_stash" in r.getMessage()
            for r in caplog.records
        ), "fallback must log aside.legacy_read_view with its reason"
    finally:
        room.teardown()
