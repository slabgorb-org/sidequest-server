"""Story 61-3 — Hard-cap the oversized-prompt canary on the SDK path.

The pre-61-3 canary (`Orchestrator._maybe_emit_oversized_canary`,
`orchestrator.py:2967-2997`) was a SOFT warning: it logged at WARNING
level + published a `prompt_oversized` event with `severity="info"`,
then let the SDK call proceed. The 2026-05-23 incident burned $313
in 48h because that warning scrolled past unread overnight while
the SDK kept billing.

Worse, the canary was wired ONLY into `_run_narration_turn_synchronous`,
NOT into `_run_narration_turn_sdk` — the production default per ADR-101
and the path the incident actually ran. The canary couldn't have fired
in the incident even if its severity had been ERROR.

Story 61-3 promotes the seam to a HARD refuse on the SDK path:

1. When `total_bytes > PROMPT_BUDGET_BYTES_HARD`, the SDK call does
   NOT fire and `run_narration_turn` returns a degraded
   `NarrationTurnResult` (in-fiction stall).
2. The emit is LOUD: `logger.error` (not WARNING) + a NEW watcher
   event `prompt_oversized_hard` with `severity="error"` so the GM
   panel can red-band filter on a stable event name.
3. The wiring gap is closed: the SDK path calls the canary.

See `sprint/context/context-story-61-3.md` for the full design
rationale (decisions A/B/C/D).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

# Importing the tools package wires the 26 adapters onto default_registry,
# matching the production SDK path's expectations.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents import orchestrator as orch_mod
from sidequest.agents.orchestrator import Orchestrator
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)


def _end_turn(text: str = "ok") -> ScriptedResponse:
    """Minimal scripted SDK response — end_turn, tiny token counts."""
    return ScriptedResponse(
        text=text,
        stop_reason="end_turn",
        input_tokens=120,
        output_tokens=18,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )


class _FakeSocket:
    """Minimal `_Sendable` for watcher_hub subscription — collects
    every published event so tests can assert delivery to the GM-panel
    transport (not just `logger.error`)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test event loop and clear subscribers.

    Same pattern as `tests/agents/test_prompt_zones_dashboard.py`."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


# ---------------------------------------------------------------------------
# 1. Refuse the SDK call when over budget; return degraded result.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_prompt_refuses_sdk_call_and_returns_degraded(
    simple_turn_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard cap MUST short-circuit the SDK path.

    With the budget forced below the realistic prompt size, the
    orchestrator must NOT invoke `complete_with_tools` at all
    (`fake.recorded_requests` empty) and must return a degraded
    `NarrationTurnResult` carrying narration text (so the player
    surface doesn't hang).
    """
    monkeypatch.setattr(orch_mod, "PROMPT_BUDGET_BYTES_HARD", 10)

    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    orch = Orchestrator(client=fake)

    result = await orch.run_narration_turn("look around", simple_turn_context)

    assert fake.recorded_requests == [], (
        "Hard cap MUST refuse the SDK call when prompt exceeds budget; "
        f"got {len(fake.recorded_requests)} recorded request(s) — the "
        "model would have been billed."
    )
    assert result.is_degraded is True, (
        "Refused turn must return is_degraded=True so the dispatch "
        "layer surfaces the refusal to the player."
    )
    assert result.narration, (
        "Degraded result must carry narration text (player would otherwise "
        "see an empty turn and assume the client hung)."
    )


# ---------------------------------------------------------------------------
# 2. Under-budget prompts proceed normally — no false positives.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_budget_prompt_proceeds_normally(
    simple_turn_context,
) -> None:
    """At the real default budget (~2MB), a normal turn must reach the SDK.

    Guards against a regression where the hard cap fires on every turn
    (e.g., an off-by-one comparator or a budget set to 0)."""
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    orch = Orchestrator(client=fake)

    result = await orch.run_narration_turn("look around", simple_turn_context)

    assert len(fake.recorded_requests) == 1, (
        "Under-budget prompt MUST reach the SDK; got "
        f"{len(fake.recorded_requests)} recorded request(s)."
    )
    assert result.is_degraded is False, "Under-budget turn must not be marked degraded."


# ---------------------------------------------------------------------------
# 3. Wiring test — loud emit reaches the GM-panel transport.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_canary_emits_loud_event_to_gm_panel(
    simple_turn_context,
    bound_hub: WatcherHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard refuse MUST publish `prompt_oversized_hard` with
    `severity="error"` so the GM panel can red-band filter on a stable
    event name without inspecting field payloads.

    This is the wiring assertion (CLAUDE.md "Every Test Suite Needs a
    Wiring Test"): drive the real production code path, subscribe a
    socket to the live watcher_hub, prove the event reaches the
    transport — not just `logger.error`.
    """
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    monkeypatch.setattr(orch_mod, "PROMPT_BUDGET_BYTES_HARD", 10)

    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", simple_turn_context)
    await asyncio.sleep(0.05)

    hard_events = [e for e in sock.events if e.get("event_type") == "prompt_oversized_hard"]
    assert len(hard_events) == 1, (
        "Exactly one `prompt_oversized_hard` event must reach watcher "
        f"subscribers per refused turn; got {len(hard_events)} "
        f"(all events: {[e.get('event_type') for e in sock.events]})."
    )
    event = hard_events[0]
    assert event.get("severity") == "error", (
        "GM panel red-band filter relies on severity='error'; got "
        f"severity={event.get('severity')!r}."
    )
    fields = event.get("fields", {})
    assert "total_bytes" in fields, (
        "GM panel needs `total_bytes` to show the operator how far over "
        f"the budget the prompt was; got fields={list(fields)}."
    )
    assert fields["total_bytes"] > 10, (
        "`total_bytes` must reflect the actual prompt size, not the "
        f"forced budget; got {fields['total_bytes']}."
    )


# ---------------------------------------------------------------------------
# 4. Log level promoted to ERROR — overnight-scroll-past failure mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_canary_logs_at_error_level_not_warning(
    simple_turn_context,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-05-23 incident's failure mode was a WARNING line scrolling
    past unread overnight. Promote to ERROR so red-band log filters
    (`just logs | grep ERROR`) surface it during long-running sessions.

    Asserts:
      - At least one ERROR-level record carrying `narrator.prompt_oversized`
        (existing prefix preserved for log-tail consumers).
      - No WARNING-level record carrying the same prefix (regression guard
        against accidental `logger.warning` reintroduction).
    """
    monkeypatch.setattr(orch_mod, "PROMPT_BUDGET_BYTES_HARD", 10)

    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    orch = Orchestrator(client=fake)

    with caplog.at_level(logging.DEBUG, logger="sidequest.agents.orchestrator"):
        await orch.run_narration_turn("look around", simple_turn_context)

    oversized_records = [r for r in caplog.records if "narrator.prompt_oversized" in r.getMessage()]
    assert oversized_records, (
        "Expected at least one log record carrying `narrator.prompt_oversized` "
        f"prefix; got records: {[r.getMessage() for r in caplog.records]}"
    )
    error_records = [r for r in oversized_records if r.levelno == logging.ERROR]
    assert error_records, (
        "Hard cap MUST log at ERROR (not WARNING) so red-band log filters "
        "catch overnight-scroll-past incidents. "
        f"levels seen: {[r.levelname for r in oversized_records]}"
    )
    warning_records = [r for r in oversized_records if r.levelno == logging.WARNING]
    assert not warning_records, (
        "Hard cap MUST NOT emit at WARNING (that was the 2026-05-23 failure "
        f"mode). Got {len(warning_records)} WARNING record(s) — regression."
    )


# ---------------------------------------------------------------------------
# 5. No-double-emit — sustained runaway must not spam the GM panel.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_emits_exactly_once_per_oversized_call(
    simple_turn_context,
    bound_hub: WatcherHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive oversized turns must produce exactly two hard
    events, not 2×N from an internal loop or recursive emit.

    Guards against the sustained-runaway observability-spam failure
    mode: during a real cost runaway the canary refuses every turn for
    minutes; one event per refuse is actionable, hundreds per turn
    drowns the GM panel.
    """
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    monkeypatch.setattr(orch_mod, "PROMPT_BUDGET_BYTES_HARD", 10)

    fake = FakeAnthropicSdkClient(responses=[_end_turn(), _end_turn()])
    orch = Orchestrator(client=fake)

    await orch.run_narration_turn("look around", simple_turn_context)
    await orch.run_narration_turn("look again", simple_turn_context)
    await asyncio.sleep(0.05)

    hard_events = [e for e in sock.events if e.get("event_type") == "prompt_oversized_hard"]
    assert len(hard_events) == 2, (
        "Each refused turn must emit exactly one `prompt_oversized_hard` "
        f"event (no internal loop, no recursive emit). Got {len(hard_events)} "
        "events across 2 turns."
    )
    # Belt-and-suspenders: the SDK still must not have been called on
    # either turn — proves the no-double-emit guard isn't paid for by
    # accidentally letting one of the two oversized turns slip through.
    assert fake.recorded_requests == [], (
        "Neither refused turn should have reached the SDK; got "
        f"{len(fake.recorded_requests)} recorded request(s)."
    )


# ---------------------------------------------------------------------------
# 6. Adversarial probe (TEA verify) — SDK + synchronous parity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_and_synchronous_paths_refuse_with_identical_shape(
    simple_turn_context,
    bound_hub: WatcherHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the SAME oversized prompt through BOTH narration paths and
    assert byte-identical refuse shape.

    Architect's spec-check (61-3, answer B) verified the canary is correctly
    placed at prompt-construction time on both paths against the same
    ``PROMPT_BUDGET_BYTES_HARD`` measure — but did NOT measure end-to-end
    shape parity. This probe ratifies that finding empirically and guards
    against future drift between the two paths (e.g., a tweak to the SDK
    refuse narration that isn't mirrored on the sync path would leave the
    GM panel showing inconsistent operator-page text depending on which
    backend was active when the runaway fired).

    Parity surface (the contract a future drift would break):

    1. ``is_degraded`` — both True.
    2. ``narration`` — both ``"[narrator-overload — operator paged]"``
       (distinct from the default SDK-error-refuse text per Dev's locked
       open-question 1 resolution).
    3. Watcher event ``prompt_oversized_hard`` fires exactly once per
       refused turn on EACH path.
    4. Event ``severity == "error"`` on both — the GM panel red-band filter
       depends on this single field.
    5. Event ``fields["action"] == "refuse"`` on both — distinguishes
       hard-refuse from a future truncate variant (epic 61 §Layer 3).
    6. Neither path bills (no SDK/client call recorded).
    """
    from unittest.mock import AsyncMock

    from sidequest.agents.claude_client import ClaudeResponse

    monkeypatch.setattr(orch_mod, "PROMPT_BUDGET_BYTES_HARD", 10)

    # --- SDK path ---------------------------------------------------------
    sdk_sock = _FakeSocket()
    await bound_hub.subscribe(sdk_sock)  # type: ignore[arg-type]

    sdk_fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    sdk_orch = Orchestrator(client=sdk_fake)
    sdk_result = await sdk_orch.run_narration_turn("look around", simple_turn_context)
    await asyncio.sleep(0.05)

    sdk_events = [e for e in sdk_sock.events if e.get("event_type") == "prompt_oversized_hard"]

    # Clear subscribers so the synchronous-path subscription doesn't also
    # receive any residual SDK-path events (defensive isolation).
    async with bound_hub._lock:  # noqa: SLF001
        bound_hub._subscribers.clear()  # noqa: SLF001

    # --- Synchronous path -------------------------------------------------
    sync_sock = _FakeSocket()
    await bound_hub.subscribe(sync_sock)  # type: ignore[arg-type]

    sync_client = AsyncMock()
    sync_client.send_stateless = AsyncMock(
        return_value=ClaudeResponse(text='{"narration":"ok"}', session_id=None)
    )
    sync_orch = Orchestrator(client=sync_client)
    sync_result = await sync_orch.run_narration_turn("look around", simple_turn_context)
    await asyncio.sleep(0.05)

    sync_events = [e for e in sync_sock.events if e.get("event_type") == "prompt_oversized_hard"]

    # --- Parity assertions ------------------------------------------------
    # 1+2. Result shape: is_degraded + narration text.
    assert sdk_result.is_degraded is True, "SDK path must refuse with is_degraded=True"
    assert sync_result.is_degraded is True, "sync path must refuse with is_degraded=True"
    assert sdk_result.narration == sync_result.narration, (
        "Refuse narration text must match across paths so operators see "
        f"consistent paged text. SDK={sdk_result.narration!r} "
        f"sync={sync_result.narration!r}"
    )
    assert sdk_result.narration == "[narrator-overload — operator paged]", (
        "Refuse narration must be the distinct budget-refuse text (Dev "
        "open-question 1 resolution — bracketed prefix + en-dash so "
        f"session grep can find it). Got {sdk_result.narration!r}."
    )

    # 3. Exactly one event per refused turn on each path.
    assert len(sdk_events) == 1, (
        f"SDK path must emit exactly one prompt_oversized_hard; got {len(sdk_events)}"
    )
    assert len(sync_events) == 1, (
        f"sync path must emit exactly one prompt_oversized_hard; got {len(sync_events)}"
    )

    # 4. Severity parity.
    assert sdk_events[0].get("severity") == sync_events[0].get("severity") == "error", (
        "Watcher event severity must be 'error' on both paths (GM panel "
        f"red-band filter). SDK={sdk_events[0].get('severity')!r} "
        f"sync={sync_events[0].get('severity')!r}"
    )

    # 5. Action field parity.
    sdk_fields = sdk_events[0].get("fields", {})
    sync_fields = sync_events[0].get("fields", {})
    assert sdk_fields.get("action") == sync_fields.get("action") == "refuse", (
        "Watcher event fields.action must be 'refuse' on both paths "
        "(distinguishes hard-refuse from a future truncate variant). "
        f"SDK={sdk_fields.get('action')!r} sync={sync_fields.get('action')!r}"
    )

    # 6. Neither path billed.
    assert sdk_fake.recorded_requests == [], (
        f"SDK path must not call complete_with_tools on refuse; got "
        f"{len(sdk_fake.recorded_requests)} request(s)."
    )
    sync_client.send_stateless.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_refuse_stamps_action_span_refused_oversized_attribute(
    simple_turn_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 61-8 §C2 (review-fix round 2) — when the hard cap fires
    on the SDK path, the open ``orchestrator_process_action_span`` MUST
    be stamped with ``refused_oversized=True`` so a future GM-panel
    per-turn view can red-band-color the entire refused turn. Companion
    to the existing ``prompt_oversized_hard`` watcher event — the
    event flags the moment, the span attribute lets per-trace views
    color the whole turn.

    Without this regression guard, a refactor that drops the
    ``_action_span.set_attribute(...)`` call would be invisible to the
    test suite (the watcher-event tests above would still pass).
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry import spans as _spans

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    monkeypatch.setattr(orch_mod, "PROMPT_BUDGET_BYTES_HARD", 10)

    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    orch = Orchestrator(client=fake)
    result = await orch.run_narration_turn("look around", simple_turn_context)

    assert result.is_degraded is True
    assert fake.recorded_requests == []

    finished = exporter.get_finished_spans()
    action_spans = [s for s in finished if s.name == "orchestrator.process_action"]
    assert len(action_spans) == 1, (
        f"Expected exactly one orchestrator.process_action span; "
        f"got {len(action_spans)}. Finished spans: "
        f"{sorted({s.name for s in finished})}"
    )
    attrs = action_spans[0].attributes or {}
    assert attrs.get("refused_oversized") is True, (
        f"orchestrator.process_action span missing or wrong "
        f"refused_oversized attribute: got {attrs.get('refused_oversized')!r}, "
        "expected True. The §C2 per-turn ribbon-coloring signal is "
        "silently absent — GM-panel per-trace views cannot color the "
        "refused turn distinctly."
    )
