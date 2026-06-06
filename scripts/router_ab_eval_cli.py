"""Operator CLI for the story 92-1 router A/B eval — Haiku vs local qwen.

Runs the ``RouterAbEvalHarness`` over a captured router corpus
(``RouterCapture`` JSONL, see ``sidequest/corpus/router_corpus.py``) and
writes the markdown gate report 92-2 consumes. Live runs only work on the
M3 Ultra (the only host where Ollama serves qwen) — anywhere else the
distinct ``EXIT_OLLAMA_UNREACHABLE`` code plus an operator note is the
documented no-op, mirroring the 48-4 ``ab_eval_harness_cli.py`` contract.

Usage:
    uv run python scripts/router_ab_eval_cli.py \
        --corpus-jsonl artifacts/router_corpus.jsonl \
        --output-md artifacts/router_ab_report.md \
        [--sample-size N] [--qwen-model qwen2.5:7b-instruct] \
        [--agreement-threshold-pct 95.0] [--p95-budget-ms 5000] \
        [--schema-validity-floor-pct 95.0]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Module-top imports (48-4 AC5 doctrine): a broken sidequest install fails AT
# SCRIPT LOAD, not at --help.
from sidequest.agents.ab_eval_harness import (
    OLLAMA_MODEL,
    QwenRouterLlm,
    RouterAbEvalHarness,
)
from sidequest.agents.claude_client import LlmClientError
from sidequest.agents.llm_factory import build_intent_router_llm
from sidequest.agents.ollama_client import OllamaClient, OllamaClientError
from sidequest.corpus.router_corpus import RouterCapture, read_captures

EXIT_PASS = 0
EXIT_CONFIG_ERROR = 2
EXIT_OLLAMA_UNREACHABLE = 4

_UNREACHABLE_NOTE = (
    "# Router A/B Evaluation — operator note\n\n"
    "Ollama was unreachable; no A/B evidence was produced. The live router\n"
    "A/B only runs on the M3 Ultra where Ollama serves the qwen models.\n"
    "Start Ollama (and confirm the model is resident — num_ctx is set\n"
    "load-time via the Modelfile, never per-request) and re-run.\n"
)


class _DeferredRouterLlm:
    """Defer real backend construction to the first ``emit_tool`` call.

    Corpus parsing and argument validation must not require credentials or a
    running daemon — the loud environment check (missing ANTHROPIC_API_KEY,
    unreachable Ollama) still fires before any eval work, on the first call.
    This is deferred fail-loud, not a fallback: a construction failure
    propagates immediately.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._inner: Any = None

    async def emit_tool(self, **kwargs: Any) -> dict[str, Any]:
        if self._inner is None:
            self._inner = self._factory()
        return await self._inner.emit_tool(**kwargs)


def _build_qwen_llm(model: str) -> QwenRouterLlm:
    # The model hint resolves through OllamaClient's model_map; map the
    # requested tag to itself so operator-chosen tags work without editing
    # DEFAULT_MODEL_MAP.
    client = OllamaClient(model_map={model: model})
    return QwenRouterLlm(client, model=model)


def _write_or_print(output_md: str | None, text: str) -> None:
    if output_md:
        out = Path(output_md)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Router A/B eval: Haiku vs local qwen")
    parser.add_argument("--corpus-jsonl", required=True, help="RouterCapture JSONL corpus")
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--output-md", default=None, help="Markdown report path (default: stdout)")
    parser.add_argument("--qwen-model", default=OLLAMA_MODEL)
    parser.add_argument("--agreement-threshold-pct", type=float, default=95.0)
    parser.add_argument("--p95-budget-ms", type=float, default=5000.0)
    parser.add_argument("--schema-validity-floor-pct", type=float, default=95.0)
    args = parser.parse_args(argv)

    if args.sample_size is not None and args.sample_size <= 0:
        print(f"config error: --sample-size must be positive, got {args.sample_size}")
        return EXIT_CONFIG_ERROR

    corpus_path = Path(args.corpus_jsonl)
    if not corpus_path.is_file():
        print(f"config error: corpus file not found: {corpus_path}")
        return EXIT_CONFIG_ERROR
    try:
        captures: list[RouterCapture] = list(read_captures(corpus_path))
    except ValueError as exc:
        print(f"config error: invalid corpus: {exc}")
        return EXIT_CONFIG_ERROR
    if not captures:
        print(f"config error: corpus is empty: {corpus_path}")
        return EXIT_CONFIG_ERROR

    harness = RouterAbEvalHarness(
        haiku_llm=_DeferredRouterLlm(build_intent_router_llm),
        qwen_llm=_DeferredRouterLlm(lambda: _build_qwen_llm(args.qwen_model)),
    )
    try:
        report = asyncio.run(harness.eval_corpus(captures, sample_size=args.sample_size))
    except OllamaClientError as exc:
        print(f"ollama unreachable — no A/B evidence produced: {exc}")
        _write_or_print(args.output_md, _UNREACHABLE_NOTE + f"\nError: {exc}\n")
        return EXIT_OLLAMA_UNREACHABLE
    except LlmClientError as exc:
        print(f"config error: backend construction failed: {exc}")
        return EXIT_CONFIG_ERROR

    md = report.to_markdown()
    # Append the gate verdict when the report supports it (the harness may be
    # substituted in tests with a report exposing only to_markdown).
    go_no_go = getattr(report, "go_no_go", None)
    if callable(go_no_go):
        verdict = go_no_go(
            agreement_threshold_pct=args.agreement_threshold_pct,
            p95_budget_ms=args.p95_budget_ms,
            schema_validity_floor_pct=args.schema_validity_floor_pct,
        )
        md += "\n## Go/No-Go (pre-adjudication)\n\n"
        md += f"**Verdict:** {'GO' if verdict.go else 'NO-GO'}\n"
        for reason in verdict.reasons:
            md += f"- {reason}\n"
        md += (
            "\nDisagreements require manual adjudication (AC4) before the "
            "final 92-2 gate decision — see RouterAdjudication.\n"
        )

    _write_or_print(args.output_md, md)
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
