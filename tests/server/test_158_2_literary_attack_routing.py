"""Story 158-2 — IntentRouter must notice literary/described attacks, not only
blunt "attacks X with Y", and must make an unrouted combat verb LOUDLY visible.

Playtest origin (2026-06-22 beneath_sunden, session 697cbc14, turns 5 vs 6):

  T5 (literary, NOT routed): "...draws his short sword, splashes two quick steps
      through the black water, and drives the point hard at the crouched thing
      along the northeast wall..." -> NO confrontation, and ZERO telemetry
      (intent_router/mechanical/confrontation consulted, 0 encounter events,
      total_beats_fired=0). The narrator free-narrated the fight.
  T6 (blunt, routed): "Groucho attacks the Pale Thing with his short sword."
      -> ENCOUNTER_STARTED, opponent seated.

ARCHITECTURE NOTE (load-bearing for why these tests target the layer they do):
The actual routing decision is the Haiku ``IntentRouter.decompose`` LLM pass,
steered by ``CONFRONTATION_TRIGGER_CORE`` (narrator_guardrails). The pre-narrator
pass code itself says paraphrased intent "cannot be lexically detected, by
construction" (``intent_router_pass._confrontation_verb_hits`` docstring). The
test harness for this surface (twin of
``test_intent_router_confrontation_classified.py``) STUBS the router, so the LLM
routing decision is NOT exercised here — it is validated by playtest / the router
eval corpus, not by these deterministic units. See the TEA Assessment + Delivery
Findings on the 158-2 session for the split.

What IS deterministically pinned here (the OTEL lie-detector half, per CLAUDE.md
"the GM panel is the lie detector"):
  AC1-det) a DESCRIBED attack whose combat verb appears in an inflected form
           ("hacks" for authored "hack") is no longer lexically invisible — the
           classification span fires with the verb in ``verb_hits`` instead of
           the turn going silent. Today the raw word-boundary match misses it.
  AC3)     when a combat verb is SEEN but not routed, the diagnostic is emitted
           at INFO (observable), not buried at DEBUG. (158-2 supersedes the
           Story 126-6 DEBUG choice for this signal — see the updated assertion
           in ``test_intent_router_confrontation_classified.py``.)
  AC4)     widening must NOT over-match: described movement / a stemmed near-miss
           ("withdraw" must not hit "draw") stays span-free.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.confrontation_intent_validator import tokenize
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass


class _FakeConfrontationDef:
    """Faithful stand-in for a ConfrontationDef.

    Mirrors the real model: exposes the raw authored ``intent_verbs`` AND the
    derived, suffix-stripped ``intent_verb_set`` (label + beat labels + verbs,
    tokenized with the SAME ``tokenize`` the pack loader uses — see
    ``rules.py`` ConfrontationDef post-init). This keeps the fixture valid
    whether the detector reads the raw list or the tokenized set.
    """

    def __init__(self, confrontation_type: str, label: str, intent_verbs: list[str]):
        self.confrontation_type = confrontation_type
        self.category = "pre_combat"
        self.label = label
        self.intent_verbs = intent_verbs
        self.beats: list[object] = []
        verbs: set[str] = set()
        verbs |= set(tokenize(label))
        for v in intent_verbs:
            verbs |= set(tokenize(v))
        self.intent_verb_set = frozenset(verbs)


class _FakeRules:
    def __init__(self, confrontations):
        self.confrontations = confrontations


class _FakePack:
    def __init__(self, confrontations):
        self.rules = _FakeRules(confrontations)
        self.witnessed_acts = None


class _StubRouter:
    """Returns a fixed package. The LLM decision is deliberately out of scope
    for this surface — see the module docstring."""

    def __init__(self, package: DispatchPackage):
        self._package = package

    async def decompose(self, *, action, state_summary):
        return self._package


def _snapshot() -> GameSnapshot:
    snap = GameSnapshot(world_slug="beneath_sunden")
    snap.genre_slug = "caverns_and_claudes"
    return snap


def _combat_pack() -> _FakePack:
    # Realistic melee-combat verbs; deliberately NO "drive"/"point" so the test
    # exercises an inflected-but-authored verb ("hacks"), not a contrived match.
    return _FakePack(
        [
            _FakeConfrontationDef(
                "combat",
                "Combat",
                ["attack", "strike", "hack", "stab", "slash", "draw"],
            )
        ]
    )


def _empty_package() -> DispatchPackage:
    return DispatchPackage(turn_id="t-empty", per_player=[], cross_player=[], confidence_global=0.4)


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _classified_spans(exporter):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name == "intent_router.confrontation_classified"
    ]


def _unrouted_log_records(caplog):
    return [r for r in caplog.records if "confrontation_verb_unrouted" in r.getMessage()]


# ---------------------------------------------------------------------------
# AC1 (deterministic half): a DESCRIBED attack with an inflected combat verb
# must be noticed — not lexically invisible.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_described_attack_with_inflected_verb_is_detected(otel_capture):
    """T5-shape: a literary attack whose combat verb is inflected ("hacks" for
    authored "hack") and buried in prose. Today the raw ``\\bhack\\b`` match
    misses "hacks" and the turn goes SILENT (no span, no log) — the exact
    zero-telemetry hole the playtest hit. After widening the detector to the
    shared suffix-stripped tokenization, the classification span must fire so
    the GM panel can SEE that a described attack was seen but not seated."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_combat_pack(),
        action=(
            "He hacks and stamps through the seething mass at his feet, "
            "the short blade rising and falling."
        ),
        player_name="Groucho",
    )
    spans = _classified_spans(otel_capture)
    assert len(spans) == 1, (
        "a described attack with an inflected authored combat verb must not be "
        "lexically invisible — the classification span must fire"
    )
    attrs = spans[0].attributes or {}
    assert attrs["emitted"] == 0
    assert "hack" in (attrs["verb_hits"] or ""), (
        "the detected combat verb must be named in verb_hits (got "
        f"{attrs.get('verb_hits')!r})"
    )


# ---------------------------------------------------------------------------
# AC3: an unrouted combat verb must be LOUDLY visible (INFO), not buried at
# DEBUG. 158-2 supersedes Story 126-6's DEBUG choice for this signal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrouted_combat_verb_logs_at_info_not_debug(otel_capture, caplog):
    """A blunt authored verb ("attack") is SEEN but the router emits no
    confrontation dispatch. The miss must surface at INFO or higher so it is
    observable in the GM panel (CLAUDE.md OTEL lie-detector principle; python
    lang-review: error/observability events must not be logged at DEBUG)."""
    router = _StubRouter(_empty_package())
    with caplog.at_level(logging.DEBUG):
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=_snapshot(),
            pack=_combat_pack(),
            action="I attack the pale thing with my short sword.",
            player_name="Groucho",
        )
    unrouted = _unrouted_log_records(caplog)
    assert unrouted, "an unrouted combat verb must still be logged, naming the verbs"
    assert all(r.levelno >= logging.INFO for r in unrouted), (
        "the unrouted-combat-verb signal must be observable (>= INFO), not "
        "buried at DEBUG — 158-2 supersedes the Story 126-6 DEBUG downgrade"
    )


@pytest.mark.asyncio
async def test_unrouted_combat_verb_span_carries_the_verb(otel_capture):
    """Regression guard for the existing span half of AC3: the decline span
    still fires (emitted=0) and still carries the verb for the GM panel."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_combat_pack(),
        action="I attack the pale thing with my short sword.",
        player_name="Groucho",
    )
    spans = _classified_spans(otel_capture)
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["emitted"] == 0
    assert "attack" in (attrs["verb_hits"] or "")


# ---------------------------------------------------------------------------
# AC4: widening must not over-match. Described non-combat actions and stemmed
# near-misses stay span-free.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_described_movement_does_not_register_as_combat(otel_capture, caplog):
    """A described move with no combat verb must stay silent — widening the
    detector must not start firing on ordinary prose (false-positive guard)."""
    router = _StubRouter(_empty_package())
    with caplog.at_level(logging.DEBUG):
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=_snapshot(),
            pack=_combat_pack(),
            action=(
                "I slip quietly along the northeast wall toward the stairs and "
                "listen for movement in the dark."
            ),
            player_name="Groucho",
        )
    assert _classified_spans(otel_capture) == []
    assert _unrouted_log_records(caplog) == []


@pytest.mark.asyncio
async def test_widening_does_not_match_stemmed_near_miss(otel_capture):
    """"withdraw" must not hit authored "draw" even after suffix-stripping —
    tokenization is word-level, not substring. Guards the widening against a
    naive substring/contains implementation."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_combat_pack(),
        action="I withdraw to the boarding house and bed down for the night.",
        player_name="Groucho",
    )
    assert _classified_spans(otel_capture) == []
