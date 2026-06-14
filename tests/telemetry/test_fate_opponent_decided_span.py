from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.fate import fate_opponent_decided_span


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_span_emits_with_attributes():
    exporter, tracer = _otel()
    fate_opponent_decided_span(
        actor="Thug",
        action="attack",
        skill="Fight",
        target="Hero",
        ladder_total=4,
        _tracer=tracer,
    )
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "fate.opponent.decided" in spans
    attrs = spans["fate.opponent.decided"].attributes
    assert attrs["actor"] == "Thug"
    assert attrs["action"] == "attack"
    assert attrs["skill"] == "Fight"
    assert attrs["target"] == "Hero"
    assert attrs["ladder_total"] == 4


def test_span_is_routed_for_the_gm_panel():
    # Registered as a typed state_transition route so the GM panel surfaces it.
    assert "fate.opponent.decided" in SPAN_ROUTES
    route = SPAN_ROUTES["fate.opponent.decided"]
    assert route.event_type == "state_transition"
    assert route.component == "fate"
    extracted = route.extract(
        type(
            "S",
            (),
            {
                "attributes": {
                    "actor": "Thug",
                    "action": "create_advantage",
                    "skill": "Provoke",
                    "target": "Hero",
                    "ladder_total": 3,
                }
            },
        )()
    )
    assert extracted["field"] == "opponent_decided"
    assert extracted["actor"] == "Thug"
    assert extracted["action"] == "create_advantage"
    assert extracted["skill"] == "Provoke"
    assert extracted["target"] == "Hero"
    assert extracted["ladder_total"] == 3
