"""Failing tests for story 92-1: router A/B eval — Haiku vs local qwen on the
real captured Intent Router corpus.

RED phase (TEA / Radar O'Reilly). These tests fail until Dev extends
``sidequest/agents/ab_eval_harness.py`` (EXTEND, do not fork — story guardrail)
with the router-shaped layer:

  - ``RouterCorpusCapturer``        — drives the REAL ``IntentRouter.decompose``
                                      to capture Haiku-baseline rows
  - ``RouterAbEvalHarness``         — drives BOTH backends through the router's
                                      production ``emit_tool``/``DispatchPackage``
                                      path on identical captured prompts
  - ``RouterAbEvalResult`` / ``RouterAbEvalReport`` / ``TypeAgreement``
  - ``RouterAdjudication``          — qwen_wrong / haiku_wrong / both_defensible
  - ``dispatch_selection_agreement`` — the ≥95% gate metric
  - ``latency_percentiles``          — p50/p95, nearest-rank

and creates ``scripts/router_ab_eval_cli.py`` (operator layer, mirrors the
48-4 ``ab_eval_harness_cli.py`` exit-code taxonomy incl. the exit-4
Ollama-unreachable operator no-op).

Spec: sprint/context/context-story-92-1.md. The THREE measurements:
  1. dispatch-selection agreement (per dispatch type — not averaged away)
  2. qwen schema-validity rate against DispatchPackage (the named top risk)
  3. latency percentiles p50/p95 per backend

AC map:
  AC1 corpus            — tests/corpus/test_router_corpus.py + capturer tests here
  AC2 identical prompts, production call shape, deterministic re-run
                        — test_eval_capture_drives_real_router_call_shape,
                          test_capturer_drives_real_router_call_shape,
                          test_eval_corpus_deterministic_rerun
  AC3 report            — test_report_*, test_per_type_agreement_breakdown
  AC4 adjudication      — test_adjudication_*
  AC5 go/no-go          — test_go_no_go_*

Every test is CI-safe: both backends are faked at the ``IntentRouterLLM``
boundary (``FakeRouterLLM``). The live A/B is operator evidence on the
M3 Ultra via the CLI, exactly as 48-4 handles it.

Rule-enforcement (.pennyfarthing/gates/lang-review/python.md):
  #1 silent exceptions   — test_qwen_schema_invalid_recorded_not_swallowed
  #2 mutable defaults    — test_rule2_no_mutable_default_args
  #3 type annotations    — test_rule3_public_api_fully_annotated
  #9 async pitfalls      — test_rule9_one_side_failure_preserves_other
  #11 input validation   — test_latency_percentiles_empty_rejected,
                           test_cli_bad_sample_size_is_config_error
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from sidequest.agents import intent_router

# RED imports: fail loudly at collection until Dev lands the code (No Silent
# Fallbacks — same convention as tests/agents/test_ab_eval_harness.py).
from sidequest.agents.ab_eval_harness import (  # noqa: E402
    RouterAbEvalHarness,
    RouterAbEvalReport,
    RouterAbEvalResult,
    RouterAdjudication,
    RouterCorpusCapturer,
    dispatch_selection_agreement,
    latency_percentiles,
)
from sidequest.agents.ollama_client import OllamaClientError
from sidequest.corpus.router_corpus import (  # noqa: E402 — RED (92-1 corpus layer)
    ROUTER_CORPUS_SCHEMA_VERSION,
    RouterCapture,
    write_captures,
)
from sidequest.corpus.schema import MineProvenance
from sidequest.protocol.dispatch import DispatchPackage

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "router_ab_eval_cli.py"


def _load_cli_module() -> Any:
    """Import scripts/router_ab_eval_cli.py by path (not a package). Fails
    loudly if absent — that is a RED signal, not a skip."""
    spec = importlib.util.spec_from_file_location("router_ab_eval_cli", CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["router_ab_eval_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Test doubles — concrete IntentRouterLLM implementations, no network.
# --------------------------------------------------------------------------- #


def _package_dict(
    *subsystems: tuple[str, dict[str, Any]],
    turn_id: str = "t1",
) -> dict[str, Any]:
    """A schema-valid DispatchPackage tool_input dict (what emit_tool returns)."""
    return {
        "turn_id": turn_id,
        "confidence_global": 0.9,
        "per_player": [
            {
                "player_id": "p1",
                "raw_action": "probe",
                "dispatch": [
                    {
                        "subsystem": name,
                        "params": params,
                        "idempotency_key": f"{turn_id}-k{i}",
                        "visibility": {"visible_to": "all"},
                        "confidence": 0.9,
                    }
                    for i, (name, params) in enumerate(subsystems)
                ],
            }
        ],
        "cross_player": [],
    }


CONFRONTATION_MELEE = ("confrontation", {"type": "melee", "opponent": {"name": "goblin"}})
CONFRONTATION_PARLEY = ("confrontation", {"type": "parley", "opponent": {"name": "goblin"}})
MOVEMENT_DEEPER = ("movement", {"direction": "deeper"})
NPC_AGENCY = ("npc_agency", {"npc_name": "goblin"})

# A stray top-level key — DispatchPackage (extra='forbid') must reject it.
INVALID_PACKAGE_DICT = {"turn_id": "t1", "confidence_global": 0.9, "hallucinated": True}


class FakeRouterLLM:
    """Concrete ``IntentRouterLLM``: returns queued payload dicts, records
    every emit_tool call's kwargs, or raises. No network."""

    def __init__(
        self,
        payloads: list[dict[str, Any]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._payloads = list(payloads or [_package_dict(CONFRONTATION_MELEE)])
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "tool_name": tool_name,
                "tool_description": tool_description,
                "tool_schema": tool_schema,
            }
        )
        if self._raises is not None:
            raise self._raises
        if len(self._payloads) > 1:
            return self._payloads.pop(0)
        return self._payloads[0]


class NotARouterLLM:
    """No ``emit_tool`` — the harness must reject this loudly at construction
    (mirror the 48-4 LlmClient guard), not AttributeError mid-run."""

    async def send_stateless(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _capture(idx: int = 0, action: str | None = None) -> RouterCapture:
    return RouterCapture(
        schema_version=ROUTER_CORPUS_SCHEMA_VERSION,
        genre="caverns_and_claudes",
        world="beneath_sunden",
        round_number=idx,
        action=action or f"I attack the goblin chief ({idx}).",
        state_summary='{"present_npcs": ["goblin chief"], "region": "ropefoot"}',
        baseline_package=DispatchPackage.model_validate(
            _package_dict(CONFRONTATION_MELEE, turn_id=f"baseline-{idx}")
        ),
        provenance=MineProvenance(source_save="test.db", event_seq=idx),
    )


def _harness(
    haiku: FakeRouterLLM | None = None,
    qwen: FakeRouterLLM | None = None,
) -> RouterAbEvalHarness:
    return RouterAbEvalHarness(
        haiku_llm=haiku or FakeRouterLLM(),
        qwen_llm=qwen or FakeRouterLLM(),
    )


# --------------------------------------------------------------------------- #
# AC2 + wiring (CLAUDE.md mandate): the harness measures the PRODUCTION code
# path — the router's real emit_tool call shape — not a parallel
# reimplementation. Fixture-driven, not source-grep.
# --------------------------------------------------------------------------- #


async def test_eval_capture_drives_real_router_call_shape() -> None:
    haiku = FakeRouterLLM()
    qwen = FakeRouterLLM()
    cap = _capture(0, action="I grab the rope and climb.")

    await _harness(haiku, qwen).eval_capture(cap)

    for side in (haiku, qwen):
        assert side.calls, "each backend must be driven once per capture"
        call = side.calls[0]
        # The router's forced-tool contract, verbatim.
        assert call["tool_name"] == "emit_dispatch_package"
        assert call["tool_schema"] == DispatchPackage.model_json_schema()
        # The production prompt builder's envelope, fed the captured triple.
        assert "<raw_action>" in call["user"]
        assert "I grab the rope and climb." in call["user"]
        assert "<game_state>" in call["user"]
        assert "ropefoot" in call["user"]
        # The production system prompt — not a harness-local rewrite.
        assert call["system"] == intent_router._SYSTEM_PROMPT

    # Identical prompts on both sides (AC2: same (action, state_summary)).
    assert haiku.calls[0]["user"] == qwen.calls[0]["user"]
    assert haiku.calls[0]["system"] == qwen.calls[0]["system"]


async def test_capturer_drives_real_router_call_shape() -> None:
    """AC1: corpus capture itself goes through the production
    ``IntentRouter.decompose`` so the baseline package is exactly what
    production Haiku would have emitted."""
    llm = FakeRouterLLM(payloads=[_package_dict(MOVEMENT_DEEPER)])
    capturer = RouterCorpusCapturer(llm=llm)

    cap = await capturer.capture(
        action="We descend the iron stair.",
        state_summary={"region": "ropefoot"},
        genre="caverns_and_claudes",
        world="beneath_sunden",
        round_number=4,
        provenance=MineProvenance(source_save="real.db", event_seq=44),
    )

    assert llm.calls and llm.calls[0]["tool_name"] == "emit_dispatch_package"
    assert isinstance(cap, RouterCapture)
    assert cap.action == "We descend the iron stair."
    # state_summary stored as the SAME serialized string the prompt used.
    assert isinstance(cap.state_summary, str)
    assert cap.state_summary in llm.calls[0]["user"]
    captured_subsystems = {
        d.subsystem for pd in cap.baseline_package.per_player for d in pd.dispatch
    }
    assert captured_subsystems == {"movement"}


# --------------------------------------------------------------------------- #
# Core eval — both packages, validity, latency, agreement.
# --------------------------------------------------------------------------- #


async def test_eval_capture_returns_both_packages_and_latency() -> None:
    result = await _harness().eval_capture(_capture(0))

    assert isinstance(result, RouterAbEvalResult)
    assert result.haiku_valid is True
    assert result.qwen_valid is True
    assert isinstance(result.haiku_package, DispatchPackage)
    assert isinstance(result.qwen_package, DispatchPackage)
    assert result.haiku_latency_ms >= 0
    assert result.qwen_latency_ms >= 0
    assert result.agreement is True  # identical canned payloads agree


async def test_eval_corpus_aggregates_and_caps_sample_size() -> None:
    captures = [_capture(i) for i in range(5)]
    report = await _harness().eval_corpus(captures, sample_size=2)

    assert isinstance(report, RouterAbEvalReport)
    assert report.sample_size == 2
    assert len(report.results) == 2
    assert report.agreement_pct == 100.0


async def test_eval_corpus_deterministic_rerun() -> None:
    """AC2: re-running over the same corpus with deterministic backends yields
    the same numbers (modulo timing/timestamp)."""
    captures = [_capture(i) for i in range(3)]

    def _run_pair() -> tuple[FakeRouterLLM, FakeRouterLLM]:
        return (
            FakeRouterLLM(payloads=[_package_dict(CONFRONTATION_MELEE)]),
            FakeRouterLLM(payloads=[_package_dict(MOVEMENT_DEEPER)]),
        )

    r1 = await _harness(*_run_pair()).eval_corpus(captures)
    r2 = await _harness(*_run_pair()).eval_corpus(captures)

    assert r1.agreement_pct == r2.agreement_pct
    assert r1.qwen_schema_validity_pct == r2.qwen_schema_validity_pct
    assert {k: (v.agreed, v.total) for k, v in r1.per_type_agreement.items()} == {
        k: (v.agreed, v.total) for k, v in r2.per_type_agreement.items()
    }


# --------------------------------------------------------------------------- #
# Metric 1 — dispatch-selection agreement (the ≥95% gate metric).
# --------------------------------------------------------------------------- #


def test_agreement_true_for_same_subsystem_sets() -> None:
    a = DispatchPackage.model_validate(_package_dict(CONFRONTATION_MELEE, NPC_AGENCY))
    b = DispatchPackage.model_validate(_package_dict(NPC_AGENCY, CONFRONTATION_MELEE, turn_id="t2"))
    assert dispatch_selection_agreement(a, b) is True


def test_agreement_false_when_subsystem_sets_differ() -> None:
    a = DispatchPackage.model_validate(_package_dict(CONFRONTATION_MELEE))
    b = DispatchPackage.model_validate(_package_dict(MOVEMENT_DEEPER, turn_id="t2"))
    assert dispatch_selection_agreement(a, b) is False


def test_agreement_requires_confrontation_type_match() -> None:
    """The load-bearing confrontation param: same subsystem set but a melee
    vs parley classification is a DISAGREEMENT — the wrong engine would seat
    the encounter. (Spec: 'and the load-bearing params like confrontation
    type/opponent'.)"""
    a = DispatchPackage.model_validate(_package_dict(CONFRONTATION_MELEE))
    b = DispatchPackage.model_validate(_package_dict(CONFRONTATION_PARLEY, turn_id="t2"))
    assert dispatch_selection_agreement(a, b) is False


def test_agreement_no_dispatch_on_both_sides_agrees() -> None:
    """Both backends deciding 'no mechanical engagement' is agreement —
    a pure-narration turn must not count against the gate metric."""
    a = DispatchPackage.model_validate(_package_dict())
    b = DispatchPackage.model_validate(_package_dict(turn_id="t2"))
    assert dispatch_selection_agreement(a, b) is True


async def test_per_type_agreement_breakdown() -> None:
    """AC3: agreement is reported PER DISPATCH TYPE so a single weak class is
    visible, not averaged away. Here confrontation agrees 1/1 while the
    movement/npc_agency split disagrees — the per-type table must show it."""
    haiku = FakeRouterLLM(
        payloads=[
            _package_dict(CONFRONTATION_MELEE),
            _package_dict(MOVEMENT_DEEPER, turn_id="t2"),
        ]
    )
    qwen = FakeRouterLLM(
        payloads=[
            _package_dict(CONFRONTATION_MELEE),
            _package_dict(NPC_AGENCY, turn_id="t2"),
        ]
    )

    report = await _harness(haiku, qwen).eval_corpus([_capture(0), _capture(1)])

    assert report.agreement_pct == 50.0
    per_type = report.per_type_agreement
    assert per_type["confrontation"].agreed == 1
    assert per_type["confrontation"].total == 1
    # A type emitted by EITHER side on a disagreeing turn counts in its total.
    assert per_type["movement"].agreed == 0
    assert per_type["movement"].total == 1
    assert per_type["npc_agency"].agreed == 0
    assert per_type["npc_agency"].total == 1


# --------------------------------------------------------------------------- #
# Metric 2 — qwen schema-validity rate (the epic's named top risk).
# --------------------------------------------------------------------------- #


async def test_qwen_schema_invalid_recorded_not_swallowed() -> None:
    """qwen emitting a DispatchPackage that fails ``model_validate`` (stray
    key, extra='forbid') must be RECORDED as qwen-invalid with the cause —
    never raised through (losing the Haiku side) and never silently counted
    as agreement. The router's bounded retry means the fake gets called twice;
    both attempts return the same bad shape."""
    qwen = FakeRouterLLM(payloads=[INVALID_PACKAGE_DICT])
    result = await _harness(qwen=qwen).eval_capture(_capture(0))

    assert result.haiku_valid is True
    assert result.qwen_valid is False
    assert result.qwen_package is None
    assert result.agreement is False
    assert result.qwen_errors
    assert any(msg.strip() for msg in result.qwen_errors)


async def test_report_schema_validity_pct() -> None:
    qwen = FakeRouterLLM(
        payloads=[
            _package_dict(CONFRONTATION_MELEE),
            INVALID_PACKAGE_DICT,
            INVALID_PACKAGE_DICT,  # retry of capture 2 also fails
        ]
    )
    report = await _harness(qwen=qwen).eval_corpus([_capture(0), _capture(1)])

    assert report.sample_size == 2
    assert report.qwen_schema_validity_pct == 50.0


# --------------------------------------------------------------------------- #
# Metric 3 — latency percentiles (p50/p95, nearest-rank).
# --------------------------------------------------------------------------- #


def test_latency_percentiles_known_vector() -> None:
    values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    p50, p95 = latency_percentiles(values)
    # Nearest-rank: ceil(0.50 * 10) = 5th → 500; ceil(0.95 * 10) = 10th → 1000.
    assert p50 == 500
    assert p95 == 1000


def test_latency_percentiles_unsorted_input() -> None:
    p50, p95 = latency_percentiles([900, 100, 500])
    assert p50 == 500
    assert p95 == 900


def test_latency_percentiles_empty_rejected() -> None:
    """Rule #11: percentiles of nothing is operator nonsense — fail loud,
    never return a fabricated 0.0 (No Silent Fallbacks)."""
    with pytest.raises(ValueError):
        latency_percentiles([])


async def test_report_carries_latency_percentiles_per_backend() -> None:
    report = await _harness().eval_corpus([_capture(i) for i in range(3)])
    for attr in (
        "haiku_latency_p50_ms",
        "haiku_latency_p95_ms",
        "qwen_latency_p50_ms",
        "qwen_latency_p95_ms",
    ):
        value = getattr(report, attr)
        assert isinstance(value, (int, float)) and value >= 0, attr


# --------------------------------------------------------------------------- #
# AC4 — disagreement adjudication: qwen-right-Haiku-wrong counts FOR qwen.
# --------------------------------------------------------------------------- #


async def _fifty_pct_report() -> RouterAbEvalReport:
    haiku = FakeRouterLLM(
        payloads=[_package_dict(CONFRONTATION_MELEE), _package_dict(MOVEMENT_DEEPER, turn_id="t2")]
    )
    qwen = FakeRouterLLM(
        payloads=[_package_dict(CONFRONTATION_MELEE), _package_dict(NPC_AGENCY, turn_id="t2")]
    )
    return await _harness(haiku, qwen).eval_corpus([_capture(0), _capture(1)])


def test_adjudication_enum_members() -> None:
    assert {a.name for a in RouterAdjudication} >= {
        "QWEN_WRONG",
        "HAIKU_WRONG",
        "BOTH_DEFENSIBLE",
    }


async def test_adjudication_haiku_wrong_counts_for_qwen() -> None:
    report = await _fifty_pct_report()
    assert report.agreement_pct == 50.0

    adjudicated = report.adjudicated_agreement_pct({1: RouterAdjudication.HAIKU_WRONG})
    assert adjudicated == 100.0


async def test_adjudication_qwen_wrong_keeps_disagreement() -> None:
    report = await _fifty_pct_report()
    adjudicated = report.adjudicated_agreement_pct({1: RouterAdjudication.QWEN_WRONG})
    assert adjudicated == 50.0


async def test_adjudication_both_defensible_counts_for_qwen() -> None:
    """A both-defensible split is not a local-model failure — it must not
    block the flip."""
    report = await _fifty_pct_report()
    adjudicated = report.adjudicated_agreement_pct({1: RouterAdjudication.BOTH_DEFENSIBLE})
    assert adjudicated == 100.0


# --------------------------------------------------------------------------- #
# AC5 — go/no-go: threshold on adjudicated agreement + latency budget +
# schema-validity floor. The artifact 92-2 gates on.
# --------------------------------------------------------------------------- #


async def test_go_no_go_pass() -> None:
    report = await _harness().eval_corpus([_capture(i) for i in range(3)])
    verdict = report.go_no_go(
        agreement_threshold_pct=95.0,
        p95_budget_ms=10_000.0,
        schema_validity_floor_pct=90.0,
    )
    assert verdict.go is True


async def test_go_no_go_fails_below_agreement_threshold() -> None:
    report = await _fifty_pct_report()  # 50% < 95%
    verdict = report.go_no_go(
        agreement_threshold_pct=95.0,
        p95_budget_ms=10_000.0,
        schema_validity_floor_pct=0.0,
    )
    assert verdict.go is False
    assert any("agreement" in r.lower() for r in verdict.reasons)


async def test_go_no_go_fails_on_schema_validity_floor() -> None:
    """Schema validity is a FLOOR: below it the flip is unsafe regardless of
    agreement on the rows that did validate."""
    qwen = FakeRouterLLM(payloads=[INVALID_PACKAGE_DICT])
    report = await _harness(qwen=qwen).eval_corpus([_capture(0)])
    verdict = report.go_no_go(
        agreement_threshold_pct=0.0,
        p95_budget_ms=10_000.0,
        schema_validity_floor_pct=90.0,
    )
    assert verdict.go is False
    assert any("schema" in r.lower() or "valid" in r.lower() for r in verdict.reasons)


async def test_go_no_go_fails_over_latency_budget() -> None:
    report = await _harness().eval_corpus([_capture(0)])
    verdict = report.go_no_go(
        agreement_threshold_pct=0.0,
        p95_budget_ms=-1.0,  # impossible budget — any real latency exceeds it
        schema_validity_floor_pct=0.0,
    )
    assert verdict.go is False
    assert any("latency" in r.lower() or "p95" in r.lower() for r in verdict.reasons)


async def test_report_markdown_contains_gate_metrics() -> None:
    report = await _fifty_pct_report()
    md = report.to_markdown().lower()
    assert "agreement" in md
    assert "p95" in md
    assert "confrontation" in md  # the per-type table is present
    assert "movement" in md


# --------------------------------------------------------------------------- #
# Failure isolation + infra propagation (48-4 doctrine carried forward).
# --------------------------------------------------------------------------- #


async def test_rule9_one_side_failure_preserves_other() -> None:
    """A haiku-side API failure (→ IntentRouterFailure after the bounded
    retry) is recorded per-side; the qwen result survives."""
    haiku = FakeRouterLLM(raises=RuntimeError("haiku API 500"))
    result = await _harness(haiku=haiku).eval_capture(_capture(0))

    assert result.haiku_valid is False
    assert result.haiku_errors
    assert result.qwen_valid is True


async def test_ollama_unreachable_propagates_not_recorded() -> None:
    """Infrastructure failure ≠ bad output: an OllamaClientError from the
    qwen side means there is no meaningful A/B to record. It must escape
    eval_capture so the CLI can emit the operator-evidence no-op — never be
    folded into a per-side 'invalid' (which would poison the validity metric
    with infra noise)."""
    qwen = FakeRouterLLM(raises=OllamaClientError("ollama transport error: HTTP 000"))
    with pytest.raises(OllamaClientError):
        await _harness(qwen=qwen).eval_capture(_capture(0))


async def test_harness_rejects_non_router_llm_loudly() -> None:
    """An object without emit_tool (e.g. a narration-shaped LlmClient) must
    be rejected with a clear domain error at construction — not an
    AttributeError mid-run. Mirrors the 48-4 LlmClient guard."""
    with pytest.raises(Exception) as excinfo:
        RouterAbEvalHarness(
            haiku_llm=NotARouterLLM(),  # type: ignore[arg-type]
            qwen_llm=FakeRouterLLM(),
        )
    assert not isinstance(excinfo.value, AttributeError)


# --------------------------------------------------------------------------- #
# Rule #2 / #3 — mutable defaults, boundary annotations.
# --------------------------------------------------------------------------- #


def test_rule2_no_mutable_default_args() -> None:
    for fn in (
        RouterAbEvalHarness.__init__,
        RouterAbEvalHarness.eval_capture,
        RouterAbEvalHarness.eval_corpus,
        RouterCorpusCapturer.capture,
    ):
        sig = inspect.signature(fn)
        for name, param in sig.parameters.items():
            assert not isinstance(param.default, (list, dict, set)), (
                f"{fn.__qualname__} param {name!r} has mutable default"
            )


async def test_rule2_result_error_lists_isolated() -> None:
    harness = _harness()
    r1 = await harness.eval_capture(_capture(0))
    r2 = await harness.eval_capture(_capture(1))
    assert r1 is not r2
    assert r1.qwen_errors is not r2.qwen_errors
    assert r1.haiku_errors is not r2.haiku_errors


def test_rule3_public_api_fully_annotated() -> None:
    for fn in (
        RouterAbEvalHarness.eval_capture,
        RouterAbEvalHarness.eval_corpus,
        RouterCorpusCapturer.capture,
        dispatch_selection_agreement,
        latency_percentiles,
    ):
        sig = inspect.signature(fn)
        assert sig.return_annotation is not inspect.Signature.empty, (
            f"{getattr(fn, '__qualname__', fn)} missing return annotation"
        )
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            assert param.annotation is not inspect.Parameter.empty, (
                f"{getattr(fn, '__qualname__', fn)} param {name!r} missing annotation"
            )


# --------------------------------------------------------------------------- #
# CLI — operator layer (mirrors 48-4's exit-code taxonomy).
# --------------------------------------------------------------------------- #


def test_cli_module_defines_exit_code_constants() -> None:
    cli = _load_cli_module()
    for const in ("EXIT_PASS", "EXIT_CONFIG_ERROR", "EXIT_OLLAMA_UNREACHABLE"):
        assert hasattr(cli, const), f"CLI must define module-level {const}"
        assert isinstance(getattr(cli, const), int)
    assert cli.EXIT_PASS == 0
    assert cli.EXIT_CONFIG_ERROR != 0
    assert cli.EXIT_OLLAMA_UNREACHABLE != 0
    assert cli.EXIT_CONFIG_ERROR != cli.EXIT_OLLAMA_UNREACHABLE


def test_cli_missing_corpus_file_is_config_error() -> None:
    cli = _load_cli_module()
    rc = cli.main(["--corpus-jsonl", "/definitely/not/here.jsonl"])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_cli_bad_sample_size_is_config_error(tmp_path: Path) -> None:
    cli = _load_cli_module()
    corpus = tmp_path / "corpus.jsonl"
    write_captures(corpus, [_capture(0)])
    rc = cli.main(["--corpus-jsonl", str(corpus), "--sample-size", "-3"])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_cli_malformed_corpus_line_is_config_error(tmp_path: Path) -> None:
    """Rule #8/#11: a corrupt corpus line surfaces as a clean config error,
    not a traceback and not a silently-skipped row."""
    cli = _load_cli_module()
    bad = tmp_path / "corpus.jsonl"
    bad.write_text('{"not": "a RouterCapture"}\n{ broken\n', encoding="utf-8")
    rc = cli.main(["--corpus-jsonl", str(bad)])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_cli_success_writes_markdown_report(tmp_path: Path, monkeypatch: Any) -> None:
    """main() exits 0 and writes the report. The harness is substituted at
    the CLI's own module symbol — proving the CLI is WIRED to
    RouterAbEvalHarness (verify wiring, not just existence)."""
    cli = _load_cli_module()
    assert hasattr(cli, "RouterAbEvalHarness"), "CLI must import/use RouterAbEvalHarness"

    corpus = tmp_path / "corpus.jsonl"
    write_captures(corpus, [_capture(0)])
    out_md = tmp_path / "report.md"

    class _FakeReport:
        def to_markdown(self) -> str:
            return "# Router A/B Report\nagreement 100% p95 12ms\n"

    class _FakeHarness:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def eval_corpus(self, *a: Any, **k: Any) -> Any:
            return _FakeReport()

    monkeypatch.setattr(cli, "RouterAbEvalHarness", _FakeHarness)
    rc = cli.main(["--corpus-jsonl", str(corpus), "--output-md", str(out_md)])

    assert rc == cli.EXIT_PASS
    assert out_md.exists()
    assert "agreement" in out_md.read_text(encoding="utf-8").lower()


def test_cli_ollama_unreachable_writes_operator_note(tmp_path: Path, monkeypatch: Any) -> None:
    """AC (operator evidence): unreachable Ollama is a graceful no-op with a
    documented note + distinct exit code — the live A/B only runs on the
    M3 Ultra (mirror of the 48-4 exit-4 contract)."""
    cli = _load_cli_module()
    corpus = tmp_path / "corpus.jsonl"
    write_captures(corpus, [_capture(0)])
    out_md = tmp_path / "report.md"

    class _UnreachableHarness:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def eval_corpus(self, *a: Any, **k: Any) -> Any:
            raise OllamaClientError("ollama /api/chat transport error: HTTP 000")

    monkeypatch.setattr(cli, "RouterAbEvalHarness", _UnreachableHarness)
    rc = cli.main(["--corpus-jsonl", str(corpus), "--output-md", str(out_md)])

    assert rc == cli.EXIT_OLLAMA_UNREACHABLE
    note = out_md.read_text(encoding="utf-8").lower() if out_md.exists() else ""
    assert "ollama" in note or "operator" in note or "m3" in note


# --------------------------------------------------------------------------- #
# CI-safety + wiring.
# --------------------------------------------------------------------------- #


def test_suite_has_no_live_backend_calls() -> None:
    """This suite must never construct a real backend (the live A/B is
    operator evidence on the M3 Ultra). AST scan, same as 48-4's AC3."""
    import ast

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"OllamaClient", "ClaudeClient", "AnthropicSdkClient", "build_intent_router_llm"}
    called: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in banned
        ):
            called.add(node.func.id)
    assert not called, (
        f"CI-safety violation: this suite must not construct real backends; "
        f"found {sorted(called)}. Use FakeRouterLLM / monkeypatch."
    )


def test_wiring_router_harness_importable_and_cli_consumes_it() -> None:
    from sidequest.agents import ab_eval_harness as mod

    assert hasattr(mod, "RouterAbEvalHarness"), (
        "story guardrail: EXTEND ab_eval_harness.py, do not fork — the router "
        "harness must live in the 48-4 module"
    )
    cli = _load_cli_module()
    assert getattr(cli, "RouterAbEvalHarness", None) is not None, (
        "scripts/router_ab_eval_cli.py must import RouterAbEvalHarness — a "
        "harness with no non-test consumer is not wired"
    )


# --------------------------------------------------------------------------- #
# Round-trip 1 (Reviewer [HIGH]): RouterCorpusCapturer must be WIRED — the CLI
# needs a capture mode so an operator can PRODUCE the AC1 corpus, not just
# evaluate one. Prompt rows are JSONL objects:
#   {"action": str, "state_summary": str|object, "genre": str, "world": str,
#    "round_number": int, "source_save": str, "event_seq": int|null}
# --------------------------------------------------------------------------- #


def _prompt_row(idx: int = 0) -> dict[str, Any]:
    return {
        "action": f"I kick over the brazier ({idx}).",
        "state_summary": {"region": "ropefoot", "present_npcs": ["goblin chief"]},
        "genre": "caverns_and_claudes",
        "world": "beneath_sunden",
        "round_number": idx,
        "source_save": "real.db",
        "event_seq": idx,
    }


def _write_prompt_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    import json as _json

    path.write_text(
        "".join(_json.dumps(r) + "\n" for r in rows),
        encoding="utf-8",
    )


def test_cli_capture_mode_end_to_end_with_real_capturer(tmp_path: Path, monkeypatch: Any) -> None:
    """The strong wiring test (Reviewer [HIGH]): drive cli.main() --capture
    through the REAL RouterCorpusCapturer — only the env/network boundary
    (the Haiku LLM factory consumed by the CLI) is substituted. The output
    must be a valid RouterCapture JSONL re-readable by read_captures, with
    baseline packages from the (fake) router call."""
    cli = _load_cli_module()
    assert getattr(cli, "RouterCorpusCapturer", None) is not None, (
        "scripts/router_ab_eval_cli.py must import RouterCorpusCapturer — a "
        "capturer with no non-test consumer is not wired (CLAUDE.md)"
    )

    prompts = tmp_path / "prompts.jsonl"
    _write_prompt_rows(prompts, [_prompt_row(0), _prompt_row(1)])
    out = tmp_path / "corpus.jsonl"

    fake = FakeRouterLLM(payloads=[_package_dict(MOVEMENT_DEEPER)])
    monkeypatch.setattr(cli, "build_intent_router_llm", lambda: fake)

    rc = cli.main(["--capture", "--prompts-jsonl", str(prompts), "--out", str(out)])

    assert rc == cli.EXIT_PASS
    from sidequest.corpus.router_corpus import read_captures

    rows = list(read_captures(out))
    assert len(rows) == 2
    assert rows[0].action == "I kick over the brazier (0)."
    assert rows[1].round_number == 1
    assert rows[0].genre == "caverns_and_claudes"
    assert rows[0].provenance.source_save == "real.db"
    # The baseline came from the (substituted) router call, proving the real
    # capturer ran the production decompose path.
    subsystems = {d.subsystem for pd in rows[0].baseline_package.per_player for d in pd.dispatch}
    assert subsystems == {"movement"}
    # state_summary stored as the serialized string the prompt used.
    assert isinstance(rows[0].state_summary, str)
    assert "ropefoot" in rows[0].state_summary


def test_cli_capture_missing_prompts_file_is_config_error() -> None:
    cli = _load_cli_module()
    rc = cli.main(["--capture", "--prompts-jsonl", "/not/here.jsonl", "--out", "/tmp/x.jsonl"])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_cli_capture_malformed_prompt_row_is_config_error(tmp_path: Path) -> None:
    """Rule #8/#11: a corrupt or wrong-shape prompt row surfaces as a clean
    config error — never a traceback, never a silently skipped row (a
    quietly shrunk corpus lies about coverage)."""
    cli = _load_cli_module()
    bad = tmp_path / "prompts.jsonl"
    bad.write_text('{"action": ""}\n{ broken\n', encoding="utf-8")
    rc = cli.main(["--capture", "--prompts-jsonl", str(bad), "--out", str(tmp_path / "o.jsonl")])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_cli_capture_backend_failure_is_loud(tmp_path: Path, monkeypatch: Any) -> None:
    """A Haiku-side failure mid-capture (after N successful rows) must fail
    the run loudly with a non-zero exit — half a corpus written silently
    would masquerade as full coverage. The atomic writer guarantees the
    output file is either complete or absent."""
    cli = _load_cli_module()
    prompts = tmp_path / "prompts.jsonl"
    _write_prompt_rows(prompts, [_prompt_row(0), _prompt_row(1)])
    out = tmp_path / "corpus.jsonl"

    fake = FakeRouterLLM(raises=RuntimeError("haiku API 500"))
    monkeypatch.setattr(cli, "build_intent_router_llm", lambda: fake)

    rc = cli.main(["--capture", "--prompts-jsonl", str(prompts), "--out", str(out)])

    assert rc != cli.EXIT_PASS
    assert not out.exists(), "a failed capture run must not leave a partial corpus file"
