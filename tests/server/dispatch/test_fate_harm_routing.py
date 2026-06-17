"""Story 126-1 — the lie-detector test for the ADR-144 Fate binding.

A Fate PC/NPC carries BOTH a Fate sheet (stress + consequences) AND the legacy
unified-model ``core.hp`` (``HpPool``, default 10/10 — ADR-007/ADR-114). When a
SEATED Fate conflict deals harm, SOUL "Bind the Ruleset, Don't Balance It"
requires the harm to ablate the **Fate sheet** and leave ``core.hp`` untouched:
the Fate engine *replaces* the native HP track for what it covers, it does not
layer on top of it. If harm ever leaked into ``core.hp`` the binding would be
half-wired.

These tests assert the invariant (AC-2) AND require a new harm-routing OTEL span
(AC-3) so the GM panel can *verify which track was written* — the routing
decision is the literal lie-detector, not something inferred from per-mark spans.

NEW-SPAN CONTRACT (Dev implements in GREEN):
  ``fate.harm.routed`` — emitted in ``_resolve_attack`` whenever an attack lands
  real harm (shifts >= 1), regardless of absorbed-vs-taken-out. Attributes:
    field  = "harm_routed"
    actor  = the target taking the harm
    by     = the attacker
    track  = "physical" | "mental"   (which stress track the hit was directed at)
    shifts = int (>= 1)
    sink   = "fate_sheet"            (the lie-detector field: NEVER "core_hp")
  Helper ``fate_harm_routed_span(...)`` lives beside the other ``fate.*`` helpers
  in ``sidequest/telemetry/spans/fate.py`` and is registered in ``SPAN_ROUTES``
  (component="fate") so the watcher/GM panel sees it. A MISSED/TIED attack
  (shifts <= 0) applies no harm and MUST NOT emit the span.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_resolution import Opposition
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import (
    dispatch_fate_action,
    run_fate_exchange,
    seal_fate_commit,
)

_HARM_SPAN = "fate.harm.routed"


class _FixedRng:
    """Deterministic stand-in for random.Random: every 4dF face is ``value``."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _npc(name: str, skills: dict[str, int], *, sheet: FateSheet | None = None) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            fate_sheet=sheet if sheet is not None else FateSheet(skills=skills),
        )
    )


def _enc(actors: list[EncounterActor], *, category: str = "combat") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category=category,
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _seal_attack(enc, module, attacker, skill_rating, target, *, skill="Fight"):
    """Seal a proactive attack with a deterministic 4dF=0 roll (mirrors
    tests/server/dispatch/test_fate_conflict.py)."""
    outcome = module.resolve_action(
        skill_rating=skill_rating, opposition=Opposition(value=0, kind="active"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor(attacker),
        action="attack",
        skill=skill,
        target=target,
        ladder_total=outcome.ladder_total,
        dice=outcome.dice,
    )


def _hero_vs_thug(*, thug_sheet: FateSheet | None = None):
    """A seated Fate conflict: Hero (PC, Fight 4) vs Thug (NPC, Athletics 1).
    Hero's sealed attack (ladder 4) beats Thug's rolled defense (1) by 3 shifts.
    Both carry the default ``core.hp`` 10/10 alongside their Fate sheets."""
    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 4})], encounter=enc
    )
    snap.npcs.append(_npc("Thug", {"Athletics": 1}, sheet=thug_sheet))
    return module, enc, snap


# --------------------------------------------------------------------------- #
# AC-2: harm ablates the Fate sheet and leaves core.hp untouched (invariant)   #
# --------------------------------------------------------------------------- #


def test_attack_harm_lands_on_fate_sheet_and_leaves_core_hp_untouched():
    """The marquee invariant: a 3-shift hit checks Thug's stress / fills a
    consequence, and his ``core.hp`` stays pristine at 10/10. No ``state_patch_hp``
    span fires — the native HP delta channel is never engaged on the Fate path."""
    module, enc, snap = _hero_vs_thug()
    thug_core = snap.find_creature_core("Thug")
    assert thug_core.hp.current == 10 and thug_core.hp.max == 10  # baseline

    _seal_attack(enc, module, "Hero", 4, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    # Harm landed on the Fate sheet.
    assert thug_core.fate_sheet.stress["physical"].boxes[1].checked is True
    assert thug_core.fate_sheet.consequences[0].aspect is not None
    # core.hp is UNTOUCHED — the binding replaced the native HP track, didn't layer on it.
    assert thug_core.hp.current == 10
    # And the native HP-delta span never fired.
    names = [s.name for s in exporter.get_finished_spans()]
    assert "state_patch_hp" not in names


def test_taken_out_attack_leaves_core_hp_untouched():
    """Even when capacity is exhausted and the target is TAKEN OUT, the harm
    routed through the Fate sheet — ``core.hp`` is still never decremented."""
    depleted = FateSheet(skills={"Athletics": 1})
    for b in depleted.stress["physical"].boxes:
        b.checked = True
    for c in depleted.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    module, enc, snap = _hero_vs_thug(thug_sheet=depleted)

    _seal_attack(enc, module, "Hero", 4, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    assert enc.find_actor("Thug").withdrawn is True  # taken out
    assert snap.find_creature_core("Thug").hp.current == 10  # but core.hp pristine
    assert "state_patch_hp" not in [s.name for s in exporter.get_finished_spans()]


# --------------------------------------------------------------------------- #
# AC-3: the harm-routing OTEL span (the GM-panel lie-detector) — genuine RED   #
# --------------------------------------------------------------------------- #


def test_attack_emits_fate_harm_routed_span_naming_the_fate_sheet_sink():
    """RED: a ``fate.harm.routed`` span must record the routing decision so the
    GM panel can confirm the hit went to the Fate sheet, not core.hp."""
    module, enc, snap = _hero_vs_thug()
    _seal_attack(enc, module, "Hero", 4, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    spans = exporter.get_finished_spans()
    assert _HARM_SPAN in [s.name for s in spans], (
        "no fate.harm.routed span — the GM panel cannot verify which track absorbed the harm"
    )
    harm = next(s for s in spans if s.name == _HARM_SPAN)
    attrs = harm.attributes or {}
    assert attrs.get("sink") == "fate_sheet"  # the lie-detector field — NEVER "core_hp"
    assert attrs.get("track") == "physical"
    assert attrs.get("actor") == "Thug"
    assert attrs.get("by") == "Hero"
    assert attrs.get("shifts") == 3


def test_taken_out_attack_still_emits_harm_routed_to_fate_sheet():
    """RED: harm that exceeds capacity (taken out) is STILL routed to the Fate
    sheet — the span fires with sink=fate_sheet even though no box absorbed it."""
    depleted = FateSheet(skills={"Athletics": 1})
    for b in depleted.stress["physical"].boxes:
        b.checked = True
    for c in depleted.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    module, enc, snap = _hero_vs_thug(thug_sheet=depleted)

    _seal_attack(enc, module, "Hero", 4, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    spans = exporter.get_finished_spans()
    assert _HARM_SPAN in [s.name for s in spans]
    harm = next(s for s in spans if s.name == _HARM_SPAN)
    assert (harm.attributes or {}).get("sink") == "fate_sheet"
    assert (harm.attributes or {}).get("shifts") == 3


def test_missed_attack_emits_no_harm_routed_span():
    """A missed attack (shifts <= 0) applies no harm, so it must NOT emit the
    routing span — the span gates on real harm, not on every attack. Guards
    against over-emission once the span exists."""
    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 0})], encounter=enc
    )
    # Thug defends with Athletics 3 vs Hero's ladder 0 → shifts -3 (clean miss).
    snap.npcs.append(_npc("Thug", {"Athletics": 3}))
    _seal_attack(enc, module, "Hero", 0, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    thug_core = snap.find_creature_core("Thug")
    assert all(not b.checked for b in thug_core.fate_sheet.stress["physical"].boxes)  # no harm
    assert thug_core.hp.current == 10
    assert _HARM_SPAN not in [s.name for s in exporter.get_finished_spans()]


# --------------------------------------------------------------------------- #
# AC-1 + AC-3 wiring: reachable from the production dispatch entry              #
# (dispatch_fate_action is what FateActionHandler calls — fixture-driven        #
#  behavior, NOT a source-text grep)                                           #
# --------------------------------------------------------------------------- #


def test_dispatch_fate_action_routes_harm_to_fate_sheet_end_to_end():
    """RED + wiring: drive the real dispatch entry. A single PC's sealed attack
    closes the barrier, fires run_fate_exchange, and the harm must (a) land on the
    Fate sheet, (b) leave core.hp untouched, (c) emit the fate.harm.routed span,
    alongside the fate.exchange.* spine — proving the lie-detector is reachable
    from the live conflict dispatch, not just the engine in isolation."""
    module, enc, snap = _hero_vs_thug()
    exporter, tracer = _otel()

    result = dispatch_fate_action(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug"),
        actor_name="Hero",
        encounter=enc,
        ruleset=module,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    # The barrier closed (single PC) and the exchange ran.
    assert result.commitment_pending is False
    assert result.exchange is not None

    thug_core = snap.find_creature_core("Thug")
    # Harm landed on the Fate sheet; core.hp untouched.
    stress_marked = any(b.checked for b in thug_core.fate_sheet.stress["physical"].boxes)
    conseq_filled = any(c.aspect is not None for c in thug_core.fate_sheet.consequences)
    assert stress_marked or conseq_filled
    assert thug_core.hp.current == 10

    names = [s.name for s in exporter.get_finished_spans()]
    # The fate.* spine fired AND the harm-routing lie-detector span is present.
    assert "fate.exchange.committed" in names
    assert "fate.exchange.resolved" in names
    assert _HARM_SPAN in names
    assert "state_patch_hp" not in names
