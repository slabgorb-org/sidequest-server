"""Operator CLI for the story 92-1 router A/B eval — Haiku vs local qwen.

Two modes:

**Eval (default).** Runs the ``RouterAbEvalHarness`` over a captured router
corpus (``RouterCapture`` JSONL, see ``sidequest/corpus/router_corpus.py``)
and writes the markdown gate report 92-2 consumes. Live runs only work on the
M3 Ultra (the only host where Ollama serves qwen) — anywhere else the
distinct ``EXIT_OLLAMA_UNREACHABLE`` code plus an operator note is the
documented no-op, mirroring the 48-4 ``ab_eval_harness_cli.py`` contract.

**Capture (``--capture``).** Produces the corpus: reads prompt rows
(JSONL of ``{"action", "state_summary" (str|object), "genre", "world",
"round_number", "source_save", "event_seq" (optional)}``), drives the REAL
``RouterCorpusCapturer`` (production ``IntentRouter.decompose``, live Haiku)
for each row, and atomically writes the ``RouterCapture`` corpus to ``--out``.
A backend failure aborts the run loudly with NO partial corpus file.

Nondeterminism characterization (AC2): backends are sampled models, so the
operator procedure is to run the eval TWICE over the same frozen corpus and
diff the two reports — the agreement-pct spread across runs IS the
characterization, and it must be recorded in the go/no-go artifact alongside
the verdict.

Usage:
    # capture
    uv run python scripts/router_ab_eval_cli.py --capture \
        --prompts-jsonl artifacts/router_prompts.jsonl \
        --out artifacts/router_corpus.jsonl

    # eval
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
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Module-top imports (48-4 AC5 doctrine): a broken sidequest install fails AT
# SCRIPT LOAD, not at --help.
from sidequest.agents.ab_eval_harness import (
    OLLAMA_MODEL,
    QwenRouterLlm,
    RouterAbEvalHarness,
    RouterCorpusCapturer,
)
from sidequest.agents.claude_client import LlmClientError
from sidequest.agents.intent_router import IntentRouterFailure
from sidequest.agents.llm_factory import build_intent_router_llm
from sidequest.agents.ollama_client import OllamaClient, OllamaClientError
from sidequest.corpus.router_corpus import RouterCapture, read_captures, write_captures
from sidequest.corpus.schema import MineProvenance

EXIT_PASS = 0
EXIT_CONFIG_ERROR = 2
EXIT_CAPTURE_ERROR = 3
EXIT_OLLAMA_UNREACHABLE = 4

_UNREACHABLE_NOTE = (
    "# Router A/B Evaluation — operator note\n\n"
    "Ollama was unreachable; no A/B evidence was produced. The live router\n"
    "A/B only runs on the M3 Ultra where Ollama serves the qwen models.\n"
    "Start Ollama (and confirm the model is resident — num_ctx is set\n"
    "load-time via the Modelfile, never per-request) and re-run.\n"
)


class _PromptRow(BaseModel):
    """One capture-mode input row. extra='forbid': a stray key in operator
    input is a mistake to surface, not data to drop."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    state_summary: Any
    genre: str = Field(min_length=1)
    world: str = Field(min_length=1)
    round_number: int = Field(ge=0)
    source_save: str = Field(min_length=1)
    event_seq: int | None = None


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


def _read_prompt_rows(path: Path) -> list[_PromptRow]:
    """Parse capture-mode prompt rows, fail-loud per line (rule #8/#11)."""
    rows: list[_PromptRow] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(_PromptRow.model_validate(json.loads(stripped)))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid prompt row: {exc}") from exc
    return rows


def _run_capture(args: argparse.Namespace) -> int:
    if not args.prompts_jsonl or not args.out:
        print("config error: --capture requires --prompts-jsonl and --out")
        return EXIT_CONFIG_ERROR
    prompts_path = Path(args.prompts_jsonl)
    if not prompts_path.is_file():
        print(f"config error: prompts file not found: {prompts_path}")
        return EXIT_CONFIG_ERROR
    try:
        rows = _read_prompt_rows(prompts_path)
    except ValueError as exc:
        print(f"config error: invalid prompts file: {exc}")
        return EXIT_CONFIG_ERROR
    if not rows:
        print(f"config error: prompts file is empty: {prompts_path}")
        return EXIT_CONFIG_ERROR

    # Eager construction here (48-4 doctrine): capture is Haiku-only and the
    # missing-credentials check should fire before any API spend.
    try:
        capturer = RouterCorpusCapturer(llm=build_intent_router_llm())
    except LlmClientError as exc:
        print(f"config error: backend construction failed: {exc}")
        return EXIT_CONFIG_ERROR

    async def _capture_all() -> list[RouterCapture]:
        captures: list[RouterCapture] = []
        for row in rows:
            captures.append(
                await capturer.capture(
                    action=row.action,
                    state_summary=row.state_summary,
                    genre=row.genre,
                    world=row.world,
                    round_number=row.round_number,
                    provenance=MineProvenance(source_save=row.source_save, event_seq=row.event_seq),
                )
            )
        return captures

    try:
        captures = asyncio.run(_capture_all())
    except (IntentRouterFailure, LlmClientError) as exc:
        # Loud abort, NO partial corpus: write_captures below never ran, so
        # --out is untouched. Half a corpus would masquerade as coverage.
        print(f"capture failed — no corpus written: {exc}")
        return EXIT_CAPTURE_ERROR

    write_captures(Path(args.out), captures)
    print(f"captured {len(captures)} rows -> {args.out}")
    return EXIT_PASS


def _run_eval(args: argparse.Namespace) -> int:
    if not args.corpus_jsonl:
        print("config error: eval mode requires --corpus-jsonl (or pass --capture)")
        return EXIT_CONFIG_ERROR
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Router A/B eval: Haiku vs local qwen")
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Capture mode: produce a RouterCapture corpus from prompt rows",
    )
    parser.add_argument("--prompts-jsonl", default=None, help="Capture-mode prompt rows JSONL")
    parser.add_argument("--out", default=None, help="Capture-mode corpus output path")
    parser.add_argument("--corpus-jsonl", default=None, help="RouterCapture JSONL corpus (eval)")
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--output-md", default=None, help="Markdown report path (default: stdout)")
    parser.add_argument("--qwen-model", default=OLLAMA_MODEL)
    parser.add_argument("--agreement-threshold-pct", type=float, default=95.0)
    parser.add_argument("--p95-budget-ms", type=float, default=5000.0)
    parser.add_argument("--schema-validity-floor-pct", type=float, default=95.0)
    args = parser.parse_args(argv)

    if args.capture:
        return _run_capture(args)
    return _run_eval(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
