"""WWN Warrior — Killing Blow + Veteran's Luck (TDD, Plan 2 Task 6).

Faithful to spec amendment §E: Fray Die is STRUCK (not implemented). Killing
Blow is a passive damage rider (ceil(level / divisor) bonus, pure math + span).
Veteran's Luck is a once-per-scene Instant action, guarded by a scene-scoped
Status (Scratch severity) that the existing ``clear_scratch_on_scene_end`` sweep
clears at scene end.

Span names are asserted LITERALLY (the GM panel is the lie detector).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.wwn import VETERANS_LUCK_USED_MARKER, WwnRulesetModule
from sidequest.game.status import StatusSeverity
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


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _warrior(name: str = "Torvin", level: int = 3) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A scarred veteran.",
        personality="Grim.",
        level=level,
    )


# ---------------------------------------------------------------------------
# Killing Blow — apply_killing_blow
# ---------------------------------------------------------------------------


def test_killing_blow_math_level3():
    """Level 3: bonus = ceil(3 / 2) = 2; total = base + 2."""
    total = _MOD.apply_killing_blow(base_total=5, level=3, cfg=_CFG)
    assert total == 7  # 5 + ceil(3/2) = 5 + 2


def test_killing_blow_math_level1():
    """Level 1: bonus = ceil(1 / 2) = 1; total = base + 1."""
    total = _MOD.apply_killing_blow(base_total=3, level=1, cfg=_CFG)
    assert total == 4  # 3 + ceil(1/2) = 3 + 1


def test_killing_blow_math_level4():
    """Level 4: bonus = ceil(4 / 2) = 2; total = base + 2."""
    total = _MOD.apply_killing_blow(base_total=8, level=4, cfg=_CFG)
    assert total == 10  # 8 + ceil(4/2) = 8 + 2


def test_killing_blow_math_level5():
    """Level 5: bonus = ceil(5 / 2) = 3; total = base + 3."""
    total = _MOD.apply_killing_blow(base_total=6, level=5, cfg=_CFG)
    assert total == 9  # 6 + ceil(5/2) = 6 + 3


def test_killing_blow_shock_path():
    """Killing Blow also applies to Shock: assert the augmented Shock total."""
    shock_base = 2  # base Shock damage
    augmented = _MOD.apply_killing_blow(base_total=shock_base, level=3, cfg=_CFG)
    assert augmented == 4  # 2 + ceil(3/2) = 2 + 2


def test_killing_blow_emits_span_with_correct_fields():
    """Span name is 'wwn.killing_blow'; bonus and total carried as attributes."""
    exporter, tracer = _exporter()
    total = _MOD.apply_killing_blow(base_total=5, level=3, cfg=_CFG, actor="Torvin", _tracer=tracer)
    assert total == 7
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "wwn.killing_blow"
    attrs = dict(spans[0].attributes or {})
    assert attrs["bonus"] == 2
    assert attrs["total"] == 7
    assert attrs["base"] == 5
    assert attrs["level"] == 3
    assert attrs["actor"] == "Torvin"


def test_killing_blow_cfg_guard_raises_on_non_wwn():
    """cfg guard must raise on non-WwnConfig (consistent with other methods)."""
    from sidequest.genre.models.rules import SwnConfig

    swn_cfg = SwnConfig(attribute_map=_AMAP)
    with pytest.raises(ValueError):
        _MOD.apply_killing_blow(base_total=5, level=3, cfg=swn_cfg)


def test_killing_blow_cfg_guard_raises_on_none():
    """cfg guard must raise on None."""
    with pytest.raises(ValueError):
        _MOD.apply_killing_blow(base_total=5, level=3, cfg=None)


# ---------------------------------------------------------------------------
# Veteran's Luck — veterans_luck
# ---------------------------------------------------------------------------

# The used-marker is a Status(text=VETERANS_LUCK_USED_MARKER, severity=Scratch).
# clear_scratch_on_scene_end sweeps Scratch statuses → the flag is auto-cleared
# at scene end by the existing sweep without any new cleanup code.


def test_veterans_luck_first_call_applied():
    """First call in a scene: applied=True, marker set on core.statuses."""
    warrior = _warrior()
    result = _MOD.veterans_luck(warrior, mode="force_hit")
    assert result.applied is True
    assert result.mode == "force_hit"
    # Marker is now in statuses.
    texts = [s.text for s in warrior.statuses]
    assert VETERANS_LUCK_USED_MARKER in texts


def test_veterans_luck_second_call_same_scene_refused():
    """Second call same scene: applied=False (already used this scene)."""
    warrior = _warrior()
    first = _MOD.veterans_luck(warrior, mode="force_hit")
    second = _MOD.veterans_luck(warrior, mode="force_miss")
    assert first.applied is True
    assert second.applied is False
    assert second.reason  # reason set explaining refusal


def test_veterans_luck_marker_is_scratch_severity():
    """The used-marker must have Scratch severity so the scene-end sweep clears it."""
    warrior = _warrior()
    _MOD.veterans_luck(warrior, mode="force_hit")
    markers = [s for s in warrior.statuses if s.text == VETERANS_LUCK_USED_MARKER]
    assert len(markers) == 1
    assert markers[0].severity == StatusSeverity.Scratch


def test_veterans_luck_scene_end_clears_marker():
    """After clear_scratch_on_scene_end, a second call in the new scene is applied=True."""
    from sidequest.game.character import Character
    from sidequest.game.session import GameSnapshot
    from sidequest.server.status_clear import clear_scratch_on_scene_end

    warrior = _warrior()
    char = Character(
        core=warrior, char_class="Warrior", race="Human", backstory="A scarred fighter."
    )
    # Build a minimal snapshot with one character.
    snap = GameSnapshot(characters=[char])

    # First use of the scene.
    first = _MOD.veterans_luck(warrior, mode="force_hit")
    assert first.applied is True

    # Second call same scene is refused.
    second = _MOD.veterans_luck(warrior, mode="force_miss")
    assert second.applied is False

    # Scene ends — sweep Scratch statuses.
    cleared = clear_scratch_on_scene_end(snap, reason="scene_end", turn=1)
    assert cleared >= 1  # at least the marker was cleared

    # Now Veteran's Luck is available again.
    third = _MOD.veterans_luck(warrior, mode="force_miss")
    assert third.applied is True


def test_veterans_luck_emits_span_on_applied():
    """Span 'wwn.veterans_luck' emitted when applied=True."""
    exporter, tracer = _exporter()
    warrior = _warrior()
    _MOD.veterans_luck(warrior, mode="force_hit", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "wwn.veterans_luck"
    attrs = dict(spans[0].attributes or {})
    assert attrs["applied"] is True
    assert attrs["mode"] == "force_hit"
    assert attrs["actor"] == "Torvin"


def test_veterans_luck_emits_span_on_refused():
    """Span 'wwn.veterans_luck' emitted on both applied=True and applied=False calls."""
    exporter, tracer = _exporter()
    warrior = _warrior()
    _MOD.veterans_luck(warrior, mode="force_hit", _tracer=tracer)
    _MOD.veterans_luck(warrior, mode="force_miss", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    names = [s.name for s in spans]
    assert names == ["wwn.veterans_luck", "wwn.veterans_luck"]
    # First applied, second refused.
    assert dict(spans[0].attributes or {})["applied"] is True
    assert dict(spans[1].attributes or {})["applied"] is False


def test_veterans_luck_mode_force_miss():
    """force_miss mode works identically to force_hit."""
    warrior = _warrior()
    result = _MOD.veterans_luck(warrior, mode="force_miss")
    assert result.applied is True
    assert result.mode == "force_miss"


def test_veterans_luck_no_fray_die():
    """Fray Die must not exist on WwnRulesetModule — the spec struck it."""
    assert not hasattr(_MOD, "resolve_fray_die")
    assert not hasattr(_MOD, "fray_die")
