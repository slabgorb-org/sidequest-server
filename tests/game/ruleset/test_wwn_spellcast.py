"""WWN cast spine — resolve_spellcast (economy + defender save + damage), span.

Plan 2 of the WWN magic engine. Covers the SRD §4.2 / spec §C-§D cast spine:
fail-loud refusal (no casts / not prepared / level too high — recorded, not
raised), spend-one-cast, the DEFENDER's own save via inherited save_params,
caster_level x die damage with save-for-half, and the wwn.spell.cast span.

The cast spine is dead-in-dispatch until Plan 3 wires cast_spell; these unit
tests against synthetic fixtures are the lie-detector for the engine math.
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.game.wwn_magic import CastInput, SpellcastingState
from sidequest.genre.models.rules import WwnConfig

_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Spirit",
    "CHARISMA": "Presence",
}
_CFG = WwnConfig(attribute_map=_AMAP)
_MOD = WwnRulesetModule()

# Stats keyed by FLAVOR names (what save_params resolves via attribute_map).
# Mental save = better-of(WISDOM, CHARISMA) → Spirit / Presence.
_DEFENDER_STATS = {
    "Brawn": 10,
    "Reflex": 10,
    "Body": 10,
    "Wits": 10,
    "Spirit": 8,  # mod 0 (8-13 band)
    "Presence": 8,  # mod 0
}


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _caster(level: int = 3, *, prepared, casts_remaining, casts_per_day=2, max_spell_level=2):
    return CreatureCore(
        name="Lyra",
        description="A channeler.",
        personality="Studious.",
        level=level,
        spellcasting=SpellcastingState(
            prepared=list(prepared),
            casts_remaining=casts_remaining,
            casts_per_day=casts_per_day,
            max_spell_level=max_spell_level,
        ),
    )


def _defender(level: int = 1):
    return CreatureCore(
        name="Goblin",
        description="A wretch.",
        personality="Mean.",
        level=level,
    )


def _fixed_rng(value: int) -> random.Random:
    rng = random.Random()
    rng.randint = lambda a, b: value  # type: ignore[method-assign]
    return rng


def _seq_rng(*values: int) -> random.Random:
    """An rng whose ``randint`` returns the given values in order (one per call).

    The cast spine calls randint once for the defender save (when a save fires),
    then once per damage die — letting a test pin the save roll and each die
    independently.
    """
    rng = random.Random()
    queue = list(values)

    def _next(a: int, b: int) -> int:
        return queue.pop(0)

    rng.randint = _next  # type: ignore[method-assign]
    return rng


def _firebolt(*, damage_per_level=True):
    return CastInput(
        id="firebolt",
        level=1,
        save="physical",
        damage_die="1d6",
        damage_per_level=damage_per_level,
    )


# ---------------------------------------------------------------------------
# Refusals — recorded (refused=True), never raised, cast NOT spent.
# ---------------------------------------------------------------------------


def test_refuse_no_casts_remaining():
    caster = _caster(prepared=["firebolt"], casts_remaining=0)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster, spell=_firebolt(), cfg=_CFG, rng=_fixed_rng(10), _tracer=tracer
    )
    assert r.cast is False
    assert r.reason
    assert r.casts_remaining == 0
    assert caster.spellcasting.casts_remaining == 0  # unchanged
    spans = exporter.get_finished_spans()
    assert spans[0].name == "wwn.spell.cast"
    attrs = dict(spans[0].attributes or {})
    assert attrs["refused"] is True


def test_refuse_not_prepared():
    caster = _caster(prepared=["lightning"], casts_remaining=2)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster, spell=_firebolt(), cfg=_CFG, rng=_fixed_rng(10), _tracer=tracer
    )
    assert r.cast is False
    assert r.reason
    assert caster.spellcasting.casts_remaining == 2  # unchanged
    spans = exporter.get_finished_spans()
    assert spans[0].name == "wwn.spell.cast"
    assert dict(spans[0].attributes or {})["refused"] is True


def test_refuse_level_too_high():
    caster = _caster(prepared=["meteor"], casts_remaining=2, max_spell_level=2)
    spell = CastInput(id="meteor", level=5, save=None, damage_die=None, damage_per_level=False)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster, spell=spell, cfg=_CFG, rng=_fixed_rng(10), _tracer=tracer
    )
    assert r.cast is False
    assert r.reason
    assert caster.spellcasting.casts_remaining == 2  # unchanged
    spans = exporter.get_finished_spans()
    assert dict(spans[0].attributes or {})["refused"] is True


# ---------------------------------------------------------------------------
# Success — spend exactly one cast, span fired refused=False.
# ---------------------------------------------------------------------------


def test_success_spends_one_cast_and_emits_span():
    caster = _caster(prepared=["mend"], casts_remaining=2)
    spell = CastInput(id="mend", level=1, save=None, damage_die=None, damage_per_level=False)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster, spell=spell, cfg=_CFG, rng=_fixed_rng(10), _tracer=tracer
    )
    assert r.cast is True
    assert r.spell_id == "mend"
    assert r.casts_remaining == 1
    assert caster.spellcasting.casts_remaining == 1  # spent exactly one
    assert r.save_made is None  # no-save utility spell
    assert r.damage == 0
    spans = exporter.get_finished_spans()
    assert spans[0].name == "wwn.spell.cast"
    attrs = dict(spans[0].attributes or {})
    assert attrs["refused"] is False
    assert attrs["casts_remaining"] == 1


def test_no_defender_save_unresolved_but_cast_succeeds():
    """A save spell with no target_core: cast still spends, save_made=None."""
    caster = _caster(prepared=["firebolt"], casts_remaining=2)
    r = _MOD.resolve_spellcast(
        caster_core=caster,
        spell=_firebolt(damage_per_level=False),
        target_core=None,
        cfg=_CFG,
        rng=_fixed_rng(10),
    )
    assert r.cast is True
    assert r.save_made is None
    assert caster.spellcasting.casts_remaining == 1


# ---------------------------------------------------------------------------
# Defender save — the DEFENDER rolls their OWN save via inherited save_params.
# ---------------------------------------------------------------------------


def test_forces_defender_save_made():
    caster = _caster(level=3, prepared=["charm"], casts_remaining=2)
    defender = _defender(level=1)
    # Mental-default save target for a L1 defender (mod 0): save_base 15 → 15.
    # A roll of 20 (+0) >= 15 → save made.
    r = _MOD.resolve_spellcast(
        caster_core=caster,
        spell=CastInput(
            id="charm", level=1, save="mental", damage_die=None, damage_per_level=False
        ),
        target_core=defender,
        target_stats=_DEFENDER_STATS,
        cfg=_CFG,
        rng=_fixed_rng(20),
    )
    assert r.cast is True
    assert r.save_made is True


def test_forces_defender_save_failed():
    caster = _caster(level=3, prepared=["charm"], casts_remaining=2)
    defender = _defender(level=1)
    # A roll of 2 (+0) < 15 → save failed.
    r = _MOD.resolve_spellcast(
        caster_core=caster,
        spell=CastInput(
            id="charm", level=1, save="mental", damage_die=None, damage_per_level=False
        ),
        target_core=defender,
        target_stats=_DEFENDER_STATS,
        cfg=_CFG,
        rng=_fixed_rng(2),
    )
    assert r.cast is True
    assert r.save_made is False


# ---------------------------------------------------------------------------
# Damage + save-for-half — caster_level x die, halved (floor) on a made save.
# ---------------------------------------------------------------------------


def test_damage_scales_with_caster_level_save_failed_full():
    caster = _caster(level=3, prepared=["firebolt"], casts_remaining=2)
    defender = _defender(level=1)
    # Sequence: save roll = 2 (fails the L1 physical save target 15), then 3
    # damage dice (caster level 3) of 5 each → 15. Save failed → full 15.
    r = _MOD.resolve_spellcast(
        caster_core=caster,
        spell=_firebolt(),
        target_core=defender,
        target_stats=_DEFENDER_STATS,
        cfg=_CFG,
        rng=_seq_rng(2, 5, 5, 5),
    )
    assert r.cast is True
    assert r.save_made is False
    assert r.damage == 15  # 3 x 5, no halving


def test_damage_halved_on_made_save_rounds_down():
    caster = _caster(level=3, prepared=["firebolt"], casts_remaining=2)
    defender = _defender(level=1)
    # Sequence: save roll = 20 → 20 + 0 >= 15 → save MADE. Then 3 dice of 5 each
    # = 15, halved (floor) → 7.
    r = _MOD.resolve_spellcast(
        caster_core=caster,
        spell=_firebolt(),  # physical save
        target_core=defender,
        target_stats=_DEFENDER_STATS,
        cfg=_CFG,
        rng=_seq_rng(20, 5, 5, 5),
    )
    assert r.cast is True
    assert r.save_made is True
    # 3 dice of 5 = 15, halved floor = 7.
    assert r.damage == 7


def test_span_carries_save_and_damage():
    caster = _caster(level=2, prepared=["firebolt"], casts_remaining=2)
    defender = _defender(level=1)
    exporter, tracer = _exporter()
    _MOD.resolve_spellcast(
        caster_core=caster,
        spell=_firebolt(),
        target_core=defender,
        target_stats=_DEFENDER_STATS,
        cfg=_CFG,
        rng=_fixed_rng(2),  # save fails, dice = 2 each → 2 dice = 4
        _tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert spans[0].name == "wwn.spell.cast"
    attrs = dict(spans[0].attributes or {})
    assert attrs["refused"] is False
    assert attrs["save"] == "physical"
    assert attrs["save_made"] is False
    assert str(attrs["damage"]) == "4"


def test_no_save_damage_spell_full_damage_and_span_unresolved_save():
    """Magic-missile analog: a damage spell with no save. Full caster_level x die
    damage (never halved), save_made is None, and the span must NOT claim a
    failed save — the save_made attribute is omitted entirely."""
    caster = _caster(level=3, prepared=["missile"], casts_remaining=2)
    spell = CastInput(id="missile", level=1, save=None, damage_die="1d6", damage_per_level=True)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster,
        spell=spell,
        cfg=_CFG,
        rng=_fixed_rng(4),  # 3 dice (caster level 3) of 4 each → 12
        _tracer=tracer,
    )
    assert r.cast is True
    assert caster.spellcasting.casts_remaining == 1  # decremented
    assert r.save_made is None
    assert r.damage == 12  # full, NOT halved
    spans = exporter.get_finished_spans()
    attrs = dict(spans[0].attributes or {})
    assert attrs["refused"] is False
    # The lie-detector must not read a failed save where none was rolled.
    assert "save_made" not in attrs
    assert str(attrs["damage"]) == "12"


# ---------------------------------------------------------------------------
# 158-53 — the span proves the spend DELTA: before AND after charges.
# AC #2: "the cast-spend emits an OTEL watcher span with before/after remaining
# so the GM panel verifies the decrement fired." The span records casts_remaining
# (AFTER) only; adding casts_before makes the spend self-evident on ONE span.
# Locked on BOTH branches so a refusal can never be misread as a spend.
# ---------------------------------------------------------------------------


def test_span_records_casts_before_on_a_spend():
    """A successful cast must record BOTH the before AND after charge count on
    wwn.spell.cast, so the GM panel proves the spend delta (before=2 after=1)
    from ONE span. The before is captured PRE-decrement. RED until casts_before
    is wired onto the span."""
    caster = _caster(prepared=["mend"], casts_remaining=2)
    spell = CastInput(id="mend", level=1, save=None, damage_die=None, damage_per_level=False)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster, spell=spell, cfg=_CFG, rng=_fixed_rng(10), _tracer=tracer
    )
    assert r.cast is True
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs["refused"] is False
    assert attrs["casts_remaining"] == 1, "the AFTER (post-spend) count"
    assert attrs.get("casts_before") == 2, (
        "the wwn.spell.cast span must record the BEFORE (pre-spend) charge count "
        "so the spend delta 2->1 is self-evident to the GM panel — AC #2; "
        f"got casts_before={attrs.get('casts_before')!r}"
    )


def test_span_records_casts_before_on_a_refusal_no_phantom_spend():
    """A REFUSED cast must ALSO carry casts_before, EQUAL to casts_remaining
    (before==after), proving NO phantom spend occurred. Locks casts_before on the
    refuse branch too, so the panel can never mistake a refusal for a spend."""
    caster = _caster(prepared=["firebolt"], casts_remaining=0)
    exporter, tracer = _exporter()
    r = _MOD.resolve_spellcast(
        caster_core=caster, spell=_firebolt(), cfg=_CFG, rng=_fixed_rng(10), _tracer=tracer
    )
    assert r.cast is False
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs["refused"] is True
    assert attrs["casts_remaining"] == 0
    assert attrs.get("casts_before") == 0, (
        "a refused cast must record casts_before == casts_remaining (0 == 0, no "
        "spend) so the GM panel sees before==after — AC #2 on the refuse branch; "
        f"got casts_before={attrs.get('casts_before')!r}"
    )


# ---------------------------------------------------------------------------
# cfg-type guard — fail loud (raise), consistent with Plan 1 methods.
# ---------------------------------------------------------------------------


def test_bad_cfg_type_raises():
    import pytest

    caster = _caster(prepared=["mend"], casts_remaining=2)
    spell = CastInput(id="mend", level=1, save=None, damage_die=None, damage_per_level=False)
    with pytest.raises(ValueError):
        _MOD.resolve_spellcast(caster_core=caster, spell=spell, cfg=None, rng=_fixed_rng(10))
