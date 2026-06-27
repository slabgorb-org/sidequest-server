"""ADR-153 §6 — a sealed-letter dogfight is ship-scale on EVERY seating door,
not just the location fallback (playtest finding 158-34, hardening).

Story 158-34's delivering change (#1084 / commit 479f7e19) firewalled the
**location-fallback** door: it added ``resolution_mode != sealed_letter_lookup``
to the personal-NPC location-fallback gate so a co-located ground creature can
no longer be conscripted as the enemy ship. That fix is real and tested
(``test_dogfight_seating_scale.py``).

But the seater has a *second* door the §6 firewall left open: the **router**
door. The intent router (ADR-113) runs first and names contacts in
``npcs_present``. If it mis-identifies the co-located Monster-Manual ground
creature ("Gengineered Killer") as the dogfight contact, the mention arrives in
``npcs_present`` — and BOTH seating gates (the location fallback AND the
default-from-frame branch) are guarded by ``not npcs_present``, so both are
skipped. The personal-scale creature then sails through the sealed-letter arity
check (``len(npcs_present) == 1``) and is seated as the enemy ship. That is
158-34's exact symptom (a ground creature standing in for the enemy vessel),
reached through the router door instead of the location-fallback door.

ADR-153 §6: the dogfight Other is a ship/chassis — the def frame or a
ship-scale router contact — **never** a personal-scale creature, regardless of
which door it arrives through. A router-named ``is_creature`` opponent must be
rejected and replaced with the default-from-frame ship, so the duel still seats
a ship Other (ADR-116: "the duel always has a ship Other"). The GM-panel
``participant.joined`` span must then record ``source="frame_default"`` — never
``source="router_named"`` on a ground creature.

RED today: ``npcs_present=[NpcMention(is_creature=True)]`` seats "Gengineered
Killer" as the blue (opponent) actor with ``source="router_named"``; no
ship-scale guard fires on the router-named opponent.

Synthetic ``swn_test_pack`` fixtures only (project rule
``feedback_no_content_in_unit_tests``); the live pack's dogfight shape is a
content invariant guarded by the pack validator, not pytest.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from sidequest.telemetry.setup import init_tracer
from tests.fixtures.dogfight_playtest_encounter import (
    GENRE_SLUG,
    make_dogfight_pack,
    make_empty_snapshot,
)

# The 158-34 ground creature: a Monster-Manual personal-scale beast that must
# never be seated as the enemy *ship*.
GROUND_CREATURE = "Gengineered Killer"


@pytest.fixture
def span_capture() -> Iterator[InMemorySpanExporter]:
    """Capture spans emitted to the live OTEL provider singleton.

    Mirrors ``tests/agents/conftest.py::otel_capture`` (itself modelled on
    ``tests/server/test_chargen_persist_and_play.py``): the span context
    managers close over the global provider, so a SimpleSpanProcessor +
    InMemorySpanExporter installed on that singleton is the reliable way to
    observe ``participant.joined`` spans emitted by the seating path.
    """
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "live provider is not a recording TracerProvider — spans would be NoOp"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_router_named_ground_creature_is_not_seated_as_dogfight_opponent(
    span_capture: InMemorySpanExporter,
) -> None:
    """A router-named PERSONAL-scale creature must not be seated as the enemy
    ship. The §6 firewall must cover the router door too: the seater rejects the
    ``is_creature`` mention and sources the default-from-frame ship instead, so
    the duel still seats a ship Other (ADR-116) and the seat is recorded
    ``source="frame_default"`` — never ``router_named`` on a ground creature."""
    pack = make_dogfight_pack()  # dogfight def: opponent_default_stats.hp == 8
    snapshot = make_empty_snapshot(pc_name="Pilot")

    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type="dogfight",
        player_name="Pilot",
        # The router door: a personal-scale ground creature named as the contact.
        npcs_present=[
            NpcMention(
                name=GROUND_CREATURE,
                role="hostile",
                side="opponent",
                is_creature=True,
            ),
        ],
        genre_slug=GENRE_SLUG,
    )

    # ADR-116: the dogfight still seats a ship Other — it never refuses.
    assert enc is not None, "a ship dogfight must still seat a ship Other (ADR-116)"
    opponents = [a for a in enc.actors if a.side == "opponent"]
    assert len(opponents) == 1, (
        f"expected exactly one opponent actor, got {[a.name for a in opponents]!r}"
    )
    # The personal-scale ground creature was NOT conscripted as the enemy ship.
    assert opponents[0].name != GROUND_CREATURE, (
        "a router-named ground creature must never be seated as the dogfight "
        "opponent — the §6 ship-scale firewall must cover the router door, not "
        "only the location fallback"
    )
    assert all(a.name != GROUND_CREATURE for a in enc.actors), (
        "the ground creature must not be seated in the dogfight at all"
    )
    # The seated opponent is the default-from-frame ship: its backing core HP
    # comes from opponent_default_stats["hp"] == 8 (not a personal creature).
    core = snapshot.find_creature_core(opponents[0].name)
    assert core is not None, (
        f"frame-default opponent {opponents[0].name!r} has no backing creature "
        "core — the duel cannot resolve via hp_depletion without one"
    )
    assert core.hp.max == 8, (
        "the seated opponent must be the default-from-frame ship "
        f"(opponent_default_stats.hp=8), got max={core.hp.max}"
    )

    # OTEL lie-detector: the opponent seat is recorded source="frame_default",
    # proving the engine REJECTED the router-named creature and sourced the frame
    # ship — never "router_named" on a ground creature (CLAUDE.md OTEL principle:
    # a substitution this load-bearing must be observable on the GM panel).
    opponent_joins = [
        span
        for span in span_capture.get_finished_spans()
        if span.name == "participant.joined"
        and (span.attributes or {}).get("side") == "opponent"
    ]
    assert opponent_joins, (
        "an opponent participant.joined span must fire so the GM panel can audit "
        "where the dogfight Other came from (ADR-116 observability)"
    )
    assert all(
        (span.attributes or {}).get("source") == "frame_default"
        for span in opponent_joins
    ), (
        "the opponent seat must record source='frame_default' (the engine sourced "
        "the frame ship after rejecting the personal-scale router mention), got "
        f"{[(span.attributes or {}).get('source') for span in opponent_joins]!r}"
    )
    assert all(
        (span.attributes or {}).get("name") != GROUND_CREATURE
        for span in opponent_joins
    ), "no participant.joined span may name the ground creature as a seated actor"
