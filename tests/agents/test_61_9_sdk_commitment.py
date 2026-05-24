"""Story 61-9 — Commit to SDK narrator; retire legacy backends.

This file holds the RED contract for story 61-9. Every test here is
designed to fail against today's code and pass after Dev's green-phase
implementation. The tests cover the six acceptance criteria from
``sprint/context/context-story-61-9.md`` and the OTEL span shape locked
to **Option (a)** (keep span, drop ``tool_backend`` attribute, hard-wire
``guardrails_skipped`` and ``bytes_saved``).

Reflection-only contract (per ``sidequest-server/CLAUDE.md`` §"No
Source-Text Wiring Tests"). All structural assertions here use
``inspect``, ``importlib``, ``pathlib.Path.exists``, or run-and-assert.
None read source files with ``Path.read_text()`` for regex match.

Authority hierarchy (TEA spec-authority): story scope > story context >
epic context > docs. The story context's DECISION LOCKED for the OTEL
span (Option a) is the assertion shape; if Dev needs to flip to Option
b, the span-shape tests get rewritten in red.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# AC-3 — llm_factory.build_llm_client() purpose gate (load-bearing)
# ---------------------------------------------------------------------------


class TestNarratorBackendGate:
    """``build_llm_client(purpose=...)`` fails loud for retired backends.

    The strict reading wins per project memory ``feedback_no_fallbacks_hard``:
    any call with ``SIDEQUEST_LLM_BACKEND`` set to ``claude`` or ``ollama``
    raises at construction, regardless of ``purpose``. The ``purpose`` kwarg
    exists so the error message can be context-aware (narrator vs tool) and
    so the dungeon-curate call site declares intent for future audits.
    """

    def test_narrator_purpose_with_claude_backend_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sidequest.agents.claude_client import LlmClientError
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client

        monkeypatch.setenv(ENV_BACKEND, "claude")
        with pytest.raises(LlmClientError) as exc_info:
            build_llm_client(purpose="narrator")
        assert "claude" in str(exc_info.value).lower(), (
            "Error message must mention the offending backend name so the "
            "operator who set SIDEQUEST_LLM_BACKEND=claude knows what to fix."
        )

    def test_narrator_purpose_with_ollama_backend_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sidequest.agents.claude_client import LlmClientError
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client

        monkeypatch.setenv(ENV_BACKEND, "ollama")
        with pytest.raises(LlmClientError) as exc_info:
            build_llm_client(purpose="narrator")
        assert "ollama" in str(exc_info.value).lower(), (
            "Error message must mention the offending backend name."
        )

    def test_tool_purpose_with_claude_backend_also_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict gate: only ``anthropic_sdk`` is viable for any caller.

        ``claude`` doesn't implement ``complete_with_tools`` (architect §A),
        so handing a tool caller a ``ClaudeClient`` would AttributeError at
        first method call — loud, but at a deep call site rather than the
        config boundary. NO-FALLBACK requires the failure at the config
        boundary, not on first use.
        """
        from sidequest.agents.claude_client import LlmClientError
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client

        monkeypatch.setenv(ENV_BACKEND, "claude")
        with pytest.raises(LlmClientError):
            build_llm_client(purpose="tool")

    def test_tool_purpose_with_ollama_backend_also_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sidequest.agents.claude_client import LlmClientError
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client

        monkeypatch.setenv(ENV_BACKEND, "ollama")
        with pytest.raises(LlmClientError):
            build_llm_client(purpose="tool")

    def test_default_purpose_with_claude_backend_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-kwarg callers must also fail loud.

        The default purpose value is intentionally not asserted here —
        whether it defaults to ``"narrator"`` or has no default, the
        behavior under a retired backend must be the same: raise.
        """
        from sidequest.agents.claude_client import LlmClientError
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client

        monkeypatch.setenv(ENV_BACKEND, "claude")
        with pytest.raises(LlmClientError):
            build_llm_client()

    def test_narrator_purpose_with_sdk_backend_returns_tooling_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client
        from sidequest.agents.tooling_protocol import ToolingLlmClient

        monkeypatch.setenv(ENV_BACKEND, "anthropic_sdk")
        client = build_llm_client(purpose="narrator")
        assert isinstance(client, ToolingLlmClient), (
            f"SDK backend must return a ToolingLlmClient; got {type(client).__name__}"
        )

    def test_tool_purpose_with_sdk_backend_returns_tooling_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sidequest.agents.llm_factory import ENV_BACKEND, build_llm_client
        from sidequest.agents.tooling_protocol import ToolingLlmClient

        monkeypatch.setenv(ENV_BACKEND, "anthropic_sdk")
        client = build_llm_client(purpose="tool")
        assert isinstance(client, ToolingLlmClient)

    def test_purpose_kwarg_is_keyword_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``purpose`` must be keyword-only so future param additions can't
        silently shift positional meaning. Architect §B recommendation."""
        from sidequest.agents.llm_factory import build_llm_client

        sig = inspect.signature(build_llm_client)
        purpose = sig.parameters.get("purpose")
        assert purpose is not None, (
            "build_llm_client() must declare a `purpose` parameter per AC-3."
        )
        assert purpose.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"`purpose` must be keyword-only; got kind={purpose.kind.name}. "
            "Positional acceptance would let future param additions shift "
            "the meaning of arg 0."
        )


# ---------------------------------------------------------------------------
# AC-1 / AC-5 — file rename + constant rename invariants
# ---------------------------------------------------------------------------


class TestConstantAndFileRename:
    """The dual-backend constants collapse to a single SDK-only name."""

    def test_narrator_output_only_sdk_constant_is_removed(self) -> None:
        """``NARRATOR_OUTPUT_ONLY_SDK`` no longer exists; importing fails."""
        from sidequest.agents import narrator_prompts

        importlib.reload(narrator_prompts)
        assert not hasattr(narrator_prompts, "NARRATOR_OUTPUT_ONLY_SDK"), (
            "AC-5: NARRATOR_OUTPUT_ONLY_SDK must be removed. Found it on "
            "the narrator_prompts module — the rename to NARRATOR_OUTPUT_ONLY "
            "should drop the _SDK suffix entirely."
        )
        assert "NARRATOR_OUTPUT_ONLY_SDK" not in narrator_prompts.__all__, (
            "AC-5: NARRATOR_OUTPUT_ONLY_SDK must be removed from __all__."
        )

    def test_narrator_output_only_constant_remains_present(self) -> None:
        """The kept constant name is ``NARRATOR_OUTPUT_ONLY`` (post-rename)."""
        from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY

        assert isinstance(NARRATOR_OUTPUT_ONLY, str)
        assert len(NARRATOR_OUTPUT_ONLY) > 0, (
            "NARRATOR_OUTPUT_ONLY must load non-empty prose from output_only.md"
        )

    def test_narrator_output_only_contains_sdk_tool_use_directive(self) -> None:
        """Post-rename, ``NARRATOR_OUTPUT_ONLY`` carries SDK tool-use prose,
        not the legacy full-sidecar prose. Sentinel phrase ``tools`` paired
        with a tool name (``begin_confrontation``) identifies the SDK file.
        Phrasing-stable enough to survive minor compaction in 61-12."""
        from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY

        assert "begin_confrontation" in NARRATOR_OUTPUT_ONLY, (
            "Post-rename, NARRATOR_OUTPUT_ONLY must be the SDK prose. The "
            "SDK prose references the begin_confrontation tool name; the "
            "legacy prose does not. Sentinel check for the file swap."
        )

    def test_legacy_output_only_sdk_file_no_longer_exists(self) -> None:
        """``output_only_sdk.md`` is gone (renamed to ``output_only.md``)."""
        from sidequest.agents import narrator_prompts

        prompts_dir = Path(narrator_prompts.__file__).parent
        assert not (prompts_dir / "output_only_sdk.md").exists(), (
            f"AC-1: {prompts_dir / 'output_only_sdk.md'} must not exist. "
            "It should have been renamed to output_only.md (replacing the "
            "legacy file)."
        )

    def test_output_only_md_file_exists_post_rename(self) -> None:
        """``output_only.md`` exists at the canonical path."""
        from sidequest.agents import narrator_prompts

        prompts_dir = Path(narrator_prompts.__file__).parent
        target = prompts_dir / "output_only.md"
        assert target.exists(), (
            f"AC-1: {target} must exist. It is the renamed-SDK prose file."
        )


# ---------------------------------------------------------------------------
# AC-2 — build_output_format signature pruning
# ---------------------------------------------------------------------------


class TestBuildOutputFormatSignature:
    """``NarratorAgent.build_output_format`` no longer takes ``tool_backend``."""

    def test_build_output_format_signature_drops_tool_backend(self) -> None:
        from sidequest.agents.narrator import NarratorAgent

        sig = inspect.signature(NarratorAgent.build_output_format)
        params = sig.parameters
        assert "tool_backend" not in params, (
            f"AC-2: build_output_format must not accept tool_backend. "
            f"Current signature: {sig}"
        )

    def test_build_output_format_no_kwarg_registers_sdk_prose(self) -> None:
        """Without the kwarg the function must still register the section,
        and the registered body must be the SDK prose. This catches the
        accidental case where Dev removes the kwarg but leaves the
        gating branch returning legacy prose by default.

        Sentinel: ``begin_confrontation`` appears in the SDK prose (it
        references the tool name) and NOT in the legacy prose (which
        instructs full-sidecar emit, not tool-routing). After Dev's
        rename, ``NARRATOR_OUTPUT_ONLY`` is the SDK prose so the body
        carries the sentinel; today the default is legacy prose so it
        doesn't.
        """
        import sidequest.agents.tools  # noqa: F401 — wires tool registry
        from sidequest.agents.narrator import NarratorAgent
        from sidequest.agents.prompt_framework.core import PromptRegistry

        agent = NarratorAgent()
        registry = PromptRegistry()
        agent.build_output_format(registry)

        sections = list(registry.registry(agent.name()))
        match = [s for s in sections if s.name == "narrator_output_only"]
        assert len(match) == 1, (
            f"Expected exactly one narrator_output_only section, got {len(match)}"
        )
        body = match[0].content
        assert "begin_confrontation" in body, (
            "AC-2: with no kwarg, build_output_format must register SDK prose. "
            "SDK prose references tool names (e.g. begin_confrontation); "
            "legacy prose does not. The default branch must return the SDK "
            "prose, not silently fall back to legacy."
        )

    @pytest.mark.asyncio
    async def test_orchestrator_call_site_passes_no_tool_backend(
        self,
        simple_turn_context_turn_three,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The single call site at ``orchestrator.py:1463-1466`` no longer
        passes ``tool_backend=...``. Verified via reflection on a captured
        call rather than source-text grep (per CLAUDE.md §No Source-Text
        Wiring Tests).
        """
        from sidequest.agents.narrator import NarratorAgent

        from tests.agents.test_57_4_recency_guardrails_migration import (
            _make_sdk_orchestrator,
        )

        captured: dict[str, object] = {}
        original = NarratorAgent.build_output_format

        def spy(self, registry, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = dict(kwargs)
            return original(self, registry, *args, **kwargs)

        monkeypatch.setattr(NarratorAgent, "build_output_format", spy)

        orch = _make_sdk_orchestrator()
        await orch.build_narrator_prompt("act", simple_turn_context_turn_three)

        assert "kwargs" in captured, (
            "build_output_format was never called during prompt build — "
            "the orchestrator wiring is broken."
        )
        assert "tool_backend" not in captured["kwargs"], (
            f"AC-2: orchestrator.build_narrator_prompt must not pass "
            f"tool_backend= to build_output_format. Captured kwargs: "
            f"{captured['kwargs']!r}"
        )


# ---------------------------------------------------------------------------
# OTEL span shape (Option a: keep span, drop tool_backend attr, hard-wire)
# ---------------------------------------------------------------------------


class TestRecencyGuardrailsSpanShape:
    """``narrator.recency_guardrails_skipped`` span post-61-9.

    Per story-context DECISION LOCKED 2026-05-24 (Option a): keep the span
    as a "migration engaged" dashboard signal, drop the now-vestigial
    ``tool_backend`` attribute, hard-wire ``guardrails_skipped`` to
    ``GUARDRAIL_NAMES`` and ``bytes_saved`` to ``TOTAL_PROSE_BYTES``.

    These tests use the existing fixture infrastructure from
    ``test_57_4_recency_guardrails_migration`` (``otel_capture``,
    ``simple_turn_context_turn_three``, ``_make_sdk_orchestrator``) — Dev
    will rewrite the legacy-path tests there during green; these tests
    define the new shape.
    """

    @pytest.mark.asyncio
    async def test_span_still_emits_on_sdk_path(
        self, simple_turn_context_turn_three, otel_capture
    ) -> None:
        """The span must keep firing — it's the constant-emit "migration
        engaged" signal the GM panel relies on for absence-of-span = bug."""
        from tests.agents.test_57_4_recency_guardrails_migration import (
            _make_sdk_orchestrator,
        )

        orch = _make_sdk_orchestrator()
        await orch.build_narrator_prompt("act", simple_turn_context_turn_three)

        spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "narrator.recency_guardrails_skipped"
        ]
        assert len(spans) == 1, (
            f"Span must still emit post-61-9 (Option a). Got {len(spans)} spans."
        )

    @pytest.mark.asyncio
    async def test_span_no_longer_carries_tool_backend_attr(
        self, simple_turn_context_turn_three, otel_capture
    ) -> None:
        """``tool_backend`` is removed (Option a) — it's a constant True
        post-AC-2 and carries no information."""
        from tests.agents.test_57_4_recency_guardrails_migration import (
            _make_sdk_orchestrator,
        )

        orch = _make_sdk_orchestrator()
        await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
        spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "narrator.recency_guardrails_skipped"
        ]
        assert spans, "Span missing — guarded by sibling test"
        attrs = dict(spans[0].attributes or {})
        assert "tool_backend" not in attrs, (
            f"Option a: tool_backend attr must be removed from the span. "
            f"Got attrs={attrs!r}"
        )

    @pytest.mark.asyncio
    async def test_span_emits_hardwired_guardrails_skipped_constant(
        self, simple_turn_context_turn_three, otel_capture
    ) -> None:
        """``guardrails_skipped`` is hard-wired to the full ``GUARDRAIL_NAMES``
        tuple — no longer gated on backend (Option a)."""
        from sidequest.agents.narrator_guardrails import GUARDRAIL_NAMES

        from tests.agents.test_57_4_recency_guardrails_migration import (
            _make_sdk_orchestrator,
        )

        orch = _make_sdk_orchestrator()
        await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
        spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "narrator.recency_guardrails_skipped"
        ]
        assert spans, "Span missing"
        attrs = dict(spans[0].attributes or {})
        skipped = attrs.get("guardrails_skipped")
        skipped_list = list(skipped) if skipped is not None else []
        assert set(skipped_list) == set(GUARDRAIL_NAMES), (
            f"Option a: guardrails_skipped must be hard-wired to "
            f"GUARDRAIL_NAMES ({sorted(GUARDRAIL_NAMES)}); got {sorted(skipped_list)}"
        )

    @pytest.mark.asyncio
    async def test_span_emits_hardwired_bytes_saved_constant(
        self, simple_turn_context_turn_three, otel_capture
    ) -> None:
        from sidequest.agents.narrator_guardrails import TOTAL_PROSE_BYTES

        from tests.agents.test_57_4_recency_guardrails_migration import (
            _make_sdk_orchestrator,
        )

        orch = _make_sdk_orchestrator()
        await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
        spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "narrator.recency_guardrails_skipped"
        ]
        assert spans, "Span missing"
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("bytes_saved") == TOTAL_PROSE_BYTES, (
            f"Option a: bytes_saved must be hard-wired to TOTAL_PROSE_BYTES "
            f"({TOTAL_PROSE_BYTES}); got {attrs.get('bytes_saved')!r}"
        )


# ---------------------------------------------------------------------------
# AC-4 — test parameterization collapse (the deletion invariant)
# ---------------------------------------------------------------------------


class TestBackendGateTestFileDeleted:
    """``test_narrator_output_format_backend_gate.py`` is deleted per AC-4.

    Its entire raison d'être is asserting legacy-vs-SDK divergence
    (architect §E single-purpose file classification). The file disappears
    when the divergence is removed. Salvageable invariants — if any —
    move into ``test_narrator.py`` or a new ``test_narrator_output_format
    .py`` during green; that re-location is a Dev judgment call, not a
    RED contract here."""

    def test_backend_gate_test_module_no_longer_exists(self) -> None:
        tests_dir = Path(__file__).parent
        legacy = tests_dir / "test_narrator_output_format_backend_gate.py"
        assert not legacy.exists(), (
            f"AC-4: {legacy} must be deleted. Architect §E classifies it as "
            "a single-purpose file whose entire raison d'être (legacy-vs-SDK "
            "divergence assertions) evaporates with the backend gate."
        )
