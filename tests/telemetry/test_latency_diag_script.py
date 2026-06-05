"""Story 82-9 (review rework) — guards on the AC5 diagnosis script.

The Reviewer flagged three defects in ``scripts/latency_diag_82_9.py``:
* S1 — a capture missing the spans the diagnosis is built on produced a silent,
  fabricated "CODE 0%" verdict (No Silent Fallbacks violation);
* S2 — ``_corr`` crashed with ``StatisticsError`` when the loop-span and
  turn-span counts diverged (plausible under Jaeger truncation);
* S2b — an all-zero decompose series silently inverted the verdict to CODE.

These tests pin the fail-loud + crash-free behavior. The script is not a package;
it is loaded by path with the established importlib pattern
(mirrors ``tests/agents/test_ab_eval_harness.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "latency_diag_82_9.py"


def _load_script() -> Any:
    """Load scripts/latency_diag_82_9.py by path (it is not a package).

    Fails loudly if the script is absent — that is a real signal, not a skip.
    """
    spec = importlib.util.spec_from_file_location("latency_diag_82_9", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["latency_diag_82_9"] = mod
    spec.loader.exec_module(mod)
    return mod


def _span(name: str, *, duration_us: int = 0, **attrs: Any) -> dict:
    return {"name": name, "duration_us": duration_us, "attributes": dict(attrs)}


def _decompose(latency_ms: float, sdk_latency_ms: float, state_summary_bytes: int) -> dict:
    return _span(
        "intent_router.decompose",
        latency_ms=latency_ms,
        sdk_latency_ms=sdk_latency_ms,
        state_summary_bytes=state_summary_bytes,
    )


def _turn(duration_us: int) -> dict:
    return _span("narration.turn", duration_us=duration_us)


def _loop(iterations_used: int, *, caller: str = "narrator", loop_exceeded: bool = False) -> dict:
    return _span(
        "narrator.tool_loop",
        iterations_used=iterations_used,
        caller=caller,
        loop_exceeded=loop_exceeded,
    )


# --- S1: fail loud on a capture missing the required span types --------------


def test_build_diagnosis_raises_when_no_decompose_spans() -> None:
    """A capture with no ``intent_router.decompose`` spans must raise — never
    silently emit a fabricated 'CODE 0%' verdict (No Silent Fallbacks)."""
    mod = _load_script()
    records = [_turn(2_000_000), _loop(2)]  # turns present, decompose absent
    with pytest.raises(ValueError, match="intent_router.decompose"):
        mod.build_diagnosis(records)


def test_build_diagnosis_raises_when_no_turn_spans() -> None:
    """A capture with no ``narration.turn`` spans must raise — the turn-latency
    half of the diagnosis cannot be fabricated."""
    mod = _load_script()
    records = [_decompose(1000.0, 950.0, 4000), _loop(2)]  # decompose present, turns absent
    with pytest.raises(ValueError, match="narration.turn"):
        mod.build_diagnosis(records)


def test_build_diagnosis_empty_capture_raises_not_silent() -> None:
    """An entirely empty capture raises rather than writing a 0%/CODE artifact."""
    mod = _load_script()
    with pytest.raises(ValueError):
        mod.build_diagnosis([])


# --- S2: _corr does not crash on diverging loop/turn counts ------------------


def test_corr_returns_none_on_length_mismatch_no_crash() -> None:
    """``_corr`` must return None (not raise StatisticsError) when the two series
    differ in length — the exact Jaeger-truncation case (more loops than turns)
    that crashed the script before."""
    mod = _load_script()
    assert mod._corr([1.0, 2.0, 3.0], [10.0, 20.0]) is None


def test_build_diagnosis_survives_more_loops_than_turns() -> None:
    """End-to-end: a capture where loop spans outnumber turn spans (truncation)
    must still produce a report, with the iteration correlation reported as
    undefined rather than crashing the whole run."""
    mod = _load_script()
    records = [
        _decompose(1000.0, 950.0, 4000),
        _decompose(1200.0, 1100.0, 5000),
        _turn(2_000_000),
        _turn(2_500_000),
        # Three narrator loops but only two turns — counts diverge.
        _loop(2),
        _loop(3),
        _loop(2),
    ]
    text = mod.build_diagnosis(records)
    assert isinstance(text, str) and "Verdict" in text
    assert "corr(iterations_used, narration.turn ms): undefined" in text


# --- S2b: all-zero decompose -> INDETERMINATE, never a silent CODE verdict ----


def test_all_zero_decompose_is_indeterminate_not_code() -> None:
    """When every decompose latency is 0 (degenerate capture), the share is
    undefined — the verdict must say INDETERMINATE, NOT silently default to
    'CODE' (which a 0/0 -> 0.0 fallback would have produced)."""
    mod = _load_script()
    records = [
        _decompose(0.0, 0.0, 4000),
        _decompose(0.0, 0.0, 5000),
        _turn(2_000_000),
        _turn(2_500_000),
        _loop(1),
        _loop(1),
    ]
    text = mod.build_diagnosis(records)
    assert "INDETERMINATE" in text
    assert "Dominant decompose cost: CODE" not in text
    assert "n/a (zero decompose latency)" in text


# --- happy path: a well-formed capture produces a real verdict ---------------


def test_build_diagnosis_happy_path_env_dominates() -> None:
    """A normal capture where the SDK round-trip is ~100% of decompose yields an
    ENVIRONMENT verdict and renders the percentile block."""
    mod = _load_script()
    records = [
        _decompose(4000.0, 3990.0, 48000),
        _decompose(3500.0, 3490.0, 47000),
        _turn(23_000_000),
        _turn(25_000_000),
        _loop(3),
        _loop(2),
    ]
    text = mod.build_diagnosis(records)
    assert "Dominant decompose cost: ENVIRONMENT" in text
    assert "Per-turn latency report" in text  # the build_latency_report render block
