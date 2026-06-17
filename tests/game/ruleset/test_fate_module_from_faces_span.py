"""Story 126-7 (ADR-148): the OTEL `source` attribute on fate.action_resolved.

The GM panel is the lie detector: it must be able to confirm a PLAYER roll
really came from the client (``source="player_thrown"``) and an NPC roll really
came from the server RNG (``source="server_rolled"``). Both wrappers emit the
SAME ``fate.action_resolved`` span; only the ``source`` attribute differs.

OTEL span assertion, NOT a source-text grep (server CLAUDE.md "No Source-Text
Wiring Tests"). Uses an explicit per-test tracer (the established
``tests/server/dispatch/test_fate_dispatch_routing.py`` pattern) rather than a
global TracerProvider, so the assertion is robust under the parallel suite.
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.ruleset.fate_resolution import Opposition


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _resolved_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name == "fate.action_resolved"]


def test_from_faces_emits_player_thrown_source():
    exporter, tracer = _otel()
    mod = FateRulesetModule()
    outcome = mod.resolve_action_from_faces(
        skill_rating=2,
        opposition=Opposition(value=1, kind="passive"),
        faces=(1, 0, -1, 1),
        actor="Rux",
        _tracer=tracer,
    )
    assert outcome.dice == (1, 0, -1, 1)  # resolved from the thrown faces
    spans = _resolved_spans(exporter)
    assert spans, "fate.action_resolved span not emitted on the player path"
    assert spans[-1].attributes.get("source") == "player_thrown"
    # The full math still rides the span (the lie detector reads it).
    assert spans[-1].attributes.get("dice") == "1,0,-1,1"


def test_rng_path_emits_server_rolled_source():
    exporter, tracer = _otel()
    mod = FateRulesetModule()
    mod.resolve_action(
        skill_rating=2,
        opposition=Opposition(value=1, kind="passive"),
        rng=random.Random(3),
        actor="Thug",
        _tracer=tracer,
    )
    spans = _resolved_spans(exporter)
    assert spans, "fate.action_resolved span not emitted on the NPC path"
    assert spans[-1].attributes.get("source") == "server_rolled"
