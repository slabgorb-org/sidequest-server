"""Story 102-5 (AC5) — improvisation detection is preserved alongside the WN
tool contract.

The contract adds verbs; it must not relax the watcher. When the narrator
narrates a WN-mechanical event WITHOUT the corresponding tool call, the
existing lie-detector surfaces still flag it: a dispatched ``magic_working``
with no engine record on the snapshot emits
``dispatch_engagement.magic_working.mismatch`` exactly as before 102-5 —
with the WN tools registered via the production import path.

These are regression pins (GREEN today, and they must STAY green after the
contract lands). They run with ``import sidequest.agents.tools`` so the
registered WN contract is in play when the watcher executes.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.agents.tools  # noqa: F401  (production registration path)
from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher
from sidequest.agents.tool_registry import default_registry
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-102-5")
    return tracer, exporter


def _magic_working_package(actor: str = "Vesska") -> DispatchPackage:
    return DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id=f"player:{actor}",
                raw_action="(synthetic for 102-5 watcher regression)",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="magic_working",
                        params={"actor": actor},
                        idempotency_key="k1",
                        confidence=1.0,
                        visibility=VisibilityTag(visible_to="all"),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )


def _bare_snapshot() -> GameSnapshot:
    """No cast log, no magic state — the engine demonstrably did NOT fire."""
    return GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")


def test_wn_contract_registration_precondition() -> None:
    """Ties this regression to 102-5: the watcher pins below only mean
    something if they run WITH the WN contract registered. RED with the suite
    (the tools don't exist yet); GREEN alongside the contract."""
    names = set(default_registry.list_names())
    wn_tools = {"wn_attack", "wn_skill_check", "wn_save", "wn_adjudicate_dead_premise"}
    assert wn_tools <= names, f"WN contract not registered: {wn_tools - names}"


def test_magic_working_without_engine_record_still_emits_mismatch() -> None:
    """AC5: narrated-but-not-engaged magic_working still trips the watcher
    with the WN toolset registered. The contract must not relax the polygraph."""
    tracer, exporter = _fresh_tracer_and_exporter()

    run_dispatch_engagement_watcher(
        package=_magic_working_package(),
        snapshot=_bare_snapshot(),
        tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    mismatch = [s for s in spans if s.name == "dispatch_engagement.magic_working.mismatch"]
    assert len(mismatch) == 1, (
        "the magic_working lie-detector went quiet — the 102-5 contract relaxed the watcher"
    )
    attrs: dict[str, Any] = dict(mismatch[0].attributes or {})
    assert attrs.get("subsystem") == "magic_working"
