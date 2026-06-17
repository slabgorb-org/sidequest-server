"""QUEST-MAJOR (sq-playtest 2026-06-14, heavy_metal/barsoom): the narrator
authored a concrete objective in prose ("find the missing egg-keeper, settle the
debt, claim the egg") but NEVER called the ``record_quest`` WRITE tool, so
``quest_log`` stayed empty and the engine filed the hook as a dormant ghost. The
narration and the engine disagreed about whether a live thread exists.

These tests pin the ``unminted-objective`` lie-detector — the quest analogue of
the improvised-combat detector. It beeps (observability, never a control-flow
block) when objective-establishing prose appears while ``quest_log`` is empty.

References:
- detect_unminted_objective: pure decision, no OTEL
- run_unminted_objective_watcher: thin OTEL-emitting wrapper
- empty-quest_log gate: a correct record_quest mint fills the log during
  narration, so a minted quest never trips the detector (no false positive)
- wiring: the handler imports the watcher (CLAUDE.md "Every Test Suite Needs a
  Wiring Test")
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot, QuestEntry

# An excerpt of the kind of objective-giving prose the barsoom narrator wrote.
_OBJECTIVE_PROSE = (
    "The keeper has not returned in eleven days. Pay her debt, if you can find "
    "her, and the egg is yours — until then it does not move."
)
_QUIET_PROSE = (
    "You push through the bead curtain into the pot-house. The air is thick with "
    "smoke and the low murmur of off-shift dock hands. Someone is singing badly."
)


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _snapshot(*, quest_log: dict[str, QuestEntry] | None = None) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="barsoom")
    if quest_log is not None:
        snap.quest_log = quest_log
    return snap


# ---------------------------------------------------------------------------
# Pure decision — detect_unminted_objective
# ---------------------------------------------------------------------------


def test_fires_on_objective_prose_with_empty_quest_log() -> None:
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    evidence = detect_unminted_objective(narration=_OBJECTIVE_PROSE, snapshot=_snapshot())

    assert evidence is not None, "objective prose + empty quest_log must flag a mismatch"
    assert "quest_log is empty" in evidence


def test_quiet_scene_does_not_fire() -> None:
    """No objective-giving marker → no beep, even with an empty quest_log. Guards
    against crying wolf on every early-game scene that merely sets a mood."""
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    assert detect_unminted_objective(narration=_QUIET_PROSE, snapshot=_snapshot()) is None


def test_existing_quest_suppresses_detector() -> None:
    """A non-empty quest_log stands the detector down: a correct ``record_quest``
    mint fills the log DURING narration (before this post-narration pass), so a
    minted objective never trips the beep."""
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    snap = _snapshot(
        quest_log={
            "egg": QuestEntry(title="The Incubator", objective="Find the keeper", status="active")
        }
    )
    assert detect_unminted_objective(narration=_OBJECTIVE_PROSE, snapshot=snap) is None


def test_empty_narration_does_not_fire() -> None:
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    assert detect_unminted_objective(narration="", snapshot=_snapshot()) is None


# ---------------------------------------------------------------------------
# OTEL wrapper — run_unminted_objective_watcher
# ---------------------------------------------------------------------------


def test_watcher_emits_span_on_hit() -> None:
    from sidequest.agents.dispatch_engagement_watcher import run_unminted_objective_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    run_unminted_objective_watcher(narration=_OBJECTIVE_PROSE, snapshot=_snapshot(), tracer=tracer)

    names = [s.name for s in exporter.get_finished_spans()]
    assert "narration.unminted_objective.suspected" in names, (
        f"expected the unminted-objective span; got {names}"
    )


def test_watcher_silent_on_quiet_scene() -> None:
    from sidequest.agents.dispatch_engagement_watcher import run_unminted_objective_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    run_unminted_objective_watcher(narration=_QUIET_PROSE, snapshot=_snapshot(), tracer=tracer)

    names = [s.name for s in exporter.get_finished_spans()]
    assert "narration.unminted_objective.suspected" not in names


# ---------------------------------------------------------------------------
# Wiring (CLAUDE.md "Every Test Suite Needs a Wiring Test")
# ---------------------------------------------------------------------------


def test_watcher_module_exports_public_api() -> None:
    from sidequest.agents import dispatch_engagement_watcher as mod

    assert callable(getattr(mod, "detect_unminted_objective", None))
    assert callable(getattr(mod, "run_unminted_objective_watcher", None))


def test_watcher_wired_into_session_handler() -> None:
    """The handler module's runtime namespace must reference the watcher — else
    it can never fire in production (reflection, not source-grep)."""
    import sys

    import sidequest.server.session_handler  # noqa: F401 — load-order fix

    handler_mod = sys.modules["sidequest.server.websocket_session_handler"]
    has_function = "run_unminted_objective_watcher" in handler_mod.__dict__
    has_module = "dispatch_engagement_watcher" in handler_mod.__dict__
    assert has_function or has_module, (
        "websocket_session_handler must import run_unminted_objective_watcher — "
        "without it the quest lie-detector cannot fire in production"
    )


def test_span_routes_to_gm_panel() -> None:
    """The new span must be registered in SPAN_ROUTES so it reaches the GM
    panel (component=narrator)."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    name = "narration.unminted_objective.suspected"
    assert name in SPAN_ROUTES, f"{name} not registered in SPAN_ROUTES"
    assert SPAN_ROUTES[name].component == "narrator"
