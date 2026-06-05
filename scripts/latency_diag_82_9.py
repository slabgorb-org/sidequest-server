#!/usr/bin/env python3
"""Story 82-9 AC5 — turn the captured span JSONL into the latency diagnosis.

Consumes a ``--span-jsonl`` capture from ``scripts/playtest.py`` (Jaeger-exported
``intent_router.decompose`` + ``narration.turn`` + ``narrator.tool_loop`` spans)
and produces the AC5 diagnosis: the decompose/turn p50/p95 via the wired
``build_latency_report`` consumer, the env-vs-code split (raw SDK round-trip vs
the rest of decompose; state-summary size correlation), and the iteration
correlation (tool-loop depth vs whole-turn wall-clock).

This is a REAL production caller of
``sidequest.telemetry.latency_report.build_latency_report`` — the consumer the
71-40 Reviewer wiring finding asked for.

Capture step (from the orchestrator root, server booted with OTEL->Jaeger):
    uv run --with rich python3 scripts/playtest.py \
        --scenario scenarios/latency_diag_82_9.yaml \
        --span-jsonl /tmp/82-9-latency.spans.jsonl --confirm-cost

Analysis step (from sidequest-server):
    uv run python3 scripts/latency_diag_82_9.py \
        --span-jsonl /tmp/82-9-latency.spans.jsonl \
        --out docs/82-9-latency-diagnosis.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from sidequest.telemetry.latency_report import build_latency_report

_DECOMPOSE = "intent_router.decompose"
_TURN = "narration.turn"
_TOOL_LOOP = "narrator.tool_loop"


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _by_name(records: list[dict], name: str) -> list[dict]:
    return [r for r in records if r.get("name") == name]


def _attr(rec: dict, key: str, default: float = 0.0) -> float:
    return float((rec.get("attributes") or {}).get(key, default))


def _corr(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or None when undefined.

    Returns None (never raises) when the series cannot support a correlation:
    unequal lengths (Story 82-9 review S2 — Jaeger truncation can yield more
    tool_loop spans than narration.turn spans, which would otherwise raise
    ``statistics.StatisticsError`` and abort the diagnosis with no output),
    fewer than 2 points, or zero variance in either series.
    """
    if len(xs) != len(ys) or len(xs) < 2 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return statistics.correlation(xs, ys)


def build_diagnosis(records: list[dict]) -> str:
    decompose = _by_name(records, _DECOMPOSE)
    turns = _by_name(records, _TURN)
    loops = [
        r
        for r in _by_name(records, _TOOL_LOOP)
        if (r.get("attributes") or {}).get("caller", "narrator") == "narrator"
    ]

    # Story 82-9 review S1 (No Silent Fallbacks): refuse to emit a fabricated
    # verdict from a capture that lacks the spans the diagnosis is built on. A
    # wrong/empty --span-jsonl must fail loud, not silently print "CODE 0%".
    if not decompose:
        raise ValueError(
            "no intent_router.decompose spans in the capture — wrong or empty "
            "--span-jsonl; refusing to emit a fabricated latency verdict"
        )
    if not turns:
        raise ValueError(
            "no narration.turn spans in the capture — wrong or empty "
            "--span-jsonl; refusing to emit a fabricated latency verdict"
        )

    decompose_total = [_attr(r, "latency_ms") for r in decompose]
    sdk_ms = [_attr(r, "sdk_latency_ms") for r in decompose]
    summary_bytes = [_attr(r, "state_summary_bytes") for r in decompose]
    code_ms = [t - s for t, s in zip(decompose_total, sdk_ms, strict=True)]
    turn_ms = [r.get("duration_us", 0) / 1000.0 for r in turns]
    iters = [_attr(r, "iterations_used") for r in loops]
    loop_turn_ms = [r.get("duration_us", 0) / 1000.0 for r in turns][: len(iters)]

    report = build_latency_report(
        decompose_latencies=decompose_total,
        turn_latencies=turn_ms,
    )

    # Story 82-9 review S2: distinguish "no decompose latency" (degenerate,
    # share is undefined → None) from a genuine 0% share. Defaulting a 0/0 to
    # 0.0 would silently flip the verdict to CODE on all-zero data.
    mean_decompose = statistics.mean(decompose_total)
    sdk_share = statistics.mean(sdk_ms) / mean_decompose if mean_decompose else None
    share_pct = (
        f"{sdk_share * 100:.0f}%" if sdk_share is not None else "n/a (zero decompose latency)"
    )
    bytes_corr = _corr(summary_bytes, decompose_total)
    iter_corr = _corr(iters, loop_turn_ms)

    # Which cause dominates the decompose budget: env (raw SDK round-trip) or
    # code (the rest — prompt assembly / oversized state summary). Undefined
    # share → INDETERMINATE, never a silently-defaulted CODE verdict.
    if sdk_share is None:
        cause = "INDETERMINATE (decompose latencies are all zero — degenerate capture)"
    elif sdk_share >= 0.5:
        cause = "ENVIRONMENT (raw SDK round-trip)"
    else:
        cause = "CODE (prompt assembly / state-summary size)"

    lines = [
        "# Story 82-9 — Per-Turn Latency Diagnosis (AC5)",
        "",
        "Live `tea_and_murder/glenross` capture (`scenarios/latency_diag_82_9.yaml`),",
        "spans pulled from Jaeger via `scripts/playtest.py --span-jsonl`, percentiles",
        "computed through the wired `build_latency_report` consumer.",
        "",
        "## Sample",
        "",
        f"- `intent_router.decompose` spans: **{len(decompose)}**",
        f"- `narration.turn` spans: **{len(turns)}**",
        f"- `narrator.tool_loop` spans (caller=narrator): **{len(loops)}**",
        "",
        "## Percentiles (build_latency_report)",
        "",
        "```",
        report.render(),
        "```",
        "",
        "## Env-vs-code split (router decompose)",
        "",
        f"- mean total decompose: **{statistics.mean(decompose_total):.1f}ms**"
        if decompose_total
        else "- mean total decompose: n/a",
        f"- mean raw SDK round-trip (`sdk_latency_ms`, env): **{statistics.mean(sdk_ms):.1f}ms**"
        if sdk_ms
        else "- mean sdk: n/a",
        f"- mean remainder (`latency_ms - sdk_latency_ms`, code/overhead): **{statistics.mean(code_ms):.1f}ms**"
        if code_ms
        else "- mean remainder: n/a",
        f"- raw-SDK share of decompose: **{share_pct}**",
        f"- mean `state_summary_bytes`: **{statistics.mean(summary_bytes):.0f}**"
        if summary_bytes
        else "- mean state_summary_bytes: n/a",
        f"- corr(state_summary_bytes, total decompose): **{bytes_corr:.2f}**"
        if bytes_corr is not None
        else "- corr(state_summary_bytes, total decompose): undefined (n<2 / no variance)",
        "",
        "## Iteration correlation (tool loop vs whole-turn wall-clock)",
        "",
        f"- mean tool-loop iterations/turn: **{statistics.mean(iters):.2f}**"
        if iters
        else "- mean iterations: n/a",
        f"- max iterations: **{int(max(iters))}**" if iters else "- max iterations: n/a",
        f"- corr(iterations_used, narration.turn ms): **{iter_corr:.2f}**"
        if iter_corr is not None
        else "- corr(iterations_used, narration.turn ms): undefined (n<2 / no variance)",
        f"- loop-exceeded turns: **{sum(1 for r in loops if (r.get('attributes') or {}).get('loop_exceeded'))}**",
        "",
        "## Verdict",
        "",
        f"**Dominant decompose cost: {cause}** "
        f"(raw SDK round-trip is {share_pct} of the decompose budget).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--span-jsonl", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    records = _load(args.span_jsonl)
    try:
        diagnosis = build_diagnosis(records)
    except ValueError as exc:
        # Fail loud with a clean operator message (no fabricated artifact written).
        raise SystemExit(f"ERROR: {exc}") from exc
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(diagnosis)
    print(diagnosis)
    print(f"\n[written] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
