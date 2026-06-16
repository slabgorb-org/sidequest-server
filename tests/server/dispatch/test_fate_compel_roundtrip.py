"""Story 118-5 (ADR-144 F3e) — the compel accept/refuse round-trip (RED).

Closes the F2b deferral. F2b's offer is stateless: ``propose_fate_compel`` fires
``fate.compel.offered`` but persists nothing, so the player has no compel to act
on. F3e

  (a) persists the offered compel onto the active conflict (``offer_compel``
      gains the encounter so the offer survives to the next projection),
  (b) surfaces it in the ``FATE_STATE`` projection the client reads
      (``conflict.pending_compels``), and
  (c) routes the player's choice through ``dispatch_fate_action``:

        compel_accept -> module.accept_compel  (+1 fate point, fate.compel.accepted)
        compel_refuse -> module.refuse_compel  (-1 fate point, fate.compel.refused)

The resolved fate-point delta rides the dispatch result so the UI can show it
inline (the mechanics-first legibility mandate — ADR-144 epic 118). Accepting or
refusing CONSUMES the pending compel (the round-trip completes). Refusal at zero
fate points, and acting on a compel that was never offered, both FAIL LOUD
(No Silent Fallbacks).

FAIL today: ``FateActionPayload.action`` has no ``compel_accept`` /
``compel_refuse`` Literal members (pydantic rejects them at construction),
``FateConflictEntry`` has no ``pending_compels`` field, ``offer_compel`` takes no
``encounter`` and persists nothing, and ``FateDispatchResult`` has no
``fate_point_delta``.
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateEconomyError
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.session import GameSnapshot
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import FateConflictError, dispatch_fate_action

ASPECT = "Owes the Mob a Favor"


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _names(exporter):
    return [s.name for s in exporter.get_finished_spans()]


def _pc(name: str, fate_points: int) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills={"Fight": 2})
    )
    core.fate_sheet.fate_points = fate_points
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _conflict() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )


def _setup(fate_points: int):
    enc = _conflict()
    hero = _pc("Hero", fate_points)
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    ruleset = get_ruleset_module("fate")
    return enc, hero, snap, ruleset


def _accept() -> FateActionPayload:
    return FateActionPayload(request_id="r1", action="compel_accept", aspect_text=ASPECT)


def _refuse() -> FateActionPayload:
    return FateActionPayload(request_id="r1", action="compel_refuse", aspect_text=ASPECT)


def _dispatch(payload, *, enc, snap, ruleset, tracer=None):
    return dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=random.Random(0),
        _tracer=tracer,
    )


# --- persistence + projection -------------------------------------------------


def test_offered_compel_persists_and_surfaces_in_fate_state():
    enc, _hero, snap, ruleset = _setup(fate_points=1)
    ruleset.offer_compel(
        encounter=enc, aspect_text=ASPECT, actor="Hero", reason="They call in the favor"
    )

    payload = build_fate_state_payload(snap)

    assert payload.conflict is not None
    pend = payload.conflict.pending_compels
    assert [c.aspect for c in pend] == [ASPECT]
    assert pend[0].target == "Hero", "the projection names who is being compelled"


def test_no_pending_compels_before_any_offer():
    _enc, _hero, snap, _ruleset = _setup(fate_points=1)
    payload = build_fate_state_payload(snap)
    assert payload.conflict is not None
    assert payload.conflict.pending_compels == []


def test_offer_on_a_resolved_encounter_fires_span_but_persists_nothing():
    # The offer_compel guard `encounter is not None and not encounter.resolved`: a
    # resolved conflict is over, so its compels would be stale fiction. The offered span
    # STILL fires (the GM panel sees WHAT was offered), but nothing is persisted — the
    # projection's `not enc.resolved` gate would drop it anyway. This pins the resolved
    # branch so the guard can't be silently removed (without it, compels from finished
    # conflicts would start surfacing in the projection).
    enc, _hero, snap, ruleset = _setup(fate_points=1)
    enc.resolved = True
    exporter, tracer = _otel()

    ruleset.offer_compel(
        encounter=enc, aspect_text=ASPECT, actor="Hero", reason="too late", _tracer=tracer
    )

    assert "fate.compel.offered" in _names(exporter), "the offer span fires even when resolved"
    assert enc.pending_compels == [], "a resolved conflict persists no pending compel"


# --- accept -------------------------------------------------------------------


def test_compel_accept_earns_a_point_emits_span_and_reports_delta():
    enc, hero, snap, ruleset = _setup(fate_points=1)
    ruleset.offer_compel(encounter=enc, aspect_text=ASPECT, actor="Hero", reason="r")
    exporter, tracer = _otel()

    result = _dispatch(_accept(), enc=enc, snap=snap, ruleset=ruleset, tracer=tracer)

    assert hero.core.fate_sheet.fate_points == 2, "accepting a compel earns one fate point"
    assert result.fate_point_delta == 1, "the +1 delta rides the result for the UI"
    assert result.commitment_pending is False, "accept is pre-roll, not a sealed action"
    assert "fate.compel.accepted" in _names(exporter)


def test_compel_accept_consumes_the_pending_compel():
    enc, _hero, snap, ruleset = _setup(fate_points=1)
    ruleset.offer_compel(encounter=enc, aspect_text=ASPECT, actor="Hero", reason="r")

    _dispatch(_accept(), enc=enc, snap=snap, ruleset=ruleset)

    payload = build_fate_state_payload(snap)
    assert payload.conflict is not None
    assert payload.conflict.pending_compels == [], "an accepted compel is consumed"


# --- refuse -------------------------------------------------------------------


def test_compel_refuse_pays_a_point_emits_span_and_reports_delta():
    enc, hero, snap, ruleset = _setup(fate_points=2)
    ruleset.offer_compel(encounter=enc, aspect_text=ASPECT, actor="Hero", reason="r")
    exporter, tracer = _otel()

    result = _dispatch(_refuse(), enc=enc, snap=snap, ruleset=ruleset, tracer=tracer)

    assert hero.core.fate_sheet.fate_points == 1, "refusing a compel pays one fate point (SRD)"
    assert result.fate_point_delta == -1, "the -1 delta rides the result for the UI"
    assert "fate.compel.refused" in _names(exporter)


def test_compel_refuse_consumes_the_pending_compel():
    enc, _hero, snap, ruleset = _setup(fate_points=2)
    ruleset.offer_compel(encounter=enc, aspect_text=ASPECT, actor="Hero", reason="r")

    _dispatch(_refuse(), enc=enc, snap=snap, ruleset=ruleset)

    payload = build_fate_state_payload(snap)
    assert payload.conflict is not None
    assert payload.conflict.pending_compels == []


def test_compel_refuse_at_zero_points_is_rejected_loudly():
    enc, hero, snap, ruleset = _setup(fate_points=0)
    ruleset.offer_compel(encounter=enc, aspect_text=ASPECT, actor="Hero", reason="r")
    exporter, tracer = _otel()

    with pytest.raises((FateEconomyError, FateConflictError)):
        _dispatch(_refuse(), enc=enc, snap=snap, ruleset=ruleset, tracer=tracer)

    assert hero.core.fate_sheet.fate_points == 0
    assert "fate.compel.refused" not in _names(exporter)
    # The rejected refusal leaves the compel on the table — it was not consumed.
    payload = build_fate_state_payload(snap)
    assert payload.conflict is not None
    assert [c.aspect for c in payload.conflict.pending_compels] == [ASPECT]


# --- fail loud: acting on a phantom compel ------------------------------------


def test_accepting_a_compel_that_was_never_offered_fails_loud():
    # No offer_compel call — there is nothing to accept. Earning a fate point for a
    # compel the engine never offered is exactly the silent fallback the project
    # forbids; the dispatch must reject it.
    enc, hero, snap, ruleset = _setup(fate_points=1)

    with pytest.raises(FateConflictError):
        _dispatch(_accept(), enc=enc, snap=snap, ruleset=ruleset)

    assert hero.core.fate_sheet.fate_points == 1, "no point is earned for a phantom compel"


def test_refusing_a_compel_that_was_never_offered_fails_loud():
    # The refuse leg of the phantom guard, mirroring the accept case above. Both verbs
    # share resolve_compel's `find_pending_compel is None -> raise`, but the refuse
    # routing is exercised independently so a future divergence can't silently let a
    # never-offered refusal through. fate_points=1 isolates the PHANTOM path from the
    # refuse-at-0 economy path: the raise must come from the missing offer (before any
    # spend), so no fate point is paid and no decline span fires.
    enc, hero, snap, ruleset = _setup(fate_points=1)
    exporter, tracer = _otel()

    with pytest.raises(FateConflictError):
        _dispatch(_refuse(), enc=enc, snap=snap, ruleset=ruleset, tracer=tracer)

    assert hero.core.fate_sheet.fate_points == 1, "no point is paid for a phantom compel"
    assert "fate.compel.refused" not in _names(exporter), "no decline span on a phantom refusal"
