"""Story 118-6 AC#3 — CONCEDE + player_action: the rider-less decision.

``fate.action.flavor_rider`` fires only on proactive actions — it sits AFTER the
``action == 'concede'`` early-return in ``dispatch_fate_action``. The AC: decide
whether a concession carrying freeform ``player_action`` text emits the rider /
threads to the narrator, or is intentionally rider-less.

TEA DECISION (ratified in the 118-6 session Delivery Findings; open to
Architect/Reviewer override): a concession is RIDER-LESS. Rationale:
  * Concede is pre-roll and withdraws the actor; its narrative is the GM's to
    narrate from the ``fate.conceded`` event (which already attests it), not a
    player-authored freeform rider.
  * Keeping concede rider-less holds the narrator-bound ``player_action`` surface
    to exactly ONE path (proactive actions, AC#2), so there is a single seam to
    sanitize rather than two — a smaller injection surface.
  * "Cut the Dull Bits": a concession resolves the scene fast; it does not earn
    its own freeform beat.

These GUARDS LOCK that decision — they pin current behavior so a future change
cannot quietly thread concede flavor to the narrator (which would also bypass the
AC#2 sanitization seam). If the project later decides concede SHOULD carry flavor,
these guards must be revised together with an AC#2-style sanitized seam — never
silently.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import dispatch_fate_action

_FLAVOR_SPAN = "fate.action.flavor_rider"


class _FixedRng:
    def choice(self, seq):
        return seq[0]


def _otel() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _solo_combat() -> tuple[GameSnapshot, StructuredEncounter]:
    core = CreatureCore(
        name="Hero", description="d", personality="p", fate_sheet=FateSheet(skills={"Fight": 4})
    )
    hero = Character(core=core, char_class="Agent", race="Human", backstory="b")
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    return snap, enc


def test_concede_does_not_emit_a_flavor_rider_even_with_player_action():
    """GUARD (AC#3 decision = rider-less): a concession carrying freeform text must
    NOT emit ``fate.action.flavor_rider`` — that span belongs to proactive actions
    only. Locks the early-return ordering so a future edit cannot quietly attach a
    rider span to a concession."""
    snap, enc = _solo_combat()
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=FateActionPayload(
            request_id="r1",
            action="concede",
            player_action="I throw down my blade and beg for mercy",
        ),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=_FixedRng(),
        _tracer=tracer,
    )

    names = [s.name for s in exporter.get_finished_spans()]
    assert _FLAVOR_SPAN not in names, (
        "a concession carrying freeform text emitted "
        f"{_FLAVOR_SPAN!r} — AC#3 ratifies concede as rider-less; the rider span is "
        f"for proactive actions only. spans: {names}"
    )


def test_concede_freeform_does_not_reach_the_narrator_seam():
    """GUARD (AC#3): concede freeform must not leak into ``narrator_hints``. Concede
    returns before any hint is built, so the narrator seam stays empty of the rider —
    the property that keeps the sanitized player_action surface to one path (AC#2)."""
    snap, enc = _solo_combat()

    dispatch_fate_action(
        payload=FateActionPayload(
            request_id="r1",
            action="concede",
            player_action="I throw down my blade and beg for mercy",
        ),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=_FixedRng(),
    )

    blob = " ".join(enc.narrator_hints).lower()
    assert "blade" not in blob and "mercy" not in blob, (
        "concede freeform text reached narrator_hints — concede is rider-less (AC#3). "
        f"hints: {enc.narrator_hints!r}"
    )


def test_concede_still_earns_its_fate_point_with_player_action():
    """GUARD: the rider-less decision must not break the concede economy. A
    concession still earns its fate point (1 + filled consequences) regardless of
    the inert ``player_action`` — pins that we dropped the rider, not the mechanics."""
    snap, enc = _solo_combat()
    core = snap.find_creature_core("Hero")
    assert core is not None and core.fate_sheet is not None
    before = core.fate_sheet.fate_points

    dispatch_fate_action(
        payload=FateActionPayload(request_id="r1", action="concede", player_action="I yield"),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=_FixedRng(),
    )

    assert core.fate_sheet.fate_points == before + 1, (
        "concede must still earn its fate point (no filled consequences here → +1) "
        f"regardless of the rider-less player_action; got {core.fate_sheet.fate_points}, "
        f"expected {before + 1}"
    )
