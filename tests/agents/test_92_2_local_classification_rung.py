"""Story 92-2 (epic 92 "Local Classification Routing") — RED suite.

Local rung in the model ladder: ``CallType.CLASSIFICATION`` / ``CallType.SCRATCH``
route to Ollama behind EXPLICIT config; fail loud if unreachable; NO silent
Haiku fallback.

Contract pinned by these tests (TEA-defined ACs — the sprint YAML carries none):

* **AC1 — Explicit config, default OFF.** New env seam
  ``SIDEQUEST_CLASSIFICATION_BACKEND`` (constant ``ENV_CLASSIFICATION_BACKEND``
  exported from ``sidequest.agents.llm_factory``), values ``anthropic``
  (default) | ``ollama``, normalized strip+lower like ``SIDEQUEST_LLM_BACKEND``.
  Unset/``anthropic`` keeps today's Haiku behavior EXACTLY — the epic's hard
  gate says no routing flip without A/B evidence, so the default must not move.
* **AC2 — Local rung in the ladder.** With the flag on,
  ``resolve_model(CallType.CLASSIFICATION)`` and ``resolve_model(CallType.SCRATCH)``
  return the A/B-validated local model id (``ab_eval_harness.OLLAMA_MODEL``,
  ``qwen2.5:7b-instruct``) — the 92-1 gate evidence is FOR that model; routing
  a different one would ship an unevaluated classifier. NARRATION tiers are
  untouched.
* **AC3 — Router factory routes local.** ``build_intent_router_llm`` with the
  flag on returns an Ollama-backed ``IntentRouterLLM`` adapter, requires NO
  ``ANTHROPIC_API_KEY``, and never constructs the Anthropic SDK.
* **AC4 — emit_tool is prompt-coerced JSON through the existing
  ``ollama_client`` transport** (Don't Reinvent — Wire Up What Exists; the
  measurement-only ``QwenRouterLlm`` docstring names this story for the
  production adapter). Tests inject transport via
  ``sidequest.agents.ollama_client.urlopen``.
* **AC5 — Fail loud if unreachable, NO silent Haiku fallback.** Transport
  errors raise ``OllamaClientError``; the Anthropic construction site
  (``build_async_anthropic``) is provably never touched on the local path.
* **AC6 — Unknown config value fails loud** at both the ladder and the
  factory (a typo silently meaning "haiku" would recreate dark spend).
* **AC7 — The 91-3 Haiku cache-floor guard does not apply to the local path**
  (there is no Anthropic cache to protect).
* **AC8 — Wiring.** The production per-turn factory
  ``build_intent_router_for_session`` honors the config seam.
* **AC9 — OTEL.** A successful local call emits a span carrying
  ``agent.backend == "ollama"`` — story 92-4's playtest verification consumes
  exactly these spans (OTEL Observability Principle).

Imports of not-yet-existing names happen INSIDE tests (house style — each
test fails individually in RED instead of erroring at collection).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request

import pytest

# ---------------------------------------------------------------------------
# Shared fakes / helpers
# ---------------------------------------------------------------------------

HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"

# Sample tool args matching the IntentRouterLLM.emit_tool signature.
_TOOL_KWARGS: dict[str, Any] = {
    "system": "You are the intent router.",
    "user": "I draw my sword and charge the bandit.",
    "tool_name": "emit_dispatch_package",
    "tool_description": "Emit the structured DispatchPackage.",
    "tool_schema": {"type": "object", "properties": {"dispatches": {"type": "array"}}},
}


class _FakeHttpResponse:
    """Context-manager HTTP response matching what ``ollama_client`` reads."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _fake_urlopen_returning(tool_input: dict[str, Any]):
    """Build a fake ``urlopen`` answering both Ollama endpoints.

    ``/api/chat`` replies with a message envelope, ``/api/generate`` with a
    response envelope — whichever transport shape the production adapter
    uses, the body is the same prompt-coerced JSON object.
    """
    body_text = json.dumps(tool_input)

    def fake_urlopen(req: Request, timeout: float | None = None) -> _FakeHttpResponse:
        url = req.full_url
        if url.endswith("/api/chat"):
            envelope = {
                "message": {"role": "assistant", "content": body_text},
                "prompt_eval_count": 120,
                "eval_count": 40,
            }
        elif url.endswith("/api/generate"):
            envelope = {
                "response": body_text,
                "prompt_eval_count": 120,
                "eval_count": 40,
            }
        else:  # pragma: no cover — an unexpected endpoint is a wiring bug
            raise AssertionError(f"unexpected Ollama endpoint: {url}")
        return _FakeHttpResponse(json.dumps(envelope).encode("utf-8"))

    return fake_urlopen


def _fake_urlopen_prose_only(req: Request, timeout: float | None = None) -> _FakeHttpResponse:
    """Ollama reachable but the model returned prose with no JSON object."""
    envelope = {
        "message": {"role": "assistant", "content": "Sorry, I cannot call tools."},
        "response": "Sorry, I cannot call tools.",
        "prompt_eval_count": 50,
        "eval_count": 12,
    }
    return _FakeHttpResponse(json.dumps(envelope).encode("utf-8"))


def _fake_urlopen_unreachable(req: Request, timeout: float | None = None) -> _FakeHttpResponse:
    raise ConnectionRefusedError(61, "Connection refused")


def _install_anthropic_sentinel(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the single Anthropic construction site with a tripwire.

    ``build_async_anthropic`` is the SOLE ``AsyncAnthropic`` construction
    site (story 91-1) and consumers must look it up late-bound, so patching
    the module attribute intercepts every possible Haiku construction. The
    returned list records any call — it must stay empty on the local path.
    """
    calls: list[str] = []

    def sentinel() -> Any:
        calls.append("build_async_anthropic")
        raise AssertionError(
            "Anthropic SDK constructed on the LOCAL classification path — "
            "this is the silent Haiku fallback story 92-2 forbids."
        )

    import sidequest.agents.llm_factory as llm_factory

    monkeypatch.setattr(llm_factory, "build_async_anthropic", sentinel)
    return calls


def _enable_local_rung(monkeypatch: pytest.MonkeyPatch, value: str = "ollama") -> None:
    monkeypatch.setenv("SIDEQUEST_CLASSIFICATION_BACKEND", value)


# ---------------------------------------------------------------------------
# AC1 — the config seam exists, default is OFF (Haiku unchanged).
# ---------------------------------------------------------------------------


def test_env_classification_backend_constant_exported() -> None:
    """The config seam is a named constant, not a stringly-typed literal —
    same doctrine as ``ENV_BACKEND`` / ``ENV_OLLAMA_URL`` in llm_factory."""
    from sidequest.agents.llm_factory import ENV_CLASSIFICATION_BACKEND

    assert ENV_CLASSIFICATION_BACKEND == "SIDEQUEST_CLASSIFICATION_BACKEND"


def test_default_classification_resolves_to_haiku(monkeypatch: pytest.MonkeyPatch) -> None:
    """Epic hard gate: NO routing flip by default. With the env unset the
    ladder must keep returning the exact Haiku id — not a local model."""
    from sidequest.agents.model_routing import CallType, resolve_model

    monkeypatch.delenv("SIDEQUEST_CLASSIFICATION_BACKEND", raising=False)
    assert resolve_model(CallType.CLASSIFICATION) == HAIKU_MODEL_ID
    assert resolve_model(CallType.SCRATCH) == HAIKU_MODEL_ID


def test_explicit_anthropic_value_keeps_haiku(monkeypatch: pytest.MonkeyPatch) -> None:
    """``anthropic`` is the explicit spelling of the default — identical
    resolution, so operators can pin the current behavior in config."""
    from sidequest.agents.model_routing import CallType, resolve_model

    _enable_local_rung(monkeypatch, "anthropic")
    assert resolve_model(CallType.CLASSIFICATION) == HAIKU_MODEL_ID
    assert resolve_model(CallType.SCRATCH) == HAIKU_MODEL_ID


# ---------------------------------------------------------------------------
# AC2 — the local rung in the ladder.
# ---------------------------------------------------------------------------


def test_local_rung_resolves_classification_to_ab_validated_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flipped ladder must serve EXACTLY the model the 92-1 A/B gate
    evaluated (``ab_eval_harness.OLLAMA_MODEL``). A drifted model id would
    ship a classifier with zero agreement evidence — a SOUL/agency problem
    per the epic, not just a cost problem."""
    from sidequest.agents.ab_eval_harness import OLLAMA_MODEL
    from sidequest.agents.model_routing import CallType, resolve_model

    _enable_local_rung(monkeypatch)
    resolved = resolve_model(CallType.CLASSIFICATION)
    assert resolved == OLLAMA_MODEL
    assert not resolved.startswith("claude-")


def test_local_rung_resolves_scratch_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCRATCH is named in the story title — the dungeon-curate caller
    (``materializer.py`` resolves ``CallType.SCRATCH``) must follow the rung."""
    from sidequest.agents.ab_eval_harness import OLLAMA_MODEL
    from sidequest.agents.model_routing import CallType, resolve_model

    _enable_local_rung(monkeypatch)
    assert resolve_model(CallType.SCRATCH) == OLLAMA_MODEL


def test_local_rung_leaves_narration_tiers_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rung is scoped to CLASSIFICATION/SCRATCH only. Narration must keep
    resolving to the Anthropic ladder even with the flag on — flipping the
    narrator is emphatically NOT this story."""
    from sidequest.agents.model_routing import CallType, resolve_model

    _enable_local_rung(monkeypatch)
    assert resolve_model(CallType.NARRATION).startswith("claude-")
    assert resolve_model(CallType.NARRATION_IMPORTANT).startswith("claude-")


def test_local_rung_does_not_break_pack_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pack overrides remain the highest-precedence rung (existing contract):
    an explicit per-pack model for CLASSIFICATION wins over the local rung."""
    from sidequest.agents.model_routing import CallType, resolve_model

    _enable_local_rung(monkeypatch)
    resolved = resolve_model(
        CallType.CLASSIFICATION,
        pack_overrides={CallType.CLASSIFICATION: "pack-special-model"},
    )
    assert resolved == "pack-special-model"


# ---------------------------------------------------------------------------
# AC6 — unknown config value fails loud (ladder AND factory).
# ---------------------------------------------------------------------------


def test_unknown_classification_backend_fails_loud_at_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo (``olama``, ``local``, ...) must never silently mean Haiku —
    that is dark spend by configuration error. The raise must name the env
    var so the operator can find the knob."""
    from sidequest.agents.model_routing import CallType, resolve_model

    _enable_local_rung(monkeypatch, "olama")
    with pytest.raises(Exception, match="SIDEQUEST_CLASSIFICATION_BACKEND"):
        resolve_model(CallType.CLASSIFICATION)


def test_unknown_classification_backend_fails_loud_at_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.agents.claude_client import LlmClientError
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _enable_local_rung(monkeypatch, "banana")
    with pytest.raises(LlmClientError, match="SIDEQUEST_CLASSIFICATION_BACKEND"):
        build_intent_router_llm(session_id=None)


# ---------------------------------------------------------------------------
# AC3 — factory routes to the local adapter behind the flag.
# ---------------------------------------------------------------------------


def test_factory_default_still_builds_haiku_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-off regression pin: with the env unset, the factory returns
    the existing Haiku SDK adapter — behavior identical to pre-92-2."""
    from sidequest.agents.llm_factory import _IntentRouterLlm, build_intent_router_llm

    monkeypatch.delenv("SIDEQUEST_CLASSIFICATION_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    adapter = build_intent_router_llm(session_id=None)
    assert isinstance(adapter, _IntentRouterLlm)


def test_factory_ollama_builds_local_adapter_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local path must not require ``ANTHROPIC_API_KEY`` — requiring an
    Anthropic credential to run a $0 local model would be absurd coupling and
    would block credential-free local dev."""
    from sidequest.agents.llm_factory import _IntentRouterLlm, build_intent_router_llm

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)
    assert not isinstance(adapter, _IntentRouterLlm)
    emit = getattr(adapter, "emit_tool", None)
    assert callable(emit), "local adapter must satisfy the IntentRouterLLM protocol"


def test_factory_ollama_never_touches_anthropic_construction_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 91-1 made ``build_async_anthropic`` the single construction
    site. On the local path it must never be called — at build time."""
    calls = _install_anthropic_sentinel(monkeypatch)
    _enable_local_rung(monkeypatch)
    from sidequest.agents.llm_factory import build_intent_router_llm

    build_intent_router_llm(session_id=None)
    assert calls == []


def test_factory_ollama_value_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace/case normalize-then-gate, matching ``SIDEQUEST_LLM_BACKEND``
    handling — '` OLLAMA `' is the same explicit choice as '`ollama`'."""
    from sidequest.agents.llm_factory import _IntentRouterLlm, build_intent_router_llm

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _enable_local_rung(monkeypatch, " OLLAMA  ")
    adapter = build_intent_router_llm(session_id=None)
    assert not isinstance(adapter, _IntentRouterLlm)


def test_session_id_remains_required_keyword_only_on_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 91-4 made ``session_id`` required keyword-only so no call site
    silently constructs an uncovered spender. The local rung must not loosen
    that signature — omission stays a ``TypeError`` regardless of backend."""
    _enable_local_rung(monkeypatch)
    from sidequest.agents.llm_factory import build_intent_router_llm

    with pytest.raises(TypeError):
        build_intent_router_llm()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# AC7 — the Haiku cache-floor guard (91-3) does not gate the local path.
# ---------------------------------------------------------------------------


def test_cache_floor_guard_not_applied_to_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 4,096-token floor protects an ANTHROPIC cache. With the rung on
    there is no Anthropic cache to protect — a sub-floor prefix must not
    refuse the build (this is also what frees 82-10's prompt slimming)."""
    import sidequest.agents.intent_router as ir
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.setattr(ir, "_SYSTEM_PROMPT", "tiny system prompt")
    monkeypatch.setattr(
        ir, "_dispatch_tool_schema", lambda: {"type": "object", "properties": {}}
    )
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)  # must not raise
    assert adapter is not None


# ---------------------------------------------------------------------------
# AC4 — emit_tool: prompt-coerced JSON through the ollama_client transport.
# ---------------------------------------------------------------------------


async def test_emit_tool_returns_parsed_tool_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the local model answers with a single JSON object; the
    adapter returns it parsed — the contract ``IntentRouter`` consumes."""
    import sidequest.agents.ollama_client as ollama_client
    from sidequest.agents.llm_factory import build_intent_router_llm

    expected = {"dispatches": [{"subsystem": "confrontation", "confidence": 0.9}]}
    monkeypatch.setattr(ollama_client, "urlopen", _fake_urlopen_returning(expected))
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)

    result = await adapter.emit_tool(**_TOOL_KWARGS)
    assert result == expected


async def test_emit_tool_raises_on_prose_only_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion with no JSON object must RAISE — never return an empty
    package (the router's retry/failure taxonomy owns the failure, same
    doctrine as the harness's ``_extract_json_object``)."""
    import sidequest.agents.ollama_client as ollama_client
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.setattr(ollama_client, "urlopen", _fake_urlopen_prose_only)
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)

    with pytest.raises(Exception):
        await adapter.emit_tool(**_TOOL_KWARGS)


# ---------------------------------------------------------------------------
# AC5 — unreachable Ollama fails LOUD; no silent Haiku fallback, ever.
# ---------------------------------------------------------------------------


async def test_unreachable_ollama_raises_ollama_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection refused surfaces as the typed Ollama transport error —
    the turn fails loudly and visibly, exactly per the epic doctrine."""
    import sidequest.agents.ollama_client as ollama_client
    from sidequest.agents.ollama_client import OllamaClientError
    from sidequest.agents.llm_factory import build_intent_router_llm

    monkeypatch.setattr(ollama_client, "urlopen", _fake_urlopen_unreachable)
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)

    with pytest.raises(OllamaClientError):
        await adapter.emit_tool(**_TOOL_KWARGS)


async def test_unreachable_ollama_never_falls_back_to_haiku(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE story invariant. With Ollama down, the failure must propagate —
    the Anthropic construction site must remain untouched through build AND
    the failing call. A silent fallback to Haiku would recreate the exact
    dark spend this epic eliminates, by design."""
    import sidequest.agents.ollama_client as ollama_client
    from sidequest.agents.ollama_client import OllamaClientError
    from sidequest.agents.llm_factory import build_intent_router_llm

    calls = _install_anthropic_sentinel(monkeypatch)
    monkeypatch.setattr(ollama_client, "urlopen", _fake_urlopen_unreachable)
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)

    with pytest.raises(OllamaClientError):
        await adapter.emit_tool(**_TOOL_KWARGS)
    assert calls == [], "Haiku fallback attempted on Ollama failure"


# ---------------------------------------------------------------------------
# AC9 — OTEL: the local call is observable as agent.backend=ollama.
# ---------------------------------------------------------------------------


async def test_emit_tool_emits_backend_ollama_span(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture,
) -> None:
    """Story 92-4's playtest cost proof greps for ``agent.backend=ollama``
    spans — without them the GM panel cannot tell the local rung is engaged
    vs. Claude just improvising (OTEL Observability Principle)."""
    import sidequest.agents.ollama_client as ollama_client
    from sidequest.agents.llm_factory import build_intent_router_llm

    expected = {"dispatches": []}
    monkeypatch.setattr(ollama_client, "urlopen", _fake_urlopen_returning(expected))
    _enable_local_rung(monkeypatch)
    adapter = build_intent_router_llm(session_id=None)

    await adapter.emit_tool(**_TOOL_KWARGS)

    backends = [
        dict(span.attributes or {}).get("agent.backend")
        for span in otel_capture.get_finished_spans()
    ]
    assert "ollama" in backends, (
        f"no span carried agent.backend=ollama; saw backends={backends!r}"
    )


# ---------------------------------------------------------------------------
# AC8 — wiring: the production per-turn factory honors the seam.
# ---------------------------------------------------------------------------


def test_wiring_session_factory_builds_local_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_intent_router_for_session`` is the production per-turn entry
    (called from ``websocket_session_handler``). With the rung on it must
    hand the IntentRouter a NON-Haiku adapter, with no API key present —
    proving the config seam is wired into the live turn path, not just the
    factory unit."""
    from sidequest.agents.intent_router import IntentRouter
    from sidequest.agents.llm_factory import _IntentRouterLlm
    from sidequest.server.intent_router_pass import build_intent_router_for_session

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _enable_local_rung(monkeypatch)
    router = build_intent_router_for_session(session_id=None)
    assert isinstance(router, IntentRouter)
    assert not isinstance(router._llm, _IntentRouterLlm)


def test_wiring_session_factory_default_remains_haiku(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-off through the production path too: env unset → the router
    gets the Haiku SDK adapter, exactly as before this story."""
    from sidequest.agents.intent_router import IntentRouter
    from sidequest.agents.llm_factory import _IntentRouterLlm
    from sidequest.server.intent_router_pass import build_intent_router_for_session

    monkeypatch.delenv("SIDEQUEST_CLASSIFICATION_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    router = build_intent_router_for_session(session_id=None)
    assert isinstance(router, IntentRouter)
    assert isinstance(router._llm, _IntentRouterLlm)
