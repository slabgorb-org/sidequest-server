"""Failing tests for story 48-4: A/B evaluation harness — Claude vs local Qwen.

RED phase (TEA / Radar O'Reilly). These tests fail until Dev creates:
  - ``sidequest/agents/ab_eval_harness.py``  → ``AbEvalHarness``, ``AbEvalResult``,
    ``AbEvalReport``
  - ``scripts/ab_eval_harness_cli.py``       → ``main(argv)`` + module-level
    ``EXIT_*`` int constants

Every test is CI-safe: both the Claude-family and Ollama backends are mocked
at the ``LlmClient`` boundary (``FakeLlmClient`` below). No test reaches a live
model — that is AC3, and it is the deliberate mitigation for the constraint
that Ollama only runs on Keith's M3 Ultra (mirrors story 48-2's
``ollama_latency_check.py`` operator-evidence pattern).

Authoritative spec source: ``.session/48-4-session.md`` (per the SM Assessment,
the Acceptance Criteria there are authoritative — there is no external Group F
design doc).

AC map:
  AC1 core harness    — test_eval_pair_*, test_eval_batch_*
  AC2 CLI script      — test_cli_*
  AC3 CI-safe          — test_ac3_suite_has_no_live_backend_calls
  AC4 operator note    — test_cli_ollama_unreachable_writes_operator_note
  AC5 48-2 pattern     — test_ac5_*

Rule-enforcement (.pennyfarthing/gates/lang-review/python.md):
  #1 silent-exceptions      — test_rule1_backend_error_recorded_not_swallowed
  #2 mutable-defaults       — test_rule2_no_mutable_default_args,
                              test_rule2_result_error_lists_isolated
  #3 type-annotations       — test_rule3_public_api_fully_annotated
  #8 unsafe-deserialization — test_rule8_malformed_backend_json_flagged_invalid,
                              test_rule8_malformed_jsonl_line_rejected
  #9 async-pitfalls         — test_rule9_one_backend_failure_preserves_other
  #11 input-validation      — test_cli_bad_sample_size_is_config_error,
                              test_cli_missing_input_file_is_config_error
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Module-under-test imports. These raise ImportError until Dev lands the code,
# which is the intended RED signal. Collected at import time on purpose: a
# missing module must fail loudly (CLAUDE.md: No Silent Fallbacks), not be
# papered over with importorskip.
# --------------------------------------------------------------------------- #
from sidequest.agents.ab_eval_harness import (  # noqa: E402
    AbEvalHarness,
    AbEvalReport,
    AbEvalResult,
)
from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.ollama_client import OllamaClientError
from sidequest.corpus.schema import MineProvenance, TrainingPair

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "ab_eval_harness_cli.py"


def _load_cli_module() -> Any:
    """Import scripts/ab_eval_harness_cli.py by path (it is not a package).

    Mirrors the importlib pattern story 48-2 used for its operator script.
    Fails loudly if the script is absent — that is a RED signal, not a skip.
    """
    spec = importlib.util.spec_from_file_location("ab_eval_harness_cli", CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ab_eval_harness_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Test doubles — concrete LlmClient implementations, no network.
# --------------------------------------------------------------------------- #


class FakeLlmClient:
    """Minimal concrete ``LlmClient`` returning a canned response or raising.

    A concrete class (not AsyncMock) so ``isinstance(c, LlmClient)`` is true
    for the runtime-checkable protocol and so the awaited coroutine returns a
    real ``ClaudeResponse``.
    """

    def __init__(
        self,
        text: str = "narration.\n{}",
        backend: str = "fake",
        raises: Exception | None = None,
    ) -> None:
        self._text = text
        self._backend = backend
        self._raises = raises
        self.calls: list[tuple[str, str, str]] = []

    def capabilities(self) -> Any:
        from sidequest.agents.claude_client import LlmCapabilities

        return LlmCapabilities(
            backend_id=self._backend,
            supports_sessions=False,
            supports_tools=False,
            max_context_tokens=8192,
        )

    async def send_with_model(self, prompt: str, model: str) -> ClaudeResponse:
        raise NotImplementedError

    async def send_with_session(self, *a: Any, **k: Any) -> ClaudeResponse:
        raise NotImplementedError

    async def send_stateless(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ClaudeResponse:
        self.calls.append((system_prompt, user_message, model))
        if self._raises is not None:
            raise self._raises
        return ClaudeResponse(text=self._text, backend=self._backend)


class ToolingOnlyClient:
    """Stand-in for the default ``anthropic_sdk`` backend: a tooling client
    with NO ``send_stateless``. The harness/CLI must reject this loudly
    (mirrors ollama_latency_check.py's isinstance guard), not AttributeError.
    """

    async def complete_with_tools(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _training_pair(idx: int, *, genre: str = "caverns_and_claudes") -> TrainingPair:
    """Build a REAL TrainingPair. NOTE: the session pseudocode used
    ``TrainingPair(user_prompt=, expected_response=)`` which does not exist —
    the real schema (sidequest/corpus/schema.py) is input_text/output_text
    plus required schema_version/genre/world/round_number/provenance. Tests
    pin Dev to the real schema. (Logged as a TEA deviation.)
    """
    return TrainingPair(
        schema_version=1,
        genre=genre,
        world="beneath_sunden",
        round_number=idx,
        input_text=f"The party does thing {idx}.",
        output_text=f"Canonical narrator response {idx}.",
        provenance=MineProvenance(source_save="test.db", event_seq=idx),
    )


VALID_PATCH_RESPONSE = 'The torch gutters.\n{"intent": "move", "patch": {"location": "cave"}}'
MALFORMED_PATCH_RESPONSE = "The torch gutters.\n{not valid json at all"


# --------------------------------------------------------------------------- #
# AC1 — Core harness: eval_pair / eval_batch
# --------------------------------------------------------------------------- #


async def test_eval_pair_runs_both_backends_and_compares() -> None:
    claude = FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="claude")
    ollama = FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="ollama")
    harness = AbEvalHarness(
        claude_client=claude,
        ollama_client=ollama,
        system_prompt="You are a SideQuest narrator.",
        genre="caverns_and_claudes",
    )

    result = await harness.eval_pair(user_prompt="The party enters the cave.")

    assert isinstance(result, AbEvalResult)
    # Both backends were actually invoked with the player's prompt.
    assert claude.calls and "cave" in claude.calls[0][1]
    assert ollama.calls and "cave" in ollama.calls[0][1]
    assert result.claude_patch_valid is True
    assert result.ollama_patch_valid is True
    # Identical canned text ⇒ similarity at the top of the 0..1 range.
    assert result.narration_similarity == pytest.approx(1.0, abs=1e-6)
    # Latency is measured, non-negative, and the ratio is derived from it.
    assert result.claude_duration_ms >= 0
    assert result.ollama_duration_ms >= 0


async def test_eval_batch_aggregates_real_training_pairs() -> None:
    claude = FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="claude")
    ollama = FakeLlmClient(text=MALFORMED_PATCH_RESPONSE, backend="ollama")
    harness = AbEvalHarness(
        claude_client=claude,
        ollama_client=ollama,
        system_prompt="sys",
        genre="caverns_and_claudes",
    )
    pairs = [_training_pair(i) for i in range(3)]

    report = await harness.eval_batch(pairs, sample_size=3)

    assert isinstance(report, AbEvalReport)
    assert report.sample_size == 3
    assert len(report.results) == 3
    # Claude produced valid patches every time; Ollama never did.
    assert report.claude_patch_valid_pct == pytest.approx(100.0)
    assert report.ollama_patch_valid_pct == pytest.approx(0.0)
    assert 0.0 <= report.avg_narration_similarity <= 1.0
    md = report.to_markdown()
    assert isinstance(md, str) and md.strip()
    # The report must surface both backends so a human can diff them.
    assert "claude" in md.lower() and "ollama" in md.lower()


async def test_eval_batch_sample_size_caps_results() -> None:
    harness = AbEvalHarness(
        claude_client=FakeLlmClient(),
        ollama_client=FakeLlmClient(),
        system_prompt="sys",
        genre="caverns_and_claudes",
    )
    pairs = [_training_pair(i) for i in range(10)]

    report = await harness.eval_batch(pairs, sample_size=4)

    assert report.sample_size == 4
    assert len(report.results) == 4


# --------------------------------------------------------------------------- #
# Rule #8 (unsafe deserialization) + #1 (no silent swallow): malformed model
# output must be RECORDED as invalid with an error message, never trusted and
# never crash the run.
# --------------------------------------------------------------------------- #


async def test_rule8_malformed_backend_json_flagged_invalid() -> None:
    harness = AbEvalHarness(
        claude_client=FakeLlmClient(text=VALID_PATCH_RESPONSE),
        ollama_client=FakeLlmClient(text=MALFORMED_PATCH_RESPONSE),
        system_prompt="sys",
        genre="caverns_and_claudes",
    )

    result = await harness.eval_pair(user_prompt="probe")

    assert result.claude_patch_valid is True
    assert result.ollama_patch_valid is False
    # The failure is explained, not swallowed.
    assert result.ollama_patch_errors
    assert any(msg.strip() for msg in result.ollama_patch_errors)


def test_rule8_malformed_jsonl_line_rejected(tmp_path: Any) -> None:
    """A corrupt line in the input JSONL is an untrusted boundary input
    (rule #8 / #11): the CLI must return the config-error exit code, not
    crash with a raw JSONDecodeError and not silently skip the bad line.
    """
    cli = _load_cli_module()
    bad = tmp_path / "corpus.jsonl"
    bad.write_text('{"this": "is not a TrainingPair"}\n{ broken json\n', encoding="utf-8")

    rc = cli.main(["--input-jsonl", str(bad)])

    assert rc == cli.EXIT_CONFIG_ERROR, (
        "malformed/invalid JSONL must surface as a clean config error, "
        "not an unhandled traceback or a silently-skipped line"
    )


# --------------------------------------------------------------------------- #
# Rule #9 (async pitfalls): one backend failing must NOT lose the other's
# result. A bare asyncio.gather() without return_exceptions cancels the
# sibling — that would be a silent data-loss bug.
# --------------------------------------------------------------------------- #


async def test_rule9_one_backend_failure_preserves_other() -> None:
    claude = FakeLlmClient(raises=RuntimeError("claude API 500"))
    ollama = FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="ollama")
    harness = AbEvalHarness(
        claude_client=claude,
        ollama_client=ollama,
        system_prompt="sys",
        genre="caverns_and_claudes",
    )

    result = await harness.eval_pair(user_prompt="probe")

    # The Ollama side still ran and is preserved.
    assert ollama.calls
    assert result.ollama_patch_valid is True
    # The Claude failure is recorded as an invalid patch with the cause,
    # not raised through and not silently dropped.
    assert result.claude_patch_valid is False
    assert result.claude_patch_errors
    assert any("500" in m or "claude" in m.lower() for m in result.claude_patch_errors)


# --------------------------------------------------------------------------- #
# AC4 regression (Reviewer HIGH): the REAL harness must propagate
# OllamaClientError (infrastructure failure) instead of absorbing it per-side,
# so the CLI can emit the operator-evidence no-op. rule-#9 isolation must
# still apply to NON-infrastructure ollama failures.
# --------------------------------------------------------------------------- #


async def test_real_harness_propagates_ollama_unreachable() -> None:
    """A hard OllamaClientError must escape eval_pair, NOT be folded into a
    recorded _Side. Otherwise the CLI's AC4 exit-4 path is dead in prod.
    """
    harness = AbEvalHarness(
        claude_client=FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="claude"),
        ollama_client=FakeLlmClient(
            backend="ollama",
            raises=OllamaClientError("ollama /api/chat transport error: HTTP 000"),
        ),
        system_prompt="sys",
        genre="caverns_and_claudes",
    )
    with pytest.raises(OllamaClientError):
        await harness.eval_pair(user_prompt="probe")


async def test_real_harness_isolates_non_infra_ollama_failure() -> None:
    """rule-#9 still holds: a NON-OllamaClientError from the ollama side is
    recorded per-side, not propagated (only infra failure propagates).
    """
    harness = AbEvalHarness(
        claude_client=FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="claude"),
        ollama_client=FakeLlmClient(
            backend="ollama", raises=RuntimeError("ollama produced garbage")
        ),
        system_prompt="sys",
        genre="caverns_and_claudes",
    )
    result = await harness.eval_pair(user_prompt="probe")
    assert result.claude_patch_valid is True
    assert result.ollama_patch_valid is False
    assert any("garbage" in m or "ollama" in m.lower() for m in result.ollama_patch_errors)


def test_cli_real_harness_ollama_unreachable_exit4(tmp_path: Any, monkeypatch: Any) -> None:
    """Integration: drive cli.main() through the REAL AbEvalHarness (NOT
    monkeypatched). Only the unavoidable env/network boundary
    (`_build_clients`) is substituted. Proves AC4 wiring end-to-end —
    this is the test the prior suite was missing (Reviewer HIGH).
    """
    cli = _load_cli_module()
    out_md = tmp_path / "report.md"

    def _fake_build_clients() -> tuple[Any, Any]:
        return (
            FakeLlmClient(text=VALID_PATCH_RESPONSE, backend="claude"),
            FakeLlmClient(
                backend="ollama",
                raises=OllamaClientError("ollama /api/chat transport error: HTTP 000"),
            ),
        )

    # Substitute ONLY client construction. cli.AbEvalHarness is the real class.
    monkeypatch.setattr(cli, "_build_clients", _fake_build_clients)
    assert cli.AbEvalHarness is AbEvalHarness, "real harness must not be mocked"

    rc = cli.main(["--user-prompt", "probe", "--output-md", str(out_md)])

    assert rc == cli.EXIT_OLLAMA_UNREACHABLE
    note = out_md.read_text(encoding="utf-8").lower()
    assert "ollama" in note or "operator" in note or "m3" in note


# --------------------------------------------------------------------------- #
# Rule #2 (mutable defaults) + #3 (boundary annotations)
# --------------------------------------------------------------------------- #


def test_rule2_no_mutable_default_args() -> None:
    for fn in (AbEvalHarness.__init__, AbEvalHarness.eval_pair, AbEvalHarness.eval_batch):
        sig = inspect.signature(fn)
        for name, param in sig.parameters.items():
            default = param.default
            assert not isinstance(default, (list, dict, set)), (
                f"{fn.__qualname__} param {name!r} has mutable default {default!r}"
            )


async def test_rule2_result_error_lists_isolated() -> None:
    """Two AbEvalResult instances must not share the same error-list object
    (the classic mutable-default / shared-class-attr bug).
    """
    harness = AbEvalHarness(
        claude_client=FakeLlmClient(text=MALFORMED_PATCH_RESPONSE),
        ollama_client=FakeLlmClient(text=MALFORMED_PATCH_RESPONSE),
        system_prompt="sys",
        genre="caverns_and_claudes",
    )
    r1 = await harness.eval_pair(user_prompt="one")
    r2 = await harness.eval_pair(user_prompt="two")

    assert r1 is not r2
    assert r1.claude_patch_errors is not r2.claude_patch_errors


def test_rule3_public_api_fully_annotated() -> None:
    for fn in (AbEvalHarness.eval_pair, AbEvalHarness.eval_batch):
        sig = inspect.signature(fn)
        assert sig.return_annotation is not inspect.Signature.empty, (
            f"{fn.__qualname__} missing return annotation"
        )
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            assert param.annotation is not inspect.Parameter.empty, (
                f"{fn.__qualname__} param {name!r} missing type annotation"
            )


# --------------------------------------------------------------------------- #
# Loud-fail contract: a client lacking send_stateless (the default
# anthropic_sdk ToolingLlmClient) must be rejected with a clear error,
# NOT an AttributeError mid-run. Mirrors ollama_latency_check.py's guard.
# --------------------------------------------------------------------------- #


async def test_tooling_only_client_rejected_loudly() -> None:
    with pytest.raises(Exception) as excinfo:
        harness = AbEvalHarness(
            claude_client=ToolingOnlyClient(),  # type: ignore[arg-type]
            ollama_client=FakeLlmClient(),
            system_prompt="sys",
            genre="caverns_and_claudes",
        )
        await harness.eval_pair(user_prompt="probe")
    # Must not be a bare AttributeError leaking the missing method name.
    assert not isinstance(excinfo.value, AttributeError), (
        "client without send_stateless must fail with a clear domain error, "
        "not a raw AttributeError"
    )


# --------------------------------------------------------------------------- #
# AC2 — CLI script: exit-code taxonomy and markdown output
# --------------------------------------------------------------------------- #


def test_cli_module_defines_exit_code_constants() -> None:
    cli = _load_cli_module()
    for const in ("EXIT_PASS", "EXIT_CONFIG_ERROR"):
        assert hasattr(cli, const), f"CLI must define module-level {const}"
        assert isinstance(getattr(cli, const), int)
    # 0 reserved for success, distinct non-zero for config errors.
    assert cli.EXIT_PASS == 0
    assert cli.EXIT_CONFIG_ERROR != 0


def test_cli_success_writes_markdown_report(tmp_path: Any, monkeypatch: Any) -> None:
    """main() exits 0 and writes a markdown report when the harness runs.

    The harness is substituted at the CLI's own module symbol so no client
    construction or network occurs — this also asserts the CLI is *wired*
    to AbEvalHarness (CLAUDE.md: verify wiring, not just existence).
    """
    cli = _load_cli_module()
    assert hasattr(cli, "AbEvalHarness"), "CLI must import/use AbEvalHarness"

    out_md = tmp_path / "report.md"

    class _FakeReport:
        def to_markdown(self) -> str:
            return "# A/B Report\nclaude vs ollama\n"

    class _FakeHarness:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def eval_pair(self, *a: Any, **k: Any) -> Any:
            return _FakeReport()

        async def eval_batch(self, *a: Any, **k: Any) -> Any:
            return _FakeReport()

    monkeypatch.setattr(cli, "AbEvalHarness", _FakeHarness)
    rc = cli.main(["--user-prompt", "The party enters.", "--output-md", str(out_md)])

    assert rc == cli.EXIT_PASS
    assert out_md.exists()
    assert "claude" in out_md.read_text(encoding="utf-8").lower()


def test_cli_bad_sample_size_is_config_error() -> None:
    """Rule #11: a non-positive --sample-size is operator nonsense and must
    return the config-error exit code, not a traceback or a silent run.
    """
    cli = _load_cli_module()
    rc = cli.main(["--input-jsonl", "/nonexistent.jsonl", "--sample-size", "-3"])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_cli_missing_input_file_is_config_error() -> None:
    cli = _load_cli_module()
    rc = cli.main(["--input-jsonl", "/definitely/not/here.jsonl"])
    assert rc == cli.EXIT_CONFIG_ERROR


# --------------------------------------------------------------------------- #
# AC4 — operator-evidence: unreachable Ollama is a graceful no-op with a
# documented note, NOT a hard crash (the live A/B run is M3-Ultra only).
# --------------------------------------------------------------------------- #


def test_cli_ollama_unreachable_writes_operator_note(tmp_path: Any, monkeypatch: Any) -> None:
    cli = _load_cli_module()

    out_md = tmp_path / "report.md"

    class _UnreachableHarness:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def eval_pair(self, *a: Any, **k: Any) -> Any:
            raise OllamaClientError("ollama /api/chat transport error: HTTP 000")

        async def eval_batch(self, *a: Any, **k: Any) -> Any:
            raise OllamaClientError("ollama /api/chat transport error: HTTP 000")

    monkeypatch.setattr(cli, "AbEvalHarness", _UnreachableHarness)
    rc = cli.main(["--user-prompt", "probe", "--output-md", str(out_md)])

    assert hasattr(cli, "EXIT_OLLAMA_UNREACHABLE"), (
        "AC4 requires a distinct exit code for unreachable Ollama so CI can "
        "skip the live A/B run gracefully"
    )
    assert rc == cli.EXIT_OLLAMA_UNREACHABLE
    # The operator-evidence note must be surfaced (report file or stdout).
    note = out_md.read_text(encoding="utf-8").lower() if out_md.exists() else ""
    assert "ollama" in note or "operator" in note or "m3" in note


# --------------------------------------------------------------------------- #
# AC5 — conformance to the story 48-2 ollama_latency_check.py pattern.
# Source-scan checks (string tokens excluded from the scan to avoid the
# self-defeating-test trap 48-2's reviewer flagged).
# --------------------------------------------------------------------------- #


def test_ac5_cli_imports_legacy_backend_classes_at_module_top() -> None:
    """AC5 (post-61-9): the CLI must import its legacy backend classes
    at module top so a broken sidequest install fails AT SCRIPT LOAD, not
    at --help (No Silent Fallbacks). Story 61-9 retired the legacy
    backends from ``build_llm_client``; the harness now constructs
    ``ClaudeClient`` and ``OllamaClient`` directly (the classes
    themselves stay per the story scope-out).
    """
    src = CLI_PATH.read_text(encoding="utf-8")
    import ast

    tree = ast.parse(src)
    top_level_import_names: set[str] = set()
    for node in tree.body:  # module body only — top-level, not function-local
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                top_level_import_names.add(alias.name)
    assert "OllamaClient" in top_level_import_names, (
        "AC5 (post-61-9): CLI must import OllamaClient at module top so a "
        "broken sidequest install fails at load, not at --help."
    )
    assert "ClaudeClient" in top_level_import_names, (
        "AC5 (post-61-9): CLI must import ClaudeClient at module top so a "
        "broken sidequest install fails at load."
    )


def test_ac5_cli_guards_against_non_llmclient_backend() -> None:
    """AC5: the CLI must isinstance-check the constructed backends for
    ``LlmClient`` (``send_stateless`` is unavailable on
    ``ToolingLlmClient``) rather than blindly calling ``send_stateless``.
    """
    src = CLI_PATH.read_text(encoding="utf-8")
    tree_ok = "LlmClient" in src and "isinstance" in src
    assert tree_ok, (
        "AC5: CLI must guard constructed backends with an "
        "isinstance(..., LlmClient) check (mirror of ollama_latency_check.py)"
    )


# --------------------------------------------------------------------------- #
# AC3 — the suite itself proves CI-safety: no test constructs a real
# ClaudeClient/OllamaClient/AnthropicSdkClient or hits the network.
# --------------------------------------------------------------------------- #


def test_ac3_suite_has_no_live_backend_calls() -> None:
    """Scan THIS test module's source (non-comment) for real backend
    construction. All backends must be FakeLlmClient or monkeypatched.
    """
    import ast

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Story 61-9 removed ``build_llm_client`` from the CLI's surface; the
    # banned-construction guard remains for the legacy + SDK classes so the
    # test suite itself doesn't accidentally hit the network.
    banned = {"OllamaClient", "ClaudeClient", "AnthropicSdkClient"}
    called: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in banned
        ):
            called.add(node.func.id)
    assert not called, (
        f"AC3 violation: this suite must not construct real backends; found "
        f"calls to {sorted(called)}. Use FakeLlmClient / monkeypatch."
    )


# --------------------------------------------------------------------------- #
# Wiring test (CLAUDE.md mandate): AbEvalHarness must be reachable from a
# production import path and the operator CLI must consume it.
# --------------------------------------------------------------------------- #


def test_wiring_harness_importable_and_cli_consumes_it() -> None:
    from sidequest.agents import ab_eval_harness as mod

    assert hasattr(mod, "AbEvalHarness")
    cli = _load_cli_module()
    # The CLI is the non-test consumer that proves the harness is wired.
    assert getattr(cli, "AbEvalHarness", None) is not None, (
        "scripts/ab_eval_harness_cli.py must import AbEvalHarness — a harness "
        "with no non-test consumer is not wired"
    )
