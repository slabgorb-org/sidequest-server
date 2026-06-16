"""RED (story 114-10): the ``fate.gear_compiled`` OTEL span — the GM-panel lie
detector for chargen gear compilation.

Per the OTEL Observability Principle, materializing a character's starting gear
onto its FateSheet MUST emit a span so the GM panel can verify gear actually
fired (not narrator improv). The span carries, per the design's OTEL section:
``archetype``; and per build: ``gear_ids``, ``aspects_placed``, ``stunts_added``,
``permission_aspects`` (count), ``refresh_before`` / ``refresh_after`` /
``refresh_debited``.

Mirrors ``test_fate_action_classified_span.py`` (emit + route assertions).
The module-level import fails collection in RED until Dev adds the span fn.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES

# NEW in 114-10 — fails in RED until Dev adds fate_gear_compiled_span.
from sidequest.telemetry.spans.fate import fate_gear_compiled_span


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_span_emits_with_attributes():
    exporter, tracer = _otel()
    fate_gear_compiled_span(
        actor="Sam Spade",
        archetype="The Gumshoe",
        gear_ids="noir_trenchcoat,noir_license",
        aspects_placed=2,
        stunts_added=1,
        permission_aspects=1,
        refresh_before=3,
        refresh_after=3,
        refresh_debited=0,
        _tracer=tracer,
    )
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "fate.gear_compiled" in spans, (
        "compiling chargen gear must emit fate.gear_compiled (GM-panel lie detector)"
    )
    attrs = spans["fate.gear_compiled"].attributes
    assert attrs is not None
    assert attrs["archetype"] == "The Gumshoe"
    # The GM panel needs to know WHICH gear fired and on WHOM — assert both the
    # comma-joined id list and the actor land on the span.
    assert attrs["gear_ids"] == "noir_trenchcoat,noir_license"
    assert attrs["actor"] == "Sam Spade"
    assert attrs["aspects_placed"] == 2
    assert attrs["stunts_added"] == 1
    assert attrs["permission_aspects"] == 1
    # The refresh delta is the whole balance story — it must be on the span so
    # the GM panel can prove a stunt-item actually debited refresh.
    assert attrs["refresh_before"] == 3
    assert attrs["refresh_after"] == 3
    assert attrs["refresh_debited"] == 0


def test_span_attributes_capture_a_refresh_debit():
    # A stunt-gear item debits refresh; the span must surface the non-zero debit.
    exporter, tracer = _otel()
    fate_gear_compiled_span(
        actor="Doc",
        archetype="The Tinkerer",
        gear_ids="gadget_belt",
        aspects_placed=0,
        stunts_added=2,
        permission_aspects=0,
        refresh_before=3,
        refresh_after=2,
        refresh_debited=1,
        _tracer=tracer,
    )
    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.gear_compiled")
    assert span.attributes is not None
    assert span.attributes["refresh_debited"] == 1
    assert span.attributes["refresh_after"] == 2


def test_span_is_routed_for_the_gm_panel():
    # Registered as a typed state_transition route so the GM panel surfaces it
    # (mirrors fate.chargen.seeded and the other fate.* routes).
    assert "fate.gear_compiled" in SPAN_ROUTES, (
        "fate.gear_compiled must have a SPAN_ROUTES entry so the GM panel can read it"
    )
    route = SPAN_ROUTES["fate.gear_compiled"]
    extracted = route.extract(
        type(
            "S",
            (),
            {
                "attributes": {
                    "actor": "Sam Spade",
                    "archetype": "The Gumshoe",
                    "gear_ids": "noir_trenchcoat,noir_license",
                    "aspects_placed": 2,
                    "stunts_added": 1,
                    "refresh_debited": 0,
                }
            },
        )()
    )
    assert extracted["field"] == "gear_compiled"
    assert extracted["archetype"] == "The Gumshoe"
    # actor + gear_ids are load-bearing GM-panel fields (who compiled the gear and
    # WHICH gear fired) — the route must pass them through, not just field/archetype.
    assert extracted["actor"] == "Sam Spade"
    assert extracted["gear_ids"] == "noir_trenchcoat,noir_license"
    assert extracted["aspects_placed"] == 2
    assert extracted["stunts_added"] == 1
    assert extracted["refresh_debited"] == 0
