"""Tests for WWN Effort engine on WwnRulesetModule.

Mirrors the InMemorySpanExporter harness from test_cwn_shock.py.
Covers: commit/over-commit, reclaim_effort (maintained), reclaim_scene_effort,
reclaim_day_and_refresh — all per WwnRulesetModule spec (Task 4).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.game.wwn_magic import EffortCommitment, EffortPool, SpellcastingState
from sidequest.genre.models.rules import WwnConfig

_MOD = WwnRulesetModule()


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _core(
    *, effort: dict[str, EffortPool], spellcasting: SpellcastingState | None = None
) -> CreatureCore:
    """Build a minimal CreatureCore with seeded effort pools."""
    return CreatureCore(
        name="Testor",
        description="A test mage",
        personality="methodical",
        hp=HpPool(current=10, max=10, base_max=10),
        effort=effort,
        spellcasting=spellcasting,
    )


# ---------------------------------------------------------------------------
# commit_effort
# ---------------------------------------------------------------------------


class TestCommitEffort:
    def test_commit_decrements_available(self):
        pool = EffortPool(source="high_mage", max=3)
        core = _core(effort={"high_mage": pool})
        result = _MOD.commit_effort(core=core, source="high_mage", points=2, duration="scene")
        assert result.applied is True
        assert result.available == 1  # 3 - 2
        assert core.effort["high_mage"].available == 1

    def test_commit_emits_span(self):
        pool = EffortPool(source="high_mage", max=3)
        core = _core(effort={"high_mage": pool})
        exporter, tracer = _exporter()
        _MOD.commit_effort(
            core=core, source="high_mage", points=1, duration="maintained", _tracer=tracer
        )
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "wwn.effort.commit"

    def test_commit_span_carries_applied_true(self):
        pool = EffortPool(source="high_mage", max=3)
        core = _core(effort={"high_mage": pool})
        exporter, tracer = _exporter()
        _MOD.commit_effort(
            core=core, source="high_mage", points=1, duration="scene", _tracer=tracer
        )
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        assert attrs["applied"] is True
        assert attrs["source"] == "high_mage"

    def test_over_commit_refused_applied_false(self):
        pool = EffortPool(source="high_mage", max=2)
        core = _core(effort={"high_mage": pool})
        result = _MOD.commit_effort(core=core, source="high_mage", points=3, duration="scene")
        assert result.applied is False
        # Pool must be UNCHANGED
        assert core.effort["high_mage"].available == 2
        assert core.effort["high_mage"].committed == 0

    def test_over_commit_emits_span_with_applied_false(self):
        """Fail-loud-but-recorded: span fires even on refusal."""
        pool = EffortPool(source="high_mage", max=1)
        core = _core(effort={"high_mage": pool})
        exporter, tracer = _exporter()
        _MOD.commit_effort(core=core, source="high_mage", points=5, duration="day", _tracer=tracer)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "wwn.effort.commit"
        attrs = dict(spans[0].attributes or {})
        assert attrs["applied"] is False

    def test_over_commit_result_has_reason(self):
        pool = EffortPool(source="high_mage", max=2)
        core = _core(effort={"high_mage": pool})
        result = _MOD.commit_effort(core=core, source="high_mage", points=3, duration="scene")
        assert result.reason != ""

    def test_missing_source_raises_value_error(self):
        core = _core(effort={})
        with pytest.raises(ValueError, match="no .* Effort pool"):
            _MOD.commit_effort(core=core, source="vowed", points=1, duration="scene")

    def test_commit_records_commitment_on_pool(self):
        pool = EffortPool(source="high_mage", max=3)
        core = _core(effort={"high_mage": pool})
        _MOD.commit_effort(
            core=core, source="high_mage", points=2, duration="day", label="Arcanist's Eye"
        )
        assert len(core.effort["high_mage"].commitments) == 1
        assert core.effort["high_mage"].commitments[0].duration == "day"
        assert core.effort["high_mage"].commitments[0].label == "Arcanist's Eye"


# ---------------------------------------------------------------------------
# reclaim_effort (maintained — explicit release of maintained commitments)
# ---------------------------------------------------------------------------


class TestReclaimEffort:
    def test_reclaim_maintained_returns_points_immediately(self):
        pool = EffortPool(
            source="high_mage",
            max=3,
            commitments=[
                EffortCommitment(points=2, duration="maintained", label="Night Eye"),
            ],
        )
        core = _core(effort={"high_mage": pool})
        result = _MOD.reclaim_effort(core=core, source="high_mage", trigger="maintained")
        assert result.applied is True
        assert core.effort["high_mage"].available == 3
        assert len(core.effort["high_mage"].commitments) == 0

    def test_reclaim_maintained_emits_span(self):
        pool = EffortPool(
            source="high_mage",
            max=3,
            commitments=[EffortCommitment(points=1, duration="maintained")],
        )
        core = _core(effort={"high_mage": pool})
        exporter, tracer = _exporter()
        _MOD.reclaim_effort(core=core, source="high_mage", trigger="maintained", _tracer=tracer)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "wwn.effort.reclaim"

    def test_reclaim_maintained_span_carries_points_returned(self):
        pool = EffortPool(
            source="high_mage",
            max=4,
            commitments=[
                EffortCommitment(points=2, duration="maintained"),
                EffortCommitment(points=1, duration="scene"),  # not touched
            ],
        )
        core = _core(effort={"high_mage": pool})
        exporter, tracer = _exporter()
        _MOD.reclaim_effort(core=core, source="high_mage", trigger="maintained", _tracer=tracer)
        attrs = dict(exporter.get_finished_spans()[0].attributes or {})
        # 2 maintained points returned; scene commitment left intact
        assert attrs["points"] == 2
        assert core.effort["high_mage"].committed == 1  # scene commitment remains

    def test_reclaim_no_matching_commitments_no_span(self):
        """Nothing to reclaim → no span emitted."""
        pool = EffortPool(
            source="high_mage",
            max=3,
            commitments=[EffortCommitment(points=1, duration="scene")],
        )
        core = _core(effort={"high_mage": pool})
        exporter, tracer = _exporter()
        result = _MOD.reclaim_effort(
            core=core, source="high_mage", trigger="maintained", _tracer=tracer
        )
        assert result.applied is False
        assert len(exporter.get_finished_spans()) == 0

    def test_missing_source_raises_value_error(self):
        core = _core(effort={})
        with pytest.raises(ValueError, match="no .* Effort pool"):
            _MOD.reclaim_effort(core=core, source="vowed", trigger="maintained")


# ---------------------------------------------------------------------------
# reclaim_scene_effort
# ---------------------------------------------------------------------------


class TestReclaimSceneEffort:
    def test_drops_only_scene_commitments(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[
                    EffortCommitment(points=1, duration="scene"),
                    EffortCommitment(points=1, duration="maintained"),
                    EffortCommitment(points=1, duration="day"),
                ],
            )
        }
        core = _core(effort=pools)
        _MOD.reclaim_scene_effort(core=core)
        remaining = core.effort["high_mage"].commitments
        # scene gone; maintained + day remain
        assert len(remaining) == 2
        durations = {c.duration for c in remaining}
        assert "scene" not in durations

    def test_emits_one_span_per_pool_touched(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[EffortCommitment(points=2, duration="scene")],
            ),
            "vowed": EffortPool(
                source="vowed",
                max=2,
                commitments=[EffortCommitment(points=1, duration="scene")],
            ),
        }
        core = _core(effort=pools)
        exporter, tracer = _exporter()
        _MOD.reclaim_scene_effort(core=core, _tracer=tracer)
        spans = exporter.get_finished_spans()
        # One span per pool that had scene commitments
        assert len(spans) == 2
        assert all(s.name == "wwn.effort.reclaim" for s in spans)

    def test_no_span_for_pools_with_no_scene_commitments(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[EffortCommitment(points=1, duration="maintained")],
            ),
        }
        core = _core(effort=pools)
        exporter, tracer = _exporter()
        _MOD.reclaim_scene_effort(core=core, _tracer=tracer)
        # Pool with only maintained commitment → nothing to reclaim → no span
        assert len(exporter.get_finished_spans()) == 0

    def test_all_scene_returned_to_available(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=3,
                commitments=[
                    EffortCommitment(points=2, duration="scene"),
                ],
            )
        }
        core = _core(effort=pools)
        _MOD.reclaim_scene_effort(core=core)
        assert core.effort["high_mage"].available == 3


# ---------------------------------------------------------------------------
# reclaim_day_and_refresh
# ---------------------------------------------------------------------------


class TestReclaimDayAndRefresh:
    def test_drops_day_and_scene_commitments(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[
                    EffortCommitment(points=1, duration="day"),
                    EffortCommitment(points=1, duration="scene"),
                    EffortCommitment(points=1, duration="maintained"),
                ],
            )
        }
        core = _core(effort=pools)
        _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=WwnConfig())
        remaining = core.effort["high_mage"].commitments
        # only maintained survives
        assert len(remaining) == 1
        assert remaining[0].duration == "maintained"

    def test_refreshes_casts_remaining(self):
        sc = SpellcastingState(casts_remaining=0, casts_per_day=3, max_spell_level=2)
        core = _core(effort={}, spellcasting=sc)
        _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=WwnConfig())
        assert core.spellcasting.casts_remaining == 3

    def test_no_spellcasting_no_crash(self):
        """Guard: spellcasting=None must not raise."""
        core = _core(effort={})
        # Must not raise
        _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=WwnConfig())
        assert core.spellcasting is None

    def test_day_reclaim_requires_comfort_blocks_day(self):
        """When day_reclaim_requires_comfort=True and comfortable=False, day
        commitments are NOT dropped."""
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[
                    EffortCommitment(points=1, duration="day"),
                    EffortCommitment(points=1, duration="scene"),
                ],
            )
        }
        cfg = WwnConfig()
        # WwnConfig.magic.day_reclaim_requires_comfort defaults True
        core = _core(effort=pools)
        _MOD.reclaim_day_and_refresh(core=core, comfortable=False, cfg=cfg)
        remaining = core.effort["high_mage"].commitments
        # scene cleared; day stays because not comfortable
        assert any(c.duration == "day" for c in remaining)
        assert not any(c.duration == "scene" for c in remaining)

    def test_day_reclaim_no_comfort_requirement_drops_day_even_uncomfortable(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[
                    EffortCommitment(points=1, duration="day"),
                ],
            )
        }
        core = _core(effort=pools)
        # We need day_reclaim_requires_comfort=False; use a fresh config
        # WwnConfig is a Pydantic model — build it with override
        cfg2 = WwnConfig.model_validate({"magic": {"day_reclaim_requires_comfort": False}})
        _MOD.reclaim_day_and_refresh(core=core, comfortable=False, cfg=cfg2)
        assert core.effort["high_mage"].available == 4

    def test_emits_reclaim_span_per_pool_touched(self):
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=3,
                commitments=[EffortCommitment(points=2, duration="day")],
            )
        }
        core = _core(effort=pools)
        exporter, tracer = _exporter()
        _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=WwnConfig(), _tracer=tracer)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "wwn.effort.reclaim"

    def test_comfortable_scene_only_pool_emits_trigger_scene(self):
        """A comfortable rest sweeps a pool holding ONLY scene Effort: the span
        must read trigger='scene' (the duration actually reclaimed), NOT 'day'.
        The GM panel is the lie detector — intent must not leak into the reading.
        """
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=3,
                commitments=[EffortCommitment(points=2, duration="scene")],
            )
        }
        core = _core(effort=pools)
        exporter, tracer = _exporter()
        _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=WwnConfig(), _tracer=tracer)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert dict(spans[0].attributes or {})["trigger"] == "scene"

    def test_comfortable_day_pool_emits_trigger_day(self):
        """A pool that actually held a day commitment reads trigger='day'."""
        pools = {
            "high_mage": EffortPool(
                source="high_mage",
                max=4,
                commitments=[
                    EffortCommitment(points=1, duration="day"),
                    EffortCommitment(points=1, duration="scene"),
                ],
            )
        }
        core = _core(effort=pools)
        exporter, tracer = _exporter()
        _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=WwnConfig(), _tracer=tracer)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert dict(spans[0].attributes or {})["trigger"] == "day"

    def test_requires_wwn_config(self):
        from sidequest.genre.models.rules import SwnConfig

        core = _core(effort={})
        with pytest.raises(ValueError, match="WwnConfig"):
            _MOD.reclaim_day_and_refresh(core=core, comfortable=True, cfg=SwnConfig())
