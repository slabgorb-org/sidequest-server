"""Story 92-2 — SCRATCH consumer wiring: dungeon curate routes to the local rung.

The dungeon curate stage is the production ``CallType.SCRATCH`` caller
(named in the story title and counted in the epic-92 cost forensics). With
``SIDEQUEST_CLASSIFICATION_BACKEND=ollama`` the curate one-shot must route
through the Ollama transport and must NEVER touch the injected Anthropic
tooling client — an Ollama failure flows the existing loud retry→degrade
ladder (ADR-106 Amendment A), never a silent fallback to Anthropic.

This is the wiring test the TEA blocking Delivery Finding asked for: it
drives the real ``_stage_curate`` (fixture-driven behavior test per the
no-source-text-wiring-tests rule) with a tripwire Anthropic client and a
fake Ollama transport.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request

import pytest

from tests.dungeon.test_materializer import (
    _curate_inputs,
    _real_cookbook_bundle,
    _setup_otel_task3,
)


class _FakeHttpResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


class _AnthropicTripwire:
    """Injected ``claude_client`` that must never be reached on the local path."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_with_tools(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError(
            "curate touched the Anthropic tooling client with the local rung on "
            "— the silent fallback story 92-2 forbids"
        )


@pytest.mark.asyncio
async def test_curate_scratch_routes_to_ollama_and_degrades_loud_not_haiku(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the rung on: (1) the curate one-shot goes out over the Ollama
    transport; (2) the Anthropic client is untouched; (3) an unusable local
    verdict degrades LOUDLY to uncurated (curated=False + per-region marker)
    instead of silently re-billing Haiku."""
    import sidequest.agents.ollama_client as ollama_client
    import sidequest.dungeon.materializer as _mat
    from sidequest.telemetry.spans.dungeon_materialize import (
        dungeon_materialize_curate_span,
    )

    transport_calls: list[str] = []

    def fake_urlopen(req: Request, timeout: float | None = None) -> _FakeHttpResponse:
        transport_calls.append(req.full_url)
        # Reachable Ollama, but the local model answers prose — an
        # unparseable verdict, so the loud degrade ladder must engage.
        envelope = {
            "message": {"role": "assistant", "content": "I cannot produce a verdict."},
            "response": "I cannot produce a verdict.",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        return _FakeHttpResponse(json.dumps(envelope).encode("utf-8"))

    monkeypatch.setattr(ollama_client, "urlopen", fake_urlopen)
    monkeypatch.setenv("SIDEQUEST_CLASSIFICATION_BACKEND", "ollama")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    bundle = _real_cookbook_bundle()
    request, palette, expansion, fill_result, _look = _curate_inputs(
        algorithm="prim", expansion_id=9, depth_score=0.5
    )
    tripwire = _AnthropicTripwire()

    exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
    try:
        with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
            result = await _mat._stage_curate(
                request,
                bundle=bundle,
                palette=palette,
                expansion=expansion,
                fill_result=fill_result,
                is_first_band_entry=True,
                claude_client=tripwire,
                span=span,
            )
    finally:
        _spans_mod.tracer = original_tracer_fn

    # (1) The call went out over the Ollama transport (both retry attempts).
    assert transport_calls, "curate never reached the Ollama transport"
    assert all(url.endswith("/api/chat") for url in transport_calls)
    # (2) The Anthropic tooling client was never touched.
    assert tripwire.calls == 0
    # (3) Loud degrade, not silence: the expansion shipped uncurated with
    # the per-region marker (ADR-106 Amendment A Layer 2).
    assert result.curated is False
    assert result.uncurated_regions, "degrade must mark the regions uncurated"


@pytest.mark.asyncio
async def test_curate_default_still_uses_injected_anthropic_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-off pin for the SCRATCH consumer: with the env unset the
    curate one-shot still goes to the injected tooling client (and an
    Ollama transport call would be a regression)."""
    import sidequest.agents.ollama_client as ollama_client
    import sidequest.dungeon.materializer as _mat
    from sidequest.telemetry.spans.dungeon_materialize import (
        dungeon_materialize_curate_span,
    )

    def forbidden_urlopen(req: Request, timeout: float | None = None) -> _FakeHttpResponse:
        raise AssertionError("default path must not touch the Ollama transport")

    monkeypatch.setattr(ollama_client, "urlopen", forbidden_urlopen)
    monkeypatch.delenv("SIDEQUEST_CLASSIFICATION_BACKEND", raising=False)

    bundle = _real_cookbook_bundle()
    request, palette, expansion, fill_result, _look = _curate_inputs(
        algorithm="prim", expansion_id=10, depth_score=0.5
    )
    # The tripwire raises AssertionError, which is NOT in the ladder's
    # (LlmClientError, CurationError) catch — so reaching the client is
    # observable as a loud AssertionError out of _stage_curate. That both
    # proves the default path targets the injected client and keeps this
    # test honest (no silent pass if the routing flipped).
    client = _AnthropicTripwire()
    exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
    try:
        with (
            dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span,
            pytest.raises(AssertionError, match="Anthropic tooling client"),
        ):
            await _mat._stage_curate(
                request,
                bundle=bundle,
                palette=palette,
                expansion=expansion,
                fill_result=fill_result,
                is_first_band_entry=True,
                claude_client=client,
                span=span,
            )
    finally:
        _spans_mod.tracer = original_tracer_fn

    assert client.calls == 1
