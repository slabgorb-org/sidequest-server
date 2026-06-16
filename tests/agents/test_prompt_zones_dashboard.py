"""Wiring test for the dashboard Prompt-tab zone breakdown.

The bug surfaced in playtest 2026-04-30: the Prompt tab's Zone Breakdown
panel rendered an empty body because the ``prompt_assembled`` watcher
event was shipping only flat aggregates and not the per-zone array the
dashboard's bar chart at ``static/dashboard.html:802`` expected. Fix
landed in sidequest-server@2abfd8b — extending the publish payload to
include ``zones: [{zone, total_tokens, sections: [{name, token_estimate,
category}]}]``.

This test guards the contract per CLAUDE.md "Every Test Suite Needs a
Wiring Test" — drives a real ``build_narrator_prompt`` through the
orchestrator and asserts the published event carries the ``zones``
array in the shape the dashboard reads. A future refactor that strips
zones, drops turn_number, or re-keys zones with lowercase names will
fail here before it lands in front of Sebastien on the GM panel.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import (
    Orchestrator,
    TurnContext,  # noqa: F401 — used via fixture type annotation
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


class _CannedClient:
    """Minimal LlmClient — `build_narrator_prompt` doesn't actually call
    the LLM, but the Orchestrator constructor wants a client."""

    async def send(self, prompt: str, **_: Any) -> ClaudeResponse:
        return ClaudeResponse(text="ok", duration_ms=0)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


@pytest.mark.asyncio
async def test_build_narrator_prompt_publishes_zones_for_dashboard(
    bound_hub: WatcherHub,
) -> None:
    """The `prompt_assembled` event must carry a non-empty `zones` array
    matching the dashboard's contract: each zone has `{zone, total_tokens,
    sections}`, each section has `{name, token_estimate, category}`,
    zone names are the PascalCase `ZONE_COLORS` keys."""
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    orch = Orchestrator(client=_CannedClient())
    context = TurnContext(
        character_name="Kael",
        state_summary="You are in a tavern.",
        turn_number=0,
    )
    await orch.build_narrator_prompt("look around", context)
    await asyncio.sleep(0.05)

    prompt_events = [e for e in sock.events if e.get("event_type") == "prompt_assembled"]
    assert len(prompt_events) == 1
    fields = prompt_events[0]["fields"]

    # turn_number must be on the wire even when 0 — the dashboard fix
    # in 2abfd8b uses `??` instead of `||`, which only kicks in for
    # null/undefined. If turn_number ever stops being published, the
    # dashboard regresses to 'T?' silently.
    assert "turn_number" in fields
    assert fields["turn_number"] == 0

    zones = fields["zones"]
    assert isinstance(zones, list)
    assert zones, "zones must be non-empty for a real prompt build"

    pascal_zone_names = {"Primacy", "Early", "Valley", "Late", "Recency"}
    for z in zones:
        assert {"zone", "total_tokens", "sections"} <= set(z)
        assert z["zone"] in pascal_zone_names, (
            f"zone name '{z['zone']}' must be PascalCase to match the dashboard's ZONE_COLORS map"
        )
        assert isinstance(z["total_tokens"], int)
        assert z["total_tokens"] >= 0
        assert z["sections"], "every zone in the payload must have sections"
        for s in z["sections"]:
            assert {"name", "token_estimate", "category"} <= set(s)
            assert isinstance(s["token_estimate"], int)
            assert s["token_estimate"] >= 0


@pytest.mark.asyncio
async def test_prompt_assembled_event_has_split_fields(
    bound_hub: WatcherHub,
    simple_turn_context: TurnContext,
) -> None:
    """ADR-098: prompt_assembled carries system_len, user_len, bounded; no tier."""

    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    orch = Orchestrator(client=_CannedClient())
    await orch.build_narrator_prompt("look", simple_turn_context)
    await asyncio.sleep(0.05)

    prompt_events = [e for e in sock.events if e.get("event_type") == "prompt_assembled"]
    assert prompt_events, "no prompt_assembled event published"
    payload = prompt_events[-1]["fields"]

    assert "system_len" in payload
    assert "user_len" in payload
    assert payload.get("bounded") is True
    assert "tier" not in payload
