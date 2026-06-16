"""Story 118-10 (ADR-144 F3d-pre) — dispatch threads ``invoke_mode`` and the
``player_action`` flavor rider through ``dispatch_fate_action``.

Two wire gaps this story closes, both pinned here behaviorally (OTEL spans + the
roll itself — never a source grep, per CLAUDE.md "No Source-Text Wiring Tests"):

(a) ``invoke_mode`` — ``dispatch_fate_action`` calls ``ruleset.invoke_aspect``
    with a HARDCODED ``mode="bonus"`` (fate_conflict.py). The ruleset and the
    ``fate.aspect.invoked`` span have carried ``mode`` since F1b; the dispatch
    just never passes the wire value. The lie-detector is the span's ``mode``
    attribute: drive a reroll-mode invoke and assert the span says ``reroll``.

(b) ``player_action`` — a freeform RP rider clicked alongside a Fate action tile
    ("chandelier swing"). Mirrors the WN combat flavor rider (story 108-5), so it
    gets the same lie-detector shape: a ``fate.action.flavor_rider`` span with
    ``attached=true, affected_mechanics=false`` proving the typed text was
    attached as narrator context WITHOUT touching the 4dF roll. The behavioral
    truth the span asserts — mechanical inertness — is pinned by a same-roll
    comparison with/without the rider.

RED today:
  * ``…invoke_mode_reroll_reaches_the_span`` — dispatch hardcodes bonus.
  * ``…player_action_emits_fate_flavor_rider_span`` — the span does not exist.
  * ``…flavor_rider_span_marks_mechanics_unaffected`` — attrs do not exist.

Green guards (must STAY green — they pin the inertness/back-compat the wire claims):
  * ``…default_invoke_mode_is_bonus_on_the_span`` — omitting the field = +2 today.
  * ``…player_action_is_mechanically_inert`` — same dice with/without the rider.
  * ``…absent_player_action_emits_no_flavor_rider_span`` — no false-positive span.

Mirrors the fixture doubles in ``tests/server/dispatch/test_fate_dispatch_routing.py``.
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
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import dispatch_fate_action

_FLAVOR_SPAN = "fate.action.flavor_rider"
_INVOKE_SPAN = "fate.aspect.invoked"
_RIDER = "I swing from the chandelier and fire"


class _FixedRng:
    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _otel() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _hero_with_invokable_aspect() -> Character:
    """Hero with a one-free-invoke aspect — the F1b economy idiom so a pre-roll
    invoke can fire (mirrors test_fate_dispatch_routing's invoke fixture)."""
    core = CreatureCore(
        name="Hero",
        description="d",
        personality="p",
        fate_sheet=FateSheet(
            fate_points=2,
            skills={"Fight": 4},
            aspects=[Aspect(text="High Ground", kind="situation", free_invokes=1)],
        ),
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _solo_combat(hero: Character | None = None) -> tuple[GameSnapshot, StructuredEncounter]:
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
    if hero is None:
        core = CreatureCore(
            name="Hero", description="d", personality="p", fate_sheet=FateSheet(skills={"Fight": 4})
        )
        hero = Character(core=core, char_class="Agent", race="Human", backstory="b")
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_depleted_thug())
    return snap, enc


# ---------------------------------------------------------------------------
# (a) invoke_mode threading
# ---------------------------------------------------------------------------


def test_invoke_mode_reroll_reaches_the_span():
    """RED: a payload that declares ``invoke_mode='reroll'`` must reach
    ``ruleset.invoke_aspect`` as ``mode='reroll'`` — observable on the
    ``fate.aspect.invoked`` span's ``mode`` attribute. ``dispatch_fate_action``
    hardcodes ``mode='bonus'`` today, so the span reports 'bonus' and this fails.
    This is the wire that unblocks the reroll half of F3d (118-6)."""
    snap, enc = _solo_combat(_hero_with_invokable_aspect())
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(
        request_id="r1",
        action="attack",
        skill="Fight",
        target="Thug",
        invoke_aspect="High Ground",
        invoke_mode="reroll",
    )
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    invoked = [s for s in exporter.get_finished_spans() if s.name == _INVOKE_SPAN]
    assert invoked, (
        f"the pre-roll invoke did not emit {_INVOKE_SPAN!r}; spans: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
    mode = dict(invoked[0].attributes or {}).get("mode")
    assert mode == "reroll", (
        "dispatch_fate_action dropped the wire ``invoke_mode='reroll'`` and invoked "
        f"the aspect as {mode!r} instead — the dispatch still hardcodes mode='bonus' "
        "so the reroll half of F3d is unreachable from the wire (story 118-10)"
    )


def test_default_invoke_mode_is_bonus_on_the_span():
    """GREEN GUARD: omitting ``invoke_mode`` keeps the +2 behavior — the span
    reports ``mode='bonus'``. Pins backward-compat: existing clients that never
    set the field must be unaffected by the new threading."""
    snap, enc = _solo_combat(_hero_with_invokable_aspect())
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(
        request_id="r1",
        action="attack",
        skill="Fight",
        target="Thug",
        invoke_aspect="High Ground",  # no invoke_mode → defaults to 'bonus'
    )
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    invoked = [s for s in exporter.get_finished_spans() if s.name == _INVOKE_SPAN]
    assert invoked, "precondition: the default-mode invoke must still fire the span"
    assert dict(invoked[0].attributes or {}).get("mode") == "bonus"


# ---------------------------------------------------------------------------
# (b) player_action flavor rider
# ---------------------------------------------------------------------------


def test_player_action_emits_fate_flavor_rider_span():
    """RED: a Fate action carrying a freeform ``player_action`` rider must emit
    ``fate.action.flavor_rider`` — the GM-panel lie-detector proving the typed
    "chandelier swing" was attached as narrator context (CLAUDE.md OTEL
    Observability Principle; mirrors story 108-5's ``wwn.action.flavor_rider``).
    The span does not exist yet, so this fails."""
    snap, enc = _solo_combat()
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(
        request_id="r1",
        action="attack",
        skill="Fight",
        target="Thug",
        player_action=_RIDER,
    )
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    rider = [s for s in exporter.get_finished_spans() if s.name == _FLAVOR_SPAN]
    assert rider, (
        f"a Fate action carrying a flavor rider did not emit {_FLAVOR_SPAN!r} — "
        "without it the GM panel cannot distinguish an inert RP affordance from a "
        "covert freeform-adjudication regression (the El Dorado failure the "
        "mechanical-scaffold architecture exists to prevent). spans: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(rider[0].attributes or {})
    assert attrs.get("attached") is True, (
        f"flavor_rider span must record attached=True when text rides the action; "
        f"got {attrs!r}"
    )


def test_flavor_rider_span_marks_mechanics_unaffected():
    """RED: the rider span must attest ``affected_mechanics=false`` — the explicit
    record that the chandelier text colored prose without touching the 4dF roll
    (mirrors 108-5 AC5). The attr does not exist yet, so this fails."""
    snap, enc = _solo_combat()
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(
        request_id="r1", action="attack", skill="Fight", target="Thug", player_action=_RIDER
    )
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    rider = [s for s in exporter.get_finished_spans() if s.name == _FLAVOR_SPAN]
    assert rider, f"precondition: {_FLAVOR_SPAN!r} must fire when a rider is attached"
    attrs = dict(rider[0].attributes or {})
    assert attrs.get("affected_mechanics") is False, (
        "flavor_rider span must record affected_mechanics=False — the lie-detector "
        "that the rider colored prose without feeding the 4dF roll a bonus or a DC. "
        f"got {attrs!r}"
    )


def test_absent_player_action_emits_no_flavor_rider_span():
    """GUARD: the rider span fires ONLY when text is actually attached. A Fate
    action with no freeform text must NOT emit ``fate.action.flavor_rider`` — a
    span on an empty rider is a false positive that pollutes the GM-panel signal
    (and would let a fabricated rider hide)."""
    snap, enc = _solo_combat()
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    rider = [s for s in exporter.get_finished_spans() if s.name == _FLAVOR_SPAN]
    assert not rider, (
        "a Fate action with NO attached freeform text emitted a "
        f"{_FLAVOR_SPAN!r} span — the rider span must fire only when text is "
        "actually attached. spans: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )


def test_player_action_is_mechanically_inert():
    """GREEN GUARD: the flavor rider is mechanically inert. With the 4dF roll
    pinned to a fixed RNG, the acting PC's own roll (dice + ladder total) is
    byte-for-byte identical whether the player attached a flourish or typed
    nothing. If the rider ever fed a bonus/DC into resolution, the two rolls would
    diverge and this guard would fail — the behavioral truth ``affected_mechanics=
    false`` asserts."""
    snap_a, enc_a = _solo_combat()
    plain = dispatch_fate_action(
        payload=FateActionPayload(
            request_id="r1", action="attack", skill="Fight", target="Thug"
        ),
        actor_name="Hero",
        encounter=enc_a,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap_a,
        rng=_FixedRng(0),
    )

    snap_b, enc_b = _solo_combat()
    ridden = dispatch_fate_action(
        payload=FateActionPayload(
            request_id="r1", action="attack", skill="Fight", target="Thug", player_action=_RIDER
        ),
        actor_name="Hero",
        encounter=enc_b,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap_b,
        rng=_FixedRng(0),
    )

    assert plain.action_roll is not None and ridden.action_roll is not None, (
        "precondition: a proactive attack must produce the acting PC's 4dF roll"
    )
    assert ridden.action_roll.dice == plain.action_roll.dice, (
        "the freeform rider changed the 4dF dice "
        f"(plain={plain.action_roll.dice}, ridden={ridden.action_roll.dice}) — the "
        "rider must be mechanically inert (same roll with or without the chandelier "
        "swing; story 118-10 / 108-5 AC3)"
    )
    assert ridden.action_roll.ladder_total == plain.action_roll.ladder_total, (
        "the freeform rider changed the ladder total "
        f"(plain={plain.action_roll.ladder_total}, ridden={ridden.action_roll.ladder_total}) "
        "— the rider must not feed the resolution a bonus"
    )
