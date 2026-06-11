"""Story 103-1 RED — apply_saint_preset: the Saint-Marked chargen path.

AC3 (story context): "creating a Saint-Marked character yields exactly the
bundle's positives + the drawback negative on the sheet, with MP accounting
consistent with MpEconomy (drawback counts as the negative that funds the
bundle per AWN pricing)."

Pinned MP contract (the faithful AWN reading — curation replaces dice, not
pricing): the preset ledger is

    mp_remaining = base_mp + per_negative_mp            # drawback funds it
                 - len(bundle) * spend_random_positive  # the spring "rolls"

With the default economy (2 + 2, random pull = 1) a 2-mark bundle leaves
2 MP banked. Negatives land before positives in the acquisition log
(AWN p.16 — burdens before gifts).

Saint-Marked vs Wild is two presets over ONE engine: after the preset, the
ordinary acquire_ops economy keeps working (the affinity test proves
composition with zero new pricing paths).

Test-design decision (recorded in session Design Deviations): an explicit
``saint_id`` applies regardless of ``mp_economy.mutant_classes`` — the
Saint's spring does not check your class; stock/class orthogonality is
103-2's model. The class gate remains for the classic seed path.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.mutation.acquire_ops import acquire_positive, acquire_random_negative
from sidequest.mutation.context_builder import build_mutation_static_block
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.saints import SaintDef, SaintRegistry, apply_saint_preset
from sidequest.mutation.state import MutationState
from sidequest.telemetry import spans as spans_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/test_obsessive",
                name="Obsessive Monologue",
                roll_range=(1, 50),
                effect="cannot stop telling a story once begun",
            ),
            NegativeMutationDef(
                id="negative/test_frail",
                name="Frail",
                roll_range=(51, 100),
                effect="frail",
            ),
        ],
        positives=[
            PositiveMutationDef(
                id="structure/test_bone_density",
                name="Whale-Bone Density",
                category="structure",
                effect="dense bones",
            ),
            PositiveMutationDef(
                id="sense/test_deep_sight",
                name="Deep-Pressure Sight",
                category="sense",
                effect="see in the deep",
            ),
            PositiveMutationDef(
                id="hybrid/test_salt_blood",
                name="Salt Blood",
                category="hybrid",
                effect="salt tolerance",
            ),
        ],
    )


def _registry() -> SaintRegistry:
    return SaintRegistry(
        saints=[
            SaintDef(
                id="herman_of_the_acushnet",
                name="Saint Herman of the Acushnet",
                tradition="literary",
                patron_regions=["whalecoast"],
                bundle=["structure/test_bone_density", "sense/test_deep_sight"],
                drawback="negative/test_obsessive",
                affinity=["hybrid/test_salt_blood"],
            )
        ]
    )


def _apply(
    state: MutationState,
    *,
    saint_id: str = "herman_of_the_acushnet",
    actor: str = "Ishmael",
):
    return apply_saint_preset(
        state,
        _catalog(),
        _registry(),
        actor=actor,
        saint_id=saint_id,
        session_id="saint-preset-test",
    )


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """In-memory exporter wired through spans_module.tracer — the same seam
    every production emit site resolves (pattern: test_mutation_wiring.py)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.saint_preset")
    monkeypatch.setattr(spans_module, "tracer", lambda: tracer)
    return exporter


# ---------------------------------------------------------------------------
# Preset application
# ---------------------------------------------------------------------------


class TestApplySaintPreset:
    def test_bundle_and_drawback_on_sheet_exactly(self) -> None:
        state = MutationState()
        cs = _apply(state)
        assert cs is not None
        assert cs.positive_ids == ["structure/test_bone_density", "sense/test_deep_sight"]
        assert cs.negative_ids == ["negative/test_obsessive"]
        # Exactly — no stowaway marks from the registry's other fields.
        assert "hybrid/test_salt_blood" not in cs.positive_ids

    def test_mp_arithmetic_matches_economy(self) -> None:
        """2 (base) + 2 (the one drawback) - 2 marks x 1 (random rate) = 2."""
        state = MutationState()
        cs = _apply(state)
        assert cs.mp_remaining == 2

    def test_burdens_before_gifts_in_acquisition_log(self) -> None:
        """AWN p.16 ordering — the drawback lands first in the log, mirroring
        the Wild path where negatives are rolled before positives."""
        state = MutationState()
        cs = _apply(state)
        assert cs.acquisition_log[0] == "negative/test_obsessive"
        assert set(cs.acquisition_log[1:]) == {
            "structure/test_bone_density",
            "sense/test_deep_sight",
        }

    def test_idempotent_no_regrant(self) -> None:
        """Re-entrant chargen handlers must not double-grant (mirrors
        seed_character_mutations' idempotency guard)."""
        state = MutationState()
        first = _apply(state)
        first_positives = list(first.positive_ids)
        first_mp = first.mp_remaining
        second = _apply(state)
        assert second.positive_ids == first_positives
        assert second.mp_remaining == first_mp
        assert len(second.negative_ids) == 1

    def test_unknown_saint_raises_keyerror(self) -> None:
        state = MutationState()
        with pytest.raises(KeyError) as exc_info:
            _apply(state, saint_id="saint_nobody")
        assert "saint_nobody" in str(exc_info.value)

    def test_state_registered_under_actor(self) -> None:
        state = MutationState()
        _apply(state, actor="Ishmael")
        assert "Ishmael" in state.characters

    def test_affinity_purchase_composes_with_acquire_ops(self) -> None:
        """Saint-Marked is a preset over the SAME engine, not a second economy:
        spec §6 'additional drawbacks for additional mutations from the
        affinity list' rides the existing acquire_ops verbatim.

        After the preset (2 MP banked): a second negative pays +2 -> 4 MP,
        then PICKING the affinity mark costs spend_pick_positive (3) -> 1 MP.
        """
        state = MutationState()
        _apply(state)
        neg = acquire_random_negative(
            state,
            _catalog(),
            actor="Ishmael",
            session_id="saint-preset-test",
            source="affinity_drawback",
        )
        assert neg.applied is True
        assert state.characters["Ishmael"].mp_remaining == 4
        pick = acquire_positive(
            state,
            _catalog(),
            actor="Ishmael",
            session_id="saint-preset-test",
            source="affinity_pick",
            mutation_id="hybrid/test_salt_blood",
        )
        assert pick.applied is True
        assert "hybrid/test_salt_blood" in state.characters["Ishmael"].positive_ids
        assert state.characters["Ishmael"].mp_remaining == 1

    def test_drawback_surfaces_in_narrator_mutation_context(self) -> None:
        """AC4's honest half with existing machinery: the drawback is part of
        'the mechanical truth — never invent powers' block the narrator
        receives. If the drawback is missing here, the narrator can quietly
        forget Saint Herman cannot stop telling a story."""
        state = MutationState()
        _apply(state)
        block = build_mutation_static_block(mutation_state=state, catalog=_catalog())
        assert "negative/test_obsessive" in block
        assert "Obsessive Monologue" in block


# ---------------------------------------------------------------------------
# OTEL — awn.saint.applied (build plan D-D: the lie-detector span)
# ---------------------------------------------------------------------------


class TestSaintAppliedSpan:
    def test_span_fires_with_mp_math(self, span_exporter: InMemorySpanExporter) -> None:
        state = MutationState()
        _apply(state)
        spans = [s for s in span_exporter.get_finished_spans() if s.name == "awn.saint.applied"]
        assert spans, (
            "awn.saint.applied must fire on preset application; captured: "
            f"{[s.name for s in span_exporter.get_finished_spans()]}"
        )
        attrs = spans[0].attributes or {}
        assert attrs.get("actor") == "Ishmael"
        assert attrs.get("saint_id") == "herman_of_the_acushnet"
        assert attrs.get("drawback") == "negative/test_obsessive"
        assert attrs.get("bundle_count") == 2
        # The MP arithmetic must be auditable from the GM panel alone:
        assert attrs.get("mp_base") == 2
        assert attrs.get("mp_from_drawback") == 2
        assert attrs.get("mp_spent") == 2
        assert attrs.get("mp_remaining") == 2

    def test_span_not_refired_on_idempotent_replay(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        """A re-entrant chargen confirm must not double-report the grant —
        one application, one span."""
        state = MutationState()
        _apply(state)
        _apply(state)
        spans = [s for s in span_exporter.get_finished_spans() if s.name == "awn.saint.applied"]
        assert len(spans) == 1

    def test_span_route_registered_for_gm_panel(self) -> None:
        """A span the WatcherHub can't route never reaches the GM panel —
        registration in SPAN_ROUTES is what makes the lie detector see it."""
        from sidequest.telemetry.spans._core import SPAN_ROUTES

        assert "awn.saint.applied" in SPAN_ROUTES
