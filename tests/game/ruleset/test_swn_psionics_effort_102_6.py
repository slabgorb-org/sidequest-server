"""Story 102-6 RED — Psionics Effort on the SWN family base + {ruleset}-namespaced spans.

THE CONTRACT (sprint/context/context-story-102-6.md, AC1 + "Span set is named in
the story"):

  * Psionics is signature SWN crunch. Today the Effort economy
    (commit/reclaim against a small pool, System Strain as the overcommit cost)
    lives ONLY on ``WwnRulesetModule`` (``sidequest/game/ruleset/wwn.py``).
    ``SwnRulesetModule`` has NO ``commit_effort`` — so a space_opera (swn) psychic
    cannot commit Effort at all.

  * The context doc's design call is explicit: "shared core in the family base,
    per-module catalogs." ``WwnRulesetModule`` extends ``SwnRulesetModule``, so the
    Effort engine must live on the SWN base; WWN then inherits it unchanged.

  * The span set the story NAMES is parameterized by ruleset slug:
    ``{ruleset}.effort.commit`` / ``{ruleset}.effort.reclaim``. For a swn-bound
    module that is ``swn.effort.commit``; for wwn it stays ``wwn.effort.commit``
    (BACKWARD COMPATIBLE — the existing constant is just the slug-substituted
    form). Today the wwn span name is HARDCODED ``"wwn.effort.commit"``
    (``telemetry/spans/wwn.py:233``), so an swn commit could never read
    ``swn.effort.commit``.

These tests drive the REAL module resolved through ``get_ruleset_module`` and
assert the OBSERVABLE span name + pool state — never the internal class layout
(per CLAUDE.md "No Source-Text Wiring Tests"). Where a not-yet-built surface is
referenced (``SwnRulesetModule.commit_effort``) it is reached at call time so the
module always collects and the failure is a crisp AttributeError/assertion.

AC1 edge ("zero free Effort → refused OR Strain-bought per the rules — loudly,
never silent success") is pinned as a disjunction so it is robust to whichever
rule Dev implements, while forbidding the one outcome the SOUL bans: a silent
success.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.wwn_magic import EffortCommitment, EffortPool

# Source key for the shared SWN psionic Effort pool (SWN's psionics use ONE
# Effort pool across disciplines; the engine keys ``core.effort`` by source).
_PSIONIC_SOURCE = "psionic"


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _psychic_core(*, max_effort: int = 3, commitments: list[EffortCommitment] | None = None):
    """A minimal CreatureCore carrying a seeded psionic Effort pool."""
    return CreatureCore(
        name="Sael",
        description="A precog of the Aureate Span",
        personality="watchful",
        hp=HpPool(current=10, max=10, base_max=10),
        effort={
            _PSIONIC_SOURCE: EffortPool(
                source=_PSIONIC_SOURCE,
                max=max_effort,
                commitments=commitments or [],
            )
        },
    )


# ===========================================================================
# Effort engine exists on the swn-resolved module (lifted to the family base)
# ===========================================================================


class TestSwnEffortEngine:
    def test_swn_module_can_commit_effort(self):
        """get_ruleset_module('swn') must expose commit_effort — the Effort
        engine is shared family-base behavior, not wwn-only."""
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        result = module.commit_effort(core=core, source=_PSIONIC_SOURCE, points=2, duration="scene")
        assert result.applied is True
        assert core.effort[_PSIONIC_SOURCE].available == 1  # 3 - 2

    def test_swn_commit_records_commitment(self):
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        module.commit_effort(
            core=core,
            source=_PSIONIC_SOURCE,
            points=1,
            duration="scene",
            label="Telepathic Contact",
        )
        pool = core.effort[_PSIONIC_SOURCE]
        assert len(pool.commitments) == 1
        assert pool.commitments[0].label == "Telepathic Contact"


# ===========================================================================
# {ruleset}.effort.commit — span name is namespaced by the resolved slug
# ===========================================================================


class TestEffortCommitSpanNamespacing:
    def test_swn_commit_emits_swn_namespaced_span(self):
        """A swn-resolved Effort commit must read ``swn.effort.commit`` — the
        ``{ruleset}`` contract. Today the span name is hardcoded ``wwn.*``."""
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        exporter, tracer = _exporter()
        module.commit_effort(
            core=core, source=_PSIONIC_SOURCE, points=1, duration="scene", _tracer=tracer
        )
        names = [s.name for s in exporter.get_finished_spans()]
        assert "swn.effort.commit" in names, (
            f"swn psionic Effort commit must emit 'swn.effort.commit'; got {names}"
        )

    def test_wwn_commit_still_emits_wwn_namespaced_span(self):
        """BACKWARD-COMPAT GUARD: lifting Effort to the family base must NOT
        rename the wwn span — a wwn-resolved commit still reads
        ``wwn.effort.commit``."""
        module = get_ruleset_module("wwn")
        core = _psychic_core(max_effort=3)
        # WWN keys effort by class source; reuse the same pool under a wwn key.
        core.effort["high_mage"] = EffortPool(source="high_mage", max=3)
        exporter, tracer = _exporter()
        module.commit_effort(
            core=core, source="high_mage", points=1, duration="scene", _tracer=tracer
        )
        names = [s.name for s in exporter.get_finished_spans()]
        assert "wwn.effort.commit" in names, (
            f"wwn Effort commit must remain 'wwn.effort.commit'; got {names}"
        )

    def test_swn_commit_span_carries_source_and_applied(self):
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        exporter, tracer = _exporter()
        module.commit_effort(
            core=core, source=_PSIONIC_SOURCE, points=1, duration="scene", _tracer=tracer
        )
        commit = [s for s in exporter.get_finished_spans() if s.name == "swn.effort.commit"]
        assert len(commit) == 1
        attrs = dict(commit[0].attributes or {})
        assert attrs["source"] == _PSIONIC_SOURCE
        assert attrs["applied"] is True


# ===========================================================================
# {ruleset}.effort.reclaim — namespaced reclaim
# ===========================================================================


class TestEffortReclaimSpanNamespacing:
    def test_swn_scene_reclaim_emits_swn_namespaced_span(self):
        module = get_ruleset_module("swn")
        core = _psychic_core(
            max_effort=3,
            commitments=[EffortCommitment(points=2, duration="scene", label="Foresight")],
        )
        exporter, tracer = _exporter()
        module.reclaim_scene_effort(core=core, _tracer=tracer)
        names = [s.name for s in exporter.get_finished_spans()]
        assert "swn.effort.reclaim" in names, (
            f"swn scene reclaim must emit 'swn.effort.reclaim'; got {names}"
        )
        # Pool restored.
        assert core.effort[_PSIONIC_SOURCE].available == 3


# ===========================================================================
# AC1 edge — zero free Effort is LOUD, never a silent success (SOUL: "The Test")
# Python rule #1 (no silent swallow) + the No Silent Fallbacks critical.
# ===========================================================================


class TestZeroEffortIsLoud:
    def test_activation_with_no_free_effort_is_not_a_silent_success(self):
        """Pool fully committed → an activation request must NOT quietly
        succeed. Either it is REFUSED (applied=False, reason set, pool
        unchanged) OR it is bought with System Strain (strain rises). The one
        forbidden outcome is silent success — effort/strain both unchanged yet
        ``applied`` True."""
        module = get_ruleset_module("swn")
        core = _psychic_core(
            max_effort=2,
            commitments=[EffortCommitment(points=2, duration="scene")],
        )
        assert core.effort[_PSIONIC_SOURCE].available == 0
        strain_before = core.system_strain.current if core.system_strain else 0

        result = module.commit_effort(core=core, source=_PSIONIC_SOURCE, points=1, duration="scene")

        refused = result.applied is False and result.reason != ""
        strain_after = core.system_strain.current if core.system_strain else 0
        strain_bought = strain_after > strain_before

        assert refused or strain_bought, (
            "zero free Effort must surface LOUDLY — a refusal (applied=False + "
            "reason) or a Strain purchase. A silent success is the exact "
            "Illusionism the SOUL bans."
        )
        if refused:
            # Refusal must not have mutated the pool.
            assert core.effort[_PSIONIC_SOURCE].committed == 2

    def test_overcommit_span_records_the_refusal(self):
        """Fail-loud-but-recorded: even a refused commit must leave a span so
        the GM panel sees it (OTEL is the lie detector)."""
        module = get_ruleset_module("swn")
        core = _psychic_core(
            max_effort=1,
            commitments=[EffortCommitment(points=1, duration="scene")],
        )
        exporter, tracer = _exporter()
        result = module.commit_effort(
            core=core, source=_PSIONIC_SOURCE, points=3, duration="day", _tracer=tracer
        )
        if result.applied is False:
            commit = [s for s in exporter.get_finished_spans() if s.name == "swn.effort.commit"]
            assert len(commit) == 1
            assert dict(commit[0].attributes or {})["applied"] is False

    def test_missing_pool_raises_not_silently_noops(self):
        """Requesting Effort from a source with no pool must raise — never a
        silent no-op (No Silent Fallbacks)."""
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        with pytest.raises(ValueError):
            module.commit_effort(
                core=core, source="nonexistent_discipline", points=1, duration="scene"
            )


# ===========================================================================
# Review rework (Round-Trip 1): a strain-costing discipline on a STRAINLESS
# SWN core must REFUSE loudly WITHOUT committing Effort — never a partial spend,
# never an opaque AttributeError from the absent SWN strain engine.
# ===========================================================================


class TestStrainDisciplineOnStrainlessSwnCoreIsLoud:
    def _strain_discipline(self):
        from sidequest.genre.models.psionics import PsionicDiscipline

        return PsionicDiscipline(
            id="dominate",
            name="Hand on the Tiller",
            level=4,
            effort_cost=1,
            duration="scene",
            save="mental",
            strain_cost=1,  # a push — needs a System Strain pool to pay
            genre_description="x",
            mechanical_effect="y",
        )

    def test_strain_discipline_refused_loud_and_effort_not_spent(self):
        """SWN is Effort-only (no strain engine, no strain pool seeded). A
        strain-costing discipline must be REFUSED before any Effort is committed
        — applied=False, reason set, pool UNTOUCHED. The old behavior committed
        Effort then AttributeError'd on the missing apply_system_strain."""
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)  # no system_strain pool
        assert core.system_strain is None

        result = module.activate_discipline(
            core=core, discipline=self._strain_discipline(), source=_PSIONIC_SOURCE
        )

        assert result.applied is False, "a strain push on a strainless core must refuse"
        assert result.reason != "", "the refusal must carry a loud reason"
        # Effort must NOT have been spent — no partial application.
        assert core.effort[_PSIONIC_SOURCE].available == 3
        assert core.effort[_PSIONIC_SOURCE].committed == 0

    def test_refusal_does_not_raise_attribute_error(self):
        """Regression: the refusal path must not reach self.apply_system_strain
        (absent on SwnRulesetModule) — no AttributeError escapes."""
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        # Must not raise (the bug raised AttributeError after committing Effort).
        result = module.activate_discipline(
            core=core, discipline=self._strain_discipline(), source=_PSIONIC_SOURCE
        )
        assert result.applied is False

    def test_refused_strain_push_records_a_refused_discipline_span(self):
        """The refusal is loud on the GM panel: a {slug}.discipline.activated
        span fires with refused=True (the lie-detector sees the blocked push)."""
        module = get_ruleset_module("swn")
        core = _psychic_core(max_effort=3)
        exporter, tracer = _exporter()
        module.activate_discipline(
            core=core,
            discipline=self._strain_discipline(),
            source=_PSIONIC_SOURCE,
            _tracer=tracer,
        )
        disc = [s for s in exporter.get_finished_spans() if s.name == "swn.discipline.activated"]
        assert len(disc) == 1
        assert dict(disc[0].attributes or {})["refused"] is True
        # No effort.commit span — nothing was spent.
        commits = [s for s in exporter.get_finished_spans() if s.name == "swn.effort.commit"]
        assert commits == []
