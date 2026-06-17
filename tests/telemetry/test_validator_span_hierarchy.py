"""RED (story 124-2): turn_complete spans must carry dependency hierarchy.

AC1 — the ``spans`` array on the ``turn_complete`` event carries enough
hierarchy (a ``depth`` and a ``leaf`` flag per span) to reconstruct the
caller▸callee tree the Tufte flame chart renders, while preserving the flat
fields (``name`` / ``component`` / ``start_ms`` / ``duration_ms``) older
dashboards read.

These tests drive the real emission path (``Validator._validate`` →
``publish_event``) — the same wiring boundary ``test_validator_phase_timing.py``
guards — so they prove the hierarchy fields actually cross into the event the
GM panel consumes, not merely that a helper can compute them in isolation
(CLAUDE.md: "Every Test Suite Needs a Wiring Test").

Scope note (see the 124-2 Delivery Findings): the *only* per-turn span source
that exists today is the flat ``PhaseTimings`` map (``dict[str, int]``), which
has no parent/child. So these tests pin the *output contract* (every span has
typed ``depth`` + ``leaf``, the set is a well-formed tree) without asserting a
specific multi-level shape — forcing a fabricated deep tree would violate the
epic-124 "no fabricated data" guardrail. Honest multi-level rendering is
covered UI-side, where synthetic depth/leaf spans are legitimate test input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sidequest.telemetry.turn_record import TurnRecord
from sidequest.telemetry.validator import Validator


def _make_record(**overrides: Any) -> TurnRecord:
    base: dict[str, Any] = dict(
        turn_id=1,
        timestamp=datetime.now(UTC),
        player_id="p1",
        player_input="hello",
        classified_intent="speak",
        agent_name="narrator",
        narration="The wind picks up.",
        patches_applied=[],
        snapshot_before_hash="x",
        snapshot_after=object(),
        delta=None,
        beats_fired=[],
        extraction_tier=1,
        token_count_in=10,
        token_count_out=20,
        agent_duration_ms=14336,
        is_degraded=False,
        # Three flat pipeline phases — the only per-turn span source that
        # exists today. The emitted spans must still carry hierarchy fields.
        phase_durations_ms={
            "preprocess_llm": 87000,
            "narrator_subprocess": 14336,
            "broadcast": 120,
        },
        phase_call_counts={
            "preprocess_llm": 1,
            "narrator_subprocess": 1,
            "broadcast": 1,
        },
        total_duration_ms=101456,
    )
    base.update(overrides)
    return TurnRecord(**base)


async def _emit_turn_complete(record: TurnRecord, monkeypatch) -> dict[str, Any]:
    """Drive the real validator emission path and return the turn_complete payload."""
    captured: list[dict[str, Any]] = []

    def fake_publish(
        event_type: str,
        payload: dict[str, Any],
        *,
        component: str = "sidequest-server",  # noqa: ARG001
        severity: str = "info",  # noqa: ARG001
    ) -> None:
        if event_type == "turn_complete":
            captured.append(payload)

    monkeypatch.setattr(
        "sidequest.telemetry.validator.publish_event",
        fake_publish,
    )

    v = Validator()
    # Pin to the emission codepath only; real checks would re-publish noise.
    v._checks = []  # noqa: SLF001 — intentional test seam (mirrors sibling test)
    await v._validate(record)  # noqa: SLF001 — testing internal emission

    assert len(captured) == 1, "expected exactly one turn_complete emission"
    return captured[0]


@pytest.mark.asyncio
async def test_turn_complete_spans_carry_depth_and_leaf(monkeypatch) -> None:
    """AC1: every span carries a typed ``depth`` (int >= 0) and ``leaf`` (bool)."""
    payload = await _emit_turn_complete(_make_record(), monkeypatch)

    spans = payload["spans"]
    assert spans, "turn_complete must carry at least one span"
    for i, span in enumerate(spans):
        depth = span.get("depth")
        leaf = span.get("leaf")
        assert isinstance(depth, int) and not isinstance(depth, bool), (
            f"span[{i}]={span.get('name')!r} missing int depth; got {depth!r}"
        )
        assert depth >= 0, f"span[{i}] depth must be >= 0, got {depth}"
        assert isinstance(leaf, bool), (
            f"span[{i}]={span.get('name')!r} missing bool leaf; got {leaf!r}"
        )


@pytest.mark.asyncio
async def test_turn_complete_spans_retain_flat_fields(monkeypatch) -> None:
    """Backward-compat guard: hierarchy is ADDED, the flat fields older
    dashboards read are untouched (name / component / start_ms / duration_ms).
    """
    payload = await _emit_turn_complete(_make_record(), monkeypatch)

    spans = payload["spans"]
    assert spans
    for span in spans:
        assert isinstance(span.get("name"), str) and span["name"]
        assert isinstance(span.get("component"), str) and span["component"]
        assert isinstance(span.get("start_ms"), int)
        assert isinstance(span.get("duration_ms"), int)


@pytest.mark.asyncio
async def test_turn_complete_spans_have_a_top_level_root(monkeypatch) -> None:
    """AC1: the tree is rooted — at least one span sits at the top level
    (depth 0). Without a root the UI can't anchor the nesting.
    """
    payload = await _emit_turn_complete(_make_record(), monkeypatch)

    spans = payload["spans"]
    depths = [s.get("depth") for s in spans]
    assert all(isinstance(d, int) and not isinstance(d, bool) for d in depths), (
        f"every span needs an int depth before a root can exist; got {depths}"
    )
    assert min(depths) == 0, f"no top-level (depth 0) span present; depths={depths}"


@pytest.mark.asyncio
async def test_turn_complete_span_tree_is_wellformed(monkeypatch) -> None:
    """AC1 ("sufficient to reconstruct the caller▸callee tree"): the depth set
    is contiguous from 0 (no depth-2 span without a depth-1 ancestor level),
    and any container (leaf=False) implies a deeper level exists for it to
    contain. Order-independent, so it does not over-constrain the emit order
    or the eventual hierarchy source.
    """
    payload = await _emit_turn_complete(_make_record(), monkeypatch)

    spans = payload["spans"]
    depths = [s.get("depth") for s in spans]
    assert all(isinstance(d, int) and not isinstance(d, bool) for d in depths), (
        f"every span needs an int depth; got {depths}"
    )

    distinct = set(depths)
    assert distinct == set(range(max(distinct) + 1)), (
        f"depths must be contiguous from 0 (a reconstructable tree), got {sorted(distinct)}"
    )

    max_depth = max(distinct)
    for span in spans:
        if span.get("leaf") is False:
            assert span["depth"] < max_depth, (
                f"container span {span.get('name')!r} at depth {span['depth']} "
                f"marked non-leaf but nothing is deeper (max depth {max_depth})"
            )
