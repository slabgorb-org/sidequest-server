"""Tests for the Fate honesty lie-detector watcher (Story 116-4 / ADR-144 F2c).

The sibling of ``dispatch_engagement_watcher`` for the Fate path. The
dispatch-engagement watcher catches "router dispatched X, engine didn't engage";
the improvised-combat watcher catches narrated wounds with no mechanical
backing. This watcher catches the Fate-specific lie: the narrator claims a
**Fate outcome** — an advantage created, a foe taken out — that the engine state
does not show. It reads narration-vs-state (the authoritative snapshot), never a
span buffer (spans are ephemeral GM-panel signals; state is durable).

Module under test (created by Dev in GREEN):
- ``sidequest/agents/fate_engagement_watcher.py``

Two witnesses this slice (conservative — a lie-detector that cries wolf is worse
than none, so each errs toward under-flagging; CLAUDE.md OTEL principle):

- ``create_advantage``: prose asserts a NEW advantage/aspect was created but
  ``encounter.situation_aspects`` has none → mismatch. The canonical Fate
  construction "creates an advantage" / "gains the advantage" is the pinned
  conservative claim phrasing these tests assume the matcher recognizes (mirror
  of the dispatch suite's ``_WOUND_PROSE`` markers).
- ``taken_out``: prose asserts a participant is taken out / out of the fight but
  no ``encounter.actors`` entry is ``withdrawn`` → mismatch. Canonical claim
  phrasing: "taken out" / "out of the fight".

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" — ``test_watcher_wired_into_session_handler``
- "No Source-Text Wiring Tests" — wiring test uses reflection on
  ``module.__dict__`` + behavior, never source-grep
- "OTEL Observability Principle" — every mismatch emits ``fate.narration.mismatch``;
  every honest turn emits ZERO spans (no false positive)
- "No Silent Fallbacks" — the watcher is NON-FATAL post-narration: an internal
  error is caught and surfaced as a crashed span, never propagated to tear down
  WS turn delivery (``test_watcher_is_non_fatal_on_internal_error``)
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect
from sidequest.game.session import GameSnapshot

# ---------------------------------------------------------------------------
# OTEL plumbing — isolated tracer/exporter per test
# ---------------------------------------------------------------------------


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _fate_encounter(
    *,
    situation_aspects: list[Aspect] | None = None,
    actors: list[EncounterActor] | None = None,
    resolved: bool = False,
) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors or [],
        situation_aspects=situation_aspects or [],
        resolved=resolved,
    )


def _snapshot(*, encounter: StructuredEncounter | None = None) -> GameSnapshot:
    return GameSnapshot(genre_slug="fate_test", world_slug="fate_world", encounter=encounter)


def _aspect(text: str) -> Aspect:
    return Aspect(text=text, kind="situation", free_invokes=1)


def _actor(name: str, side: str, *, withdrawn: bool = False) -> EncounterActor:
    return EncounterActor(name=name, role="r", side=side, withdrawn=withdrawn)  # type: ignore[arg-type]


# Pinned conservative claim prose (the Fate-canonical constructions the matcher
# must recognize). Mirrors the dispatch suite's _WOUND_PROSE marker fixtures.
_CREATE_ADV_CLAIM_PROSE = (
    "With a sharp feint, Vesska creates an advantage on the guard, who is now "
    "Off-Balance against the rail."
)
_TAKEN_OUT_CLAIM_PROSE = (
    "Vesska's strike lands clean and the Zodangan is taken out, dropping to the "
    "deck and out of the fight."
)
_BOTH_CLAIMS_PROSE = (
    "Vesska creates an advantage — the guard is now Off-Balance — then follows "
    "through, and the second Zodangan is taken out, out of the fight entirely."
)
# Benign tension prose: no create-advantage and no taken-out claim (must NOT flag).
_BENIGN_PROSE = (
    "Vesska circles the guard warily, watching the lamplight shift across the wet "
    "stones. Neither of them has moved, and the air is thick with held breath."
)


# ---------------------------------------------------------------------------
# Witness 1 — create_advantage claim vs situation_aspects (pure)
# ---------------------------------------------------------------------------


def test_create_advantage_claim_with_no_situation_aspect_is_a_mismatch() -> None:
    """Prose claims an advantage was created, but ``situation_aspects`` is empty
    → the engine placed nothing → one create_advantage mismatch."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    snap = _snapshot(encounter=_fate_encounter(situation_aspects=[]))
    mismatches = detect_fate_narration_mismatch(
        narration=_CREATE_ADV_CLAIM_PROSE, snapshot=snap, package=None
    )
    assert len(mismatches) == 1, f"expected one create_advantage mismatch, got {mismatches!r}"
    assert mismatches[0].subsystem == "create_advantage"


def test_create_advantage_claim_with_matching_situation_aspect_is_no_mismatch() -> None:
    """Same claim prose, but a situation aspect IS present in state → the engine
    backed the claim → no mismatch (claim-vs-state, the honest case)."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    snap = _snapshot(encounter=_fate_encounter(situation_aspects=[_aspect("Off-Balance")]))
    mismatches = detect_fate_narration_mismatch(
        narration=_CREATE_ADV_CLAIM_PROSE, snapshot=snap, package=None
    )
    assert mismatches == [], f"a backed create-advantage must not flag; got {mismatches!r}"


# ---------------------------------------------------------------------------
# Witness 2 — taken_out claim vs withdrawn actors (pure)
# ---------------------------------------------------------------------------


def test_taken_out_claim_with_no_withdrawn_actor_is_a_mismatch() -> None:
    """Prose claims a foe is taken out, but no actor is ``withdrawn`` → the
    engine took no one out → one taken_out mismatch."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    enc = _fate_encounter(actors=[_actor("Vesska", "player"), _actor("Zodangan", "opponent")])
    mismatches = detect_fate_narration_mismatch(
        narration=_TAKEN_OUT_CLAIM_PROSE, snapshot=_snapshot(encounter=enc), package=None
    )
    assert len(mismatches) == 1, f"expected one taken_out mismatch, got {mismatches!r}"
    assert mismatches[0].subsystem == "taken_out"


def test_taken_out_claim_with_withdrawn_actor_is_no_mismatch() -> None:
    """Same claim prose, but an actor IS withdrawn → the engine actually took
    someone out → no mismatch."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    enc = _fate_encounter(
        actors=[_actor("Vesska", "player"), _actor("Zodangan", "opponent", withdrawn=True)]
    )
    mismatches = detect_fate_narration_mismatch(
        narration=_TAKEN_OUT_CLAIM_PROSE, snapshot=_snapshot(encounter=enc), package=None
    )
    assert mismatches == [], f"a backed taken-out must not flag; got {mismatches!r}"


# ---------------------------------------------------------------------------
# False-positive discipline + gating (no-op cases)
# ---------------------------------------------------------------------------


def test_benign_prose_with_active_encounter_is_no_mismatch() -> None:
    """Ordinary tension prose with no create-advantage / taken-out claim must
    NOT flag, even with a live Fate encounter — the lie-detector under-flags."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    enc = _fate_encounter(
        actors=[_actor("Vesska", "player"), _actor("Zodangan", "opponent")],
        situation_aspects=[],
    )
    mismatches = detect_fate_narration_mismatch(
        narration=_BENIGN_PROSE, snapshot=_snapshot(encounter=enc), package=None
    )
    assert mismatches == [], f"benign prose must not flag; got {mismatches!r}"


def test_no_encounter_is_a_noop() -> None:
    """No active Fate encounter → the watcher is a no-op (early return), even on
    claim prose. Nothing to compare the claim against."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    mismatches = detect_fate_narration_mismatch(
        narration=_BOTH_CLAIMS_PROSE, snapshot=_snapshot(encounter=None), package=None
    )
    assert mismatches == [], f"no-encounter turn must be a no-op; got {mismatches!r}"


def test_empty_narration_is_a_noop() -> None:
    """Empty narration → nothing to inspect → no mismatch (guards the wrapper's
    ``getattr(result, 'narration', '') or ''`` empty-string call site)."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    enc = _fate_encounter(actors=[_actor("Zodangan", "opponent")])
    mismatches = detect_fate_narration_mismatch(
        narration="", snapshot=_snapshot(encounter=enc), package=None
    )
    assert mismatches == []


# ---------------------------------------------------------------------------
# Pure-function discipline — detect returns records WITHOUT emitting OTEL
# ---------------------------------------------------------------------------


def test_pure_detect_emits_no_spans() -> None:
    """The pure ``detect_fate_narration_mismatch`` returns records without
    touching the tracer — testable in isolation, and a thin wrapper emits."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    _, exporter = _fresh_tracer_and_exporter()
    snap = _snapshot(encounter=_fate_encounter(situation_aspects=[]))

    mismatches = detect_fate_narration_mismatch(
        narration=_CREATE_ADV_CLAIM_PROSE, snapshot=snap, package=None
    )

    assert len(mismatches) >= 1
    assert [s for s in exporter.get_finished_spans() if "fate.narration" in s.name] == []


def test_mismatch_record_carries_subsystem_claim_reason() -> None:
    """Each FateNarrationMismatch carries enough for the wrapper to emit a useful
    span: subsystem + the claimed prose excerpt + a reason."""
    from sidequest.agents.fate_engagement_watcher import detect_fate_narration_mismatch

    enc = _fate_encounter(actors=[_actor("Zodangan", "opponent")])
    mismatches = detect_fate_narration_mismatch(
        narration=_TAKEN_OUT_CLAIM_PROSE, snapshot=_snapshot(encounter=enc), package=None
    )
    assert len(mismatches) == 1
    m = mismatches[0]
    assert m.subsystem == "taken_out"
    assert isinstance(m.claim, str) and m.claim, "mismatch must carry the claimed prose excerpt"
    assert isinstance(m.reason, str) and m.reason, "mismatch must carry a reason string"


# ---------------------------------------------------------------------------
# OTEL wrapper — emits fate.narration.mismatch per mismatch
# ---------------------------------------------------------------------------


def test_run_watcher_emits_fate_narration_mismatch_span() -> None:
    """``run_fate_engagement_watcher`` emits one ``fate.narration.mismatch`` span
    carrying subsystem + claim + reason — the F2 lie-detector the GM panel reads."""
    from sidequest.agents.fate_engagement_watcher import run_fate_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    snap = _snapshot(encounter=_fate_encounter(situation_aspects=[]))

    run_fate_engagement_watcher(
        narration=_CREATE_ADV_CLAIM_PROSE, package=None, snapshot=snap, tracer=tracer
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == "fate.narration.mismatch"]
    assert len(spans) == 1, (
        f"expected one fate.narration.mismatch span; got {[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "create_advantage"
    assert attrs.get("claim"), "span must carry the claimed prose excerpt"
    assert attrs.get("reason"), "span must carry the mismatch reason"


def test_run_watcher_emits_one_span_per_mismatch() -> None:
    """A turn claiming BOTH a created advantage AND a taken-out, with neither
    backed in state, emits two distinct spans (per-witness accountability)."""
    from sidequest.agents.fate_engagement_watcher import run_fate_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    enc = _fate_encounter(
        actors=[_actor("Vesska", "player"), _actor("Zodangan", "opponent")],
        situation_aspects=[],
    )

    run_fate_engagement_watcher(
        narration=_BOTH_CLAIMS_PROSE, package=None, snapshot=_snapshot(encounter=enc), tracer=tracer
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == "fate.narration.mismatch"]
    assert len(spans) == 2, f"expected two mismatch spans; got {len(spans)}"
    subsystems = sorted(str(dict(s.attributes or {}).get("subsystem")) for s in spans)
    assert subsystems == ["create_advantage", "taken_out"]


def test_run_watcher_no_span_on_honest_turn() -> None:
    """A backed turn (claim + matching state) emits ZERO spans — no false beep."""
    from sidequest.agents.fate_engagement_watcher import run_fate_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    enc = _fate_encounter(
        actors=[_actor("Zodangan", "opponent", withdrawn=True)],
        situation_aspects=[_aspect("Off-Balance")],
    )

    run_fate_engagement_watcher(
        narration=_BOTH_CLAIMS_PROSE, package=None, snapshot=_snapshot(encounter=enc), tracer=tracer
    )

    spans = [s for s in exporter.get_finished_spans() if "fate.narration" in s.name]
    assert spans == [], (
        f"a fully-backed turn must emit no mismatch span; got {[s.name for s in spans]}"
    )


# ---------------------------------------------------------------------------
# Non-fatal contract — a crash inside the watcher must NOT abort the turn
# ---------------------------------------------------------------------------


def test_watcher_is_non_fatal_on_internal_error(monkeypatch: Any) -> None:
    """The watcher runs POST-narration in the WS pipeline; a raised exception
    must NOT propagate (it would tear down turn delivery — the 2026-06-07
    dispatch-watcher crash). It is caught and surfaced as a crashed span.

    Forces the failure by replacing the pure detector with a raiser — the
    wrapper's try/except is the contract under test."""
    import sidequest.agents.fate_engagement_watcher as watcher_mod

    def _boom(*, narration: str, snapshot: Any, package: Any) -> list[Any]:
        raise RuntimeError("synthetic fate watcher explosion")

    monkeypatch.setattr(watcher_mod, "detect_fate_narration_mismatch", _boom)

    tracer, exporter = _fresh_tracer_and_exporter()
    snap = _snapshot(encounter=_fate_encounter(situation_aspects=[]))

    # MUST NOT raise.
    watcher_mod.run_fate_engagement_watcher(
        narration=_CREATE_ADV_CLAIM_PROSE, package=None, snapshot=snap, tracer=tracer
    )

    crashed = [s for s in exporter.get_finished_spans() if "crashed" in s.name]
    assert len(crashed) >= 1, (
        "an internal watcher error must surface a crashed span (loud), not vanish; "
        f"got spans {[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(crashed[0].attributes or {})
    assert attrs.get("error_type") == "RuntimeError"
    assert "synthetic fate watcher explosion" in str(attrs.get("error", ""))


# ---------------------------------------------------------------------------
# Wiring (CLAUDE.md "Every Test Suite Needs a Wiring Test",
# "No Source-Text Wiring Tests" → reflection on module namespace)
# ---------------------------------------------------------------------------


def test_module_exports_public_api() -> None:
    """Wiring tripwire #1: the production module exports the pure decision
    function and the OTEL-emitting wrapper at the names the handler imports."""
    from sidequest.agents import fate_engagement_watcher as mod

    assert hasattr(mod, "detect_fate_narration_mismatch")
    assert hasattr(mod, "run_fate_engagement_watcher")
    assert callable(mod.detect_fate_narration_mismatch)
    assert callable(mod.run_fate_engagement_watcher)


def test_watcher_wired_into_session_handler() -> None:
    """Wiring tripwire #2 (reflection, not source-grep): the handler module's
    runtime namespace must reference ``run_fate_engagement_watcher`` (the
    function directly or the module) — else the Fate lie-detector never fires
    in production. Mirrors the dispatch / improvised-combat wiring tests.

    Imports via ``sidequest.server.session_handler`` first to dodge the known
    circular import between session_handler and websocket_session_handler."""
    import sys

    import sidequest.server.session_handler  # noqa: F401 — load-order fix

    handler_mod = sys.modules["sidequest.server.websocket_session_handler"]
    has_function = "run_fate_engagement_watcher" in handler_mod.__dict__
    has_module = "fate_engagement_watcher" in handler_mod.__dict__
    assert has_function or has_module, (
        "websocket_session_handler must import run_fate_engagement_watcher (or the "
        "fate_engagement_watcher module) — the post-narration Fate lie-detector "
        "call site is missing; without it the watcher cannot fire in production."
    )
