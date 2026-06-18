"""RED tests for the post-narration Haiku sidecar extractor (Story 151-2).

ADR-150 step 2 — the **foundation** of the sidecar-accounting epic. A new
post-narration Haiku ``emit_tool`` pass (AsideResolver/IntentRouter-shaped)
reads the narrator's emitted prose and produces the eleven *bucket-B*
sidecar fields, running in **shadow mode** first: it computes fields and
emits OTEL, but applies nothing, so the lie-detector watches from day one
before any field cuts over (151-4 / 151-5).

Module under test (created by Dev in GREEN):
- ``sidequest/agents/sidecar_extractor.py``

Two layers, mirroring the live ADR-113 lineage exactly:
1. **Core pass** — ``SidecarExtractor.extract(...)`` (the analogue of
   ``IntentRouter.decompose``): a single-shot forced-``emit_tool`` Haiku call
   (ADR-102, no JSON parsing), one bounded retry, and on persistent failure
   an explicit ``SidecarExtractionFailure`` raise (No Silent Fallbacks). It
   emits the ``sidecar_extraction.run`` and per-field
   ``sidecar_extraction.{field}`` spans.
2. **Shadow runner** — ``run_sidecar_extraction_watcher(...)`` (the analogue
   of ``run_dispatch_engagement_watcher``): a *non-fatal* post-narration
   observability wrapper that runs the core pass, emits the
   ``sidecar_extraction.mismatch`` lie-detector span, and NEVER raises into
   the WS turn pipeline (the per-field catch-loops remain the loud net).

RED-phase interface pins (resolved by TEA; see the TEA Assessment in
``.session/151-2-session.md`` for rationale + deviations). The story context
fixed the span names, the eleven bucket-B field names, the ``emit_tool``
shape, the shadow/no-mutation constraint, the retry-once discipline, and the
wiring requirement; TEA pinned the module path, the public symbol names, and
the seed *mismatch witness* (extractor-invented ``npcs_present`` name absent
from the engine-owned cast — membership is engine-owned per ADR-150).

Project-rule coverage (CLAUDE.md / SOUL / python.md):
- "Every Test Suite Needs a Wiring Test" + "No Source-Text Wiring Tests" —
  ``test_runner_wired_into_session_handler`` uses reflection on the handler
  module namespace (``__dict__``), never a source-text grep, plus a
  behavioral span assertion that the runner is reachable and emits.
- "No Silent Fallbacks" / python.md #1 — induced failure raises an explicit
  error from the core pass and surfaces a loud span from the runner; it is
  never a silent narrator-only continuation.
- "OTEL Observability Principle" / python.md #4 — every pass emits a span;
  a divergence emits a mismatch span; a quiet/consistent turn emits none.
- python.md #9 (async) — ``extract`` / the runner are coroutines.
- python.md #10 (import hygiene) — the module declares ``__all__``.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot

# ---------------------------------------------------------------------------
# Canonical contract data — the eleven bucket-B fields (ADR-150 §Decision,
# verified against NarrationTurnResult field names at orchestrator.py:474-587).
# `scene_mood` is the real field the ADR refers to as "mood".
# ---------------------------------------------------------------------------

BUCKET_B_FIELD_NAMES = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
    "gold_change",
    "companions_added",
    "companions_dismissed",
    "npcs_present",
    "scene_mood",
    "visual_scene",
    "footnotes",
)


def _full_emit() -> dict[str, Any]:
    """An emit_tool payload exercising a representative spread of fields:
    one item gained, a gold delta, one present NPC, a mood; the rest empty."""
    return {
        "items_gained": [{"name": "silver ring"}],
        "items_lost": [],
        "items_discarded": [],
        "items_consumed": [],
        "gold_change": 5,
        "companions_added": [],
        "companions_dismissed": [],
        "npcs_present": [{"name": "Harlan"}],
        "scene_mood": "tense",
        "visual_scene": None,
        "footnotes": [],
    }


def _empty_emit() -> dict[str, Any]:
    """A quiet-turn payload: every bucket-B field empty/absent."""
    return {
        "items_gained": [],
        "items_lost": [],
        "items_discarded": [],
        "items_consumed": [],
        "gold_change": None,
        "companions_added": [],
        "companions_dismissed": [],
        "npcs_present": [],
        "scene_mood": None,
        "visual_scene": None,
        "footnotes": [],
    }


def _make_mock_llm(response: dict | BaseException) -> AsyncMock:
    """An extractor LLM stub: ``emit_tool(...) -> dict`` per ADR-102.

    If ``response`` is an exception it is raised; otherwise returned. Mirrors
    ``tests/agents/test_intent_router.py::_make_mock_router_llm``.
    """
    mock = AsyncMock()
    if isinstance(response, BaseException):
        mock.emit_tool = AsyncMock(side_effect=response)
    else:
        mock.emit_tool = AsyncMock(return_value=response)
    return mock


def _make_sequenced_llm(*responses: dict | BaseException) -> AsyncMock:
    """An extractor LLM stub returning/raising a sequence across calls."""
    mock = AsyncMock()
    mock.emit_tool = AsyncMock(side_effect=list(responses))
    return mock


def _snapshot(
    *,
    npc_pool: list[NpcPoolMember] | None = None,
) -> GameSnapshot:
    """Minimal post-turn snapshot. The engine-owned cast lives in
    ``npc_pool`` (identity channel) — the seed mismatch witness compares the
    extractor's ``npcs_present`` names against it."""
    return GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        npc_pool=npc_pool or [],
    )


def _known_npc(name: str) -> NpcPoolMember:
    return NpcPoolMember(
        name=name,
        role="innkeeper",
        pronouns="they/them",
        appearance="weathered",
        drawn_from="world_authored",
    )


def _extraction_spans(exporter: Any) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("sidecar_extraction")]


# ===========================================================================
# AC1 — Structured output via forced emit_tool, no JSON parsing
# ===========================================================================


async def test_extract_returns_validated_object_with_bucket_b_fields() -> None:
    """``extract`` returns a structured object carrying the bucket-B fields,
    sourced from the ``emit_tool`` dict (ADR-102 — no fenced JSON to parse)."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    llm = _make_mock_llm(_full_emit())
    extractor = SidecarExtractor(llm=llm)

    result = await extractor.extract(
        narration="You pocket a silver ring. Harlan watches, tense.",
        snapshot=_snapshot(),
    )

    # Scalars round-trip exactly (representation-stable assertions).
    assert result.gold_change == 5
    assert result.scene_mood == "tense"
    assert len(result.items_gained) == 1
    assert len(result.npcs_present) == 1
    assert llm.emit_tool.await_count == 1


async def test_extract_forces_emit_tool_with_a_schema() -> None:
    """ADR-102 wiring: the call is a forced single tool-use whose input_schema
    is supplied — structured input comes back, no free-text JSON path."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    llm = _make_mock_llm(_full_emit())
    await SidecarExtractor(llm=llm).extract(narration="quiet", snapshot=_snapshot())

    kwargs = llm.emit_tool.await_args.kwargs
    assert kwargs.get("tool_name"), "extractor must name the emit tool (forced tool-use)"
    assert isinstance(kwargs.get("tool_schema"), dict) and kwargs["tool_schema"], (
        "extractor must pass a non-empty tool_schema so Haiku returns structured "
        "input (ADR-102), not fenced JSON the extractor would have to parse"
    )


async def test_extract_passes_narration_prose_into_the_user_prompt() -> None:
    """The extractor is a *reader of prose* — the narration must reach the
    model. Guards against a skeleton that calls Haiku with empty context."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    llm = _make_mock_llm(_empty_emit())
    prose = "A copper lantern swings overhead as the door groans shut."
    await SidecarExtractor(llm=llm).extract(narration=prose, snapshot=_snapshot())

    kwargs = llm.emit_tool.await_args.kwargs
    assert prose in kwargs.get("user", ""), "narration prose must be in the extractor's user prompt"



async def test_extract_truncates_overlong_narration_before_the_sdk_call() -> None:
    """Reviewer RT1 [HIGH] / python.md #11 (CWE-400): the per-turn live call must
    bound the player-influenced prose. A narration past `_MAX_NARRATION_CHARS` is
    truncated before reaching the prompt — the tail must NOT be transmitted — yet
    the call still fires on the head (truncate, never reject)."""
    from sidequest.agents.sidecar_extractor import _MAX_NARRATION_CHARS, SidecarExtractor

    llm = _make_mock_llm(_empty_emit())
    overlong = "A" * (_MAX_NARRATION_CHARS + 50) + "UNIQUE_TAIL_MARKER"
    await SidecarExtractor(llm=llm).extract(narration=overlong, snapshot=_snapshot())

    user = llm.emit_tool.await_args.kwargs.get("user", "")
    assert "UNIQUE_TAIL_MARKER" not in user, (
        "narration past the cap must be truncated off the prompt"
    )
    # Extract just the narration portion (strip preamble and footer).
    narration_start = user.find("\n") + 1
    narration_end = user.rfind("\n\nEmit")
    narration_portion = user[narration_start:narration_end]
    assert narration_portion.count("A") <= _MAX_NARRATION_CHARS, (
        "transmitted narration must not exceed the cap"
    )
    assert llm.emit_tool.await_count == 1, (
        "truncation must not reject the turn — the head still runs"
    )


def test_bucket_b_field_set_is_the_eleven_canonical_fields() -> None:
    """The module exposes the canonical bucket-B field set so the 151-4/151-5
    cutover stories reference ONE source of truth, not drifting string lists."""
    from sidequest.agents import sidecar_extractor as mod

    assert hasattr(mod, "BUCKET_B_FIELDS"), "module must publish a BUCKET_B_FIELDS constant"
    assert set(mod.BUCKET_B_FIELDS) == set(BUCKET_B_FIELD_NAMES), (
        "BUCKET_B_FIELDS must be exactly the eleven prose-readout fields from "
        "NarrationTurnResult (ADR-150 §Decision)"
    )


# ===========================================================================
# AC2 — Shadow mode: computes fields, mutates no state
# ===========================================================================


async def test_extract_does_not_mutate_the_snapshot() -> None:
    """Shadow mode: the pass is a pure read. The post-turn snapshot handed in
    must be byte-for-byte unchanged after extraction."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    snap = _snapshot(npc_pool=[_known_npc("Harlan")])
    before = snap.model_dump()

    await SidecarExtractor(llm=_make_mock_llm(_full_emit())).extract(
        narration="You pocket a silver ring.", snapshot=snap
    )

    assert snap.model_dump() == before, "shadow-mode extraction must not mutate the snapshot"


async def test_run_watcher_returns_extraction_without_applying_it(otel_capture) -> None:
    """The shadow runner computes the extraction (and emits OTEL) but applies
    nothing — the snapshot is unchanged this story (cutover is 151-4/151-5)."""
    from sidequest.agents.sidecar_extractor import run_sidecar_extraction_watcher

    snap = _snapshot(npc_pool=[_known_npc("Harlan")])
    before = snap.model_dump()

    await run_sidecar_extraction_watcher(
        narration="You pocket a silver ring. Harlan watches.",
        snapshot=snap,
        llm=_make_mock_llm(_full_emit()),
    )

    assert snap.model_dump() == before, "shadow runner must not apply fields to the snapshot"
    assert _extraction_spans(otel_capture), "shadow runner must still emit sidecar_extraction spans"


# ===========================================================================
# AC3 — Spans fire (sidecar_extraction.run + per-field)
# ===========================================================================


async def test_extract_emits_run_span_with_attributes(otel_capture) -> None:
    """``sidecar_extraction.run`` fires once with the documented attributes:
    model, input prose length, field count, latency."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    await SidecarExtractor(llm=_make_mock_llm(_full_emit())).extract(
        narration="You pocket a silver ring.", snapshot=_snapshot()
    )

    runs = [s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.run"]
    assert len(runs) == 1, "exactly one sidecar_extraction.run span per pass"
    attrs = dict(runs[0].attributes or {})
    assert attrs.get("model"), "run span must record the resolved model id"
    for key in ("prose_length", "field_count"):
        assert key in attrs, f"run span must carry the {key!r} attribute"


async def test_extract_emits_per_field_span_emitted_true_for_present_field(otel_capture) -> None:
    """A populated bucket-B field emits its ``sidecar_extraction.{field}`` span
    marked emitted=True (per-field name mirrors dispatch_engagement.{subsystem})."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    await SidecarExtractor(llm=_make_mock_llm(_full_emit())).extract(
        narration="You pocket a silver ring.", snapshot=_snapshot()
    )

    gold = [
        s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.gold_change"
    ]
    assert len(gold) == 1, "the populated gold_change field must emit its per-field span"
    assert dict(gold[0].attributes or {}).get("emitted") is True


async def test_extract_emits_per_field_span_emitted_false_for_empty_field(otel_capture) -> None:
    """An empty bucket-B field still emits its per-field span marked
    emitted=False — the panel sees 'extractor looked, found nothing', which is
    distinct from 'extractor never ran' (No Silent Fallbacks)."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    await SidecarExtractor(llm=_make_mock_llm(_empty_emit())).extract(
        narration="The road is quiet.", snapshot=_snapshot()
    )

    mood = [
        s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.scene_mood"
    ]
    assert len(mood) == 1, "the empty scene_mood field must still emit its per-field span"
    assert dict(mood[0].attributes or {}).get("emitted") is False


# ===========================================================================
# AC4 — Lie-detector: sidecar_extraction.mismatch
# ===========================================================================


async def test_mismatch_span_fires_when_extractor_invents_an_npc(otel_capture) -> None:
    """Seed witness: membership is engine-owned (ADR-150). When the extractor
    reports an ``npcs_present`` name the engine never seated (absent from the
    snapshot cast), the two readers of the prose disagree → mismatch span."""
    from sidequest.agents.sidecar_extractor import run_sidecar_extraction_watcher

    emit = _empty_emit()
    emit["npcs_present"] = [{"name": "Mordecai the Unseated"}]

    await run_sidecar_extraction_watcher(
        narration="Mordecai steps from the shadows.",
        snapshot=_snapshot(npc_pool=[_known_npc("Harlan")]),  # no Mordecai
        llm=_make_mock_llm(emit),
    )

    mismatches = [
        s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.mismatch"
    ]
    assert len(mismatches) == 1, "an extractor-invented NPC must raise one mismatch span"
    assert "npcs_present" in str(dict(mismatches[0].attributes or {})), (
        "the mismatch span must name the diverging field so the GM panel shows which"
    )


async def test_no_mismatch_span_when_extraction_agrees_with_state(otel_capture) -> None:
    """The converse: an ``npcs_present`` name the engine DID seat is consistent
    — no mismatch span on an honest turn (no false-positive lie-detection)."""
    from sidequest.agents.sidecar_extractor import run_sidecar_extraction_watcher

    emit = _empty_emit()
    emit["npcs_present"] = [{"name": "Harlan"}]

    await run_sidecar_extraction_watcher(
        narration="Harlan refills your cup.",
        snapshot=_snapshot(npc_pool=[_known_npc("Harlan")]),
        llm=_make_mock_llm(emit),
    )

    mismatches = [
        s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.mismatch"
    ]
    assert mismatches == [], "a consistent extraction must not emit a mismatch span"


# ===========================================================================
# AC5 — No silent fallback: ERROR span + one bounded retry + explicit error
# ===========================================================================


async def test_extract_retries_once_then_raises_on_persistent_failure(otel_capture) -> None:
    """Both attempts time out → exactly two emit_tool calls, an ERROR span, and
    an explicit ``SidecarExtractionFailure`` raise (never a silent empty return)."""
    from sidequest.agents.sidecar_extractor import (
        SidecarExtractionFailure,
        SidecarExtractor,
    )

    llm = _make_mock_llm(TimeoutError("haiku timed out"))
    extractor = SidecarExtractor(llm=llm)

    with pytest.raises(SidecarExtractionFailure):
        await extractor.extract(narration="anything", snapshot=_snapshot())

    assert llm.emit_tool.await_count == 2, "exactly one bounded retry (2 total attempts)"
    failed = [s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.failed"]
    assert failed, "a persistent failure must emit at least one sidecar_extraction.failed span"


async def test_extract_recovers_on_retry() -> None:
    """A transient first failure followed by a good second attempt returns the
    extraction — the retry is a real self-heal, not decoration."""
    from sidequest.agents.sidecar_extractor import SidecarExtractor

    llm = _make_sequenced_llm(TimeoutError("transient"), _full_emit())
    result = await SidecarExtractor(llm=llm).extract(narration="ok", snapshot=_snapshot())

    assert result.gold_change == 5
    assert llm.emit_tool.await_count == 2


async def test_extract_retries_on_schema_invalid_output_then_raises() -> None:
    """A malformed emit_tool payload (fails structured validation) is a failure
    mode too: retry once, then raise — never coerce garbage into the sidecar."""
    from sidequest.agents.sidecar_extractor import (
        SidecarExtractionFailure,
        SidecarExtractor,
    )

    # gold_change must be an int|None; a dict is schema-invalid both times.
    bad = _empty_emit()
    bad["gold_change"] = {"not": "an int"}
    llm = _make_sequenced_llm(bad, bad)

    with pytest.raises(SidecarExtractionFailure):
        await SidecarExtractor(llm=llm).extract(narration="x", snapshot=_snapshot())

    assert llm.emit_tool.await_count == 2


async def test_run_watcher_surfaces_failure_as_span_without_crashing(otel_capture) -> None:
    """The shadow runner is non-fatal post-narration (mirrors
    run_dispatch_engagement_watcher): a persistently-failing extractor surfaces
    a LOUD span and the runner does NOT raise — a crash here would tear down WS
    turn delivery after the prose already broadcast. The failure is visible
    (span present), never a silent narrator-only continuation."""
    from sidequest.agents.sidecar_extractor import run_sidecar_extraction_watcher

    # MUST NOT raise — the turn pipeline continues; catch-loops are the net.
    await run_sidecar_extraction_watcher(
        narration="anything",
        snapshot=_snapshot(),
        llm=_make_mock_llm(TimeoutError("down")),
    )

    failed = [s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.failed"]
    assert failed, "runner must surface a loud failure span instead of silently swallowing"


# ===========================================================================

async def test_run_watcher_emits_crashed_span_on_unexpected_error(otel_capture, monkeypatch) -> None:
    """Reviewer RT1 [MEDIUM] / OTEL Observability: when the shadow runner itself
    crashes on an UNEXPECTED error (not the extractor's own loud
    SidecarExtractionFailure), a sidecar_extraction.watcher_crashed span emits
    loudly so the GM panel shows "the lie detector itself is broken" — never a
    silent continue. Mirrors dispatch_engagement_watcher's crashed-span (ADR-031
    Observability Principle: every subsystem decision, including a broken
    observability pass, emits a span)."""
    from sidequest.agents.sidecar_extractor import run_sidecar_extraction_watcher
    from sidequest.agents import sidecar_extractor as mod

    # Break the mismatch detection (which runs AFTER extraction succeeds) so the
    # runner crashes in the outer exception handler, emitting the watcher_crashed
    # span. This tests the "the lie detector itself is broken" path.
    def broken_detect(*args, **kwargs):
        raise RuntimeError("mismatch witness bug: snapshot corruption")

    monkeypatch.setattr(mod, "detect_sidecar_extraction_mismatch", broken_detect)

    # MUST NOT raise — the turn pipeline continues; the span is the loud signal.
    await run_sidecar_extraction_watcher(
        narration="You meet the mysterious stranger.",
        snapshot=_snapshot(),
        llm=_make_mock_llm(_full_emit()),
    )

    crashed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "sidecar_extraction.watcher_crashed"
    ]
    assert crashed, "runner must emit watcher_crashed span on unexpected error"
    assert crashed[0].attributes.get("error_type") == "RuntimeError"
    assert "snapshot corruption" in crashed[0].attributes.get("error", "")


# AC6 — Wiring: reachable through the real post-narration pipeline
# ===========================================================================


async def test_run_sidecar_extraction_watcher_is_reachable_and_emits(otel_capture) -> None:
    """Behavioral reachability: the public runner exists, is a coroutine, and
    driving it produces the run span — proving it is a real entry point Dev can
    mount in the WS handler, not just an inert unit."""
    from sidequest.agents.sidecar_extractor import run_sidecar_extraction_watcher

    await run_sidecar_extraction_watcher(
        narration="You pocket a silver ring.",
        snapshot=_snapshot(),
        llm=_make_mock_llm(_full_emit()),
    )

    runs = [s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.run"]
    assert len(runs) == 1


def test_runner_wired_into_session_handler() -> None:
    """Pipeline wiring (reflection, NOT source-grep): the WS turn handler — the
    live post-narration seam that already runs run_dispatch_engagement_watcher
    et al. — must import the shadow runner into its module namespace. A missing
    import means the extractor is built but never reached in production (the
    half-wired failure CLAUDE.md forbids)."""
    from sidequest.server import websocket_session_handler as wsh

    assert "run_sidecar_extraction_watcher" in wsh.__dict__, (
        "websocket_session_handler must import run_sidecar_extraction_watcher so the "
        "shadow extractor runs on the live post-narration turn path (ADR-150 step 2)"
    )


# ===========================================================================
# Project-rule enforcement (python.md #9 async, #10 import hygiene)
# ===========================================================================


def test_public_pass_and_runner_are_coroutines() -> None:
    """python.md #9: the extractor pass and runner are async — a non-coroutine
    here would silently never execute when awaited."""
    from sidequest.agents.sidecar_extractor import (
        SidecarExtractor,
        run_sidecar_extraction_watcher,
    )

    assert inspect.iscoroutinefunction(SidecarExtractor.extract)
    assert inspect.iscoroutinefunction(run_sidecar_extraction_watcher)


def test_module_declares_all() -> None:
    """python.md #10: the public module declares __all__ (clear public API), as
    the sibling intent_router / dispatch_engagement_watcher modules do."""
    from sidequest.agents import sidecar_extractor as mod

    assert hasattr(mod, "__all__") and mod.__all__, "module must declare a non-empty __all__"
    for name in ("SidecarExtractor", "SidecarExtractionFailure", "run_sidecar_extraction_watcher"):
        assert name in mod.__all__, f"{name} must be in the module's public __all__"
