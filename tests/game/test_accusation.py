"""Tests for ``sidequest.game.accusation`` — Story 50-8.

Covers the rule-based ``AccusationEvaluator`` from ADR-053 — the
component that turns a player's assembled evidence into a deterministic
verdict (Circumstantial / Strong / Airtight) plus an
``EvidenceSummary`` audit trail. The narrator dramatizes the summary;
it does NOT determine the verdict.

Test rubric:
- AC1: verdict computation across the three bands plus boundary cases
- AC2: ``EvidenceItem`` captures clue_id, chain-of-custody, confidence,
       contribution; ``EvidenceSummary`` carries the audited verdict +
       per-item scoring rationale
- AC3: ``SPAN_SCENARIO_ACCUSATION`` emits with full audit trail
       (evidence list, verdict, threshold reasoning) — per the
       CLAUDE.md OTEL Observability Principle
- AC4: red-herring clues score 0 against the actual guilty NPC
- Rule coverage: python lang-review #1 (silent fallbacks), #6 (test
       quality — meaningful assertions), #11 (input validation at
       boundaries). SOUL.md "No Silent Fallbacks": bad input MUST
       raise, never be swallowed.
- Wiring: evaluator is importable from its canonical module path and
       listed in ``__all__`` so consumers can star-import.

Scoring contract pinned by these tests (Dev must satisfy):

    Per confidence:
      Certain     = 2.0
      Suspected   = 1.0
      Rumored     = 0.5
      Discovered  = 1.5  (server-minted from ScenarioClue)

    Per contribution multiplier:
      helps   = +1
      hurts   = -1
      neutral =  0

    Chain-of-custody decay:
      raw * (0.7 ** hops)   — 0 hops = direct, each gossip hop multiplies.

    Red-herring clues (``ClueNode.red_herring is True``):
      Score 0 — they cannot contribute to the verdict regardless of confidence.

    Verdict bands (default thresholds):
      score < strong_threshold (3.0)         → Circumstantial
      strong_threshold <= score < airtight   → Strong
      score >= airtight_threshold (5.0)      → Airtight
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import ValidationError

from sidequest.game.accusation import (
    AccusationEvaluator,
    AccusationVerdict,
    EvidenceItem,
    EvidenceSummary,
)
from sidequest.game.scenario_state import ScenarioState
from sidequest.genre.models.scenario import ClueGraph, ClueNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clue(node_id: str, *, red_herring: bool = False) -> ClueNode:
    return ClueNode(
        id=node_id,
        type="testimony",
        description=f"clue {node_id}",
        discovery_method="conversation",
        visibility="public",
        red_herring=red_herring,
    )


def _scenario(*, clue_ids: list[str], red_herrings: list[str] | None = None) -> ScenarioState:
    red_set = set(red_herrings or [])
    return ScenarioState(
        clue_graph=ClueGraph(nodes=[_clue(cid, red_herring=cid in red_set) for cid in clue_ids]),
        guilty_npc="Erskine",
    )


def _evidence(
    *,
    clue_id: str = "c1",
    description: str = "Was seen near the library at 9pm",
    confidence: str = "Certain",
    chain_of_custody: list[str] | None = None,
    contribution: str = "helps",
) -> EvidenceItem:
    return EvidenceItem(
        clue_id=clue_id,
        description=description,
        confidence=confidence,
        chain_of_custody=chain_of_custody or [],
        contribution=contribution,
    )


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# AC1 — Verdict computation across the three bands.
# ---------------------------------------------------------------------------


class TestVerdictBands:
    def test_low_score_returns_circumstantial(self) -> None:
        """One Rumored helps-item (raw 0.5) sits well below the strong
        threshold and resolves to Circumstantial."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[_evidence(clue_id="c1", confidence="Rumored")],
        )

        assert isinstance(summary, EvidenceSummary)
        assert summary.verdict == AccusationVerdict.Circumstantial
        assert summary.score == pytest.approx(0.5)

    def test_medium_score_returns_strong(self) -> None:
        """Two Certain helps-items (2.0 + 2.0 = 4.0) lands in the Strong
        band — at-or-above the strong threshold (3.0) but below airtight
        (5.0)."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain"),
                _evidence(clue_id="c2", confidence="Certain"),
            ],
        )

        assert summary.verdict == AccusationVerdict.Strong
        assert summary.score == pytest.approx(4.0)

    def test_high_score_returns_airtight(self) -> None:
        """Three Certain helps-items (2.0 × 3 = 6.0) clears the airtight
        threshold (5.0)."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2", "c3"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain"),
                _evidence(clue_id="c2", confidence="Certain"),
                _evidence(clue_id="c3", confidence="Certain"),
            ],
        )

        assert summary.verdict == AccusationVerdict.Airtight
        assert summary.score == pytest.approx(6.0)

    def test_score_exactly_at_strong_threshold_is_strong(self) -> None:
        """Boundary: score == strong_threshold lands in the Strong band
        (inclusive lower bound). Without this pin, a refactor that flips
        the comparator to ``>`` would silently demote borderline-Strong
        accusations to Circumstantial."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        # 1.5 + 1.5 = 3.0 exactly.
        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Discovered"),
                _evidence(clue_id="c2", confidence="Discovered"),
            ],
        )

        assert summary.verdict == AccusationVerdict.Strong
        assert summary.score == pytest.approx(3.0)

    def test_score_exactly_at_airtight_threshold_is_airtight(self) -> None:
        """Boundary: score == airtight_threshold lands Airtight (inclusive)."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2", "c3", "c4"])

        # 1.5 × 4 = 6.0 — well past 5.0; pin a 5.0-exact case via mix:
        # Certain (2.0) + Certain (2.0) + Discovered (1.0 after helps) → no.
        # Use: Certain + Certain + 1×Suspected (1.0) = 5.0 exactly.
        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain"),
                _evidence(clue_id="c2", confidence="Certain"),
                _evidence(clue_id="c3", confidence="Suspected"),
            ],
        )

        assert summary.score == pytest.approx(5.0)
        assert summary.verdict == AccusationVerdict.Airtight

    def test_hurts_contribution_subtracts_from_score(self) -> None:
        """An item marked ``contribution='hurts'`` subtracts from the
        score — used when the player presents evidence that exonerates
        the accused. Without this pin, a regression could ignore the
        sign and treat hurts the same as helps."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        # Certain helps (+2.0) + Certain hurts (-2.0) = 0.0 → Circumstantial.
        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain", contribution="helps"),
                _evidence(clue_id="c2", confidence="Certain", contribution="hurts"),
            ],
        )

        assert summary.score == pytest.approx(0.0)
        assert summary.verdict == AccusationVerdict.Circumstantial

    def test_neutral_contribution_does_not_score(self) -> None:
        """``contribution='neutral'`` contributes zero regardless of
        confidence — the evidence is on the table but the player
        flagged it as ambiguous."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain", contribution="helps"),
                _evidence(clue_id="c2", confidence="Certain", contribution="neutral"),
            ],
        )

        # Only the first item scores (2.0). Second adds 0.0.
        assert summary.score == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# AC2 — EvidenceSummary captures source, confidence, contribution, plus
# the audited verdict and a rationale string.
# ---------------------------------------------------------------------------


class TestEvidenceSummaryShape:
    def test_summary_preserves_input_evidence_verbatim(self) -> None:
        """Every EvidenceItem the caller passed must round-trip into the
        summary — no silent reordering, no field stripping."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        items = [
            _evidence(
                clue_id="c1",
                description="Glove found in study",
                confidence="Discovered",
                chain_of_custody=[],
            ),
            _evidence(
                clue_id="c2",
                description="Maid heard footsteps",
                confidence="Rumored",
                chain_of_custody=["Bert"],
            ),
        ]

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=items,
        )

        assert summary.accused_npc == "Erskine"
        assert len(summary.evidence) == 2
        assert summary.evidence[0].clue_id == "c1"
        assert summary.evidence[0].description == "Glove found in study"
        assert summary.evidence[0].confidence == "Discovered"
        assert summary.evidence[0].chain_of_custody == []
        assert summary.evidence[1].clue_id == "c2"
        assert summary.evidence[1].confidence == "Rumored"
        assert summary.evidence[1].chain_of_custody == ["Bert"]

    def test_summary_rationale_is_non_empty(self) -> None:
        """The summary must carry a non-empty rationale string — the
        narrator dramatizes from this. A blank rationale would force
        the narrator to improvise, which the OTEL Observability
        Principle and ADR-053 explicitly forbid."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[_evidence(clue_id="c1", confidence="Certain")],
        )

        assert isinstance(summary.rationale, str)
        assert summary.rationale.strip(), (
            "rationale must be non-empty — the narrator dramatizes from this; a "
            "blank string forces it to improvise (ADR-053 forbids)."
        )

    def test_summary_chain_of_custody_preserved_for_indirect_testimony(self) -> None:
        """Multi-hop testimony chains must round-trip into the summary so
        the audit trail can show how a low-confidence Rumored claim
        reached the player. Without this, the verdict explanation
        cannot answer 'why did this score so low?' for the GM panel."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(
                    clue_id="c1",
                    confidence="Certain",
                    chain_of_custody=["Alice", "Bert", "Carol"],
                )
            ],
        )

        assert summary.evidence[0].chain_of_custody == ["Alice", "Bert", "Carol"]


# ---------------------------------------------------------------------------
# AC3/AC5 — OTEL emission. SPAN_SCENARIO_ACCUSATION must fire with full
# audit trail: evidence count, verdict, score, accused_npc, threshold
# reasoning. The GM panel is the lie detector.
# ---------------------------------------------------------------------------


class TestOtelObservability:
    def test_evaluate_emits_scenario_accusation_span(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        from sidequest.telemetry.spans import SPAN_SCENARIO_ACCUSATION

        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain"),
                _evidence(clue_id="c2", confidence="Certain"),
            ],
        )

        spans = _spans_named(otel_capture, SPAN_SCENARIO_ACCUSATION)
        assert len(spans) == 1, (
            f"Expected exactly one SPAN_SCENARIO_ACCUSATION per evaluate() "
            f"call; got {len(spans)} in "
            f"{[s.name for s in otel_capture.get_finished_spans()]}"
        )

    def test_accusation_span_carries_full_audit_trail(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        """The GM panel needs accused_npc, verdict, score, and an
        evidence-count snapshot on the span. Pin them explicitly so a
        regression that emits the span with sparse attributes doesn't
        pass a bare existence check."""
        from sidequest.telemetry.spans import SPAN_SCENARIO_ACCUSATION

        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"])

        evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain"),
                _evidence(clue_id="c2", confidence="Certain"),
            ],
        )

        spans = _spans_named(otel_capture, SPAN_SCENARIO_ACCUSATION)
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})

        # Audit-trail fields — every one a load-bearing OTEL contract.
        assert attrs["accused_npc"] == "Erskine"
        assert attrs["verdict"] == AccusationVerdict.Strong
        assert attrs["score"] == pytest.approx(4.0)
        assert attrs["evidence_count"] == 2
        # Threshold reasoning — explains WHY this score → that verdict.
        assert attrs["strong_threshold"] == pytest.approx(3.0)
        assert attrs["airtight_threshold"] == pytest.approx(5.0)
        # Match between accused and the scenario's guilty suspect — the
        # GM panel uses this to flag false-accusation arcs.
        assert attrs["matches_guilty"] is True

    def test_accusation_span_records_mismatched_guilty(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        """When the player accuses an NPC who is NOT the scenario's
        guilty suspect, the span must surface ``matches_guilty=False`` —
        the GM panel reads this to detect false accusations without
        having to diff scenario state."""
        from sidequest.telemetry.spans import SPAN_SCENARIO_ACCUSATION

        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])  # guilty_npc="Erskine"

        evaluator.evaluate(
            scenario=scenario,
            accused_npc="Maud",  # not the guilty NPC
            evidence=[_evidence(clue_id="c1", confidence="Certain")],
        )

        spans = _spans_named(otel_capture, SPAN_SCENARIO_ACCUSATION)
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs["accused_npc"] == "Maud"
        assert attrs["matches_guilty"] is False, (
            "False accusation must surface as matches_guilty=False on the "
            "span; the GM panel uses this to flag missed-mystery arcs."
        )


# ---------------------------------------------------------------------------
# AC4 — Chain-of-custody decay + red-herring exclusion.
# ---------------------------------------------------------------------------


class TestChainOfCustodyDecay:
    def test_direct_evidence_no_decay(self) -> None:
        """Zero-hop evidence is direct testimony — full raw weight."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[_evidence(clue_id="c1", confidence="Certain", chain_of_custody=[])],
        )

        # Certain (2.0) × 0.7^0 = 2.0 × 1.0 = 2.0
        assert summary.score == pytest.approx(2.0)

    def test_single_hop_decays_by_factor_07(self) -> None:
        """One gossip hop: raw × 0.7."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[_evidence(clue_id="c1", confidence="Certain", chain_of_custody=["Alice"])],
        )

        # 2.0 × 0.7 = 1.4
        assert summary.score == pytest.approx(1.4)

    def test_two_hop_decays_more_than_one_hop(self) -> None:
        """A→B→C two-hop chain decays strictly more than a one-hop chain.
        Pinning the exact value (2.0 × 0.49 = 0.98) catches a regression
        that drops decay altogether or uses the wrong base."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(
                    clue_id="c1",
                    confidence="Certain",
                    chain_of_custody=["Alice", "Bert"],
                )
            ],
        )

        # 2.0 × (0.7 ** 2) = 2.0 × 0.49 = 0.98
        assert summary.score == pytest.approx(0.98)


class TestRedHerringExclusion:
    def test_red_herring_clue_scores_zero(self) -> None:
        """A clue marked red_herring=True in the scenario's clue_graph
        scores zero regardless of confidence. The narrator may dramatize
        the player's confusion, but the rule layer refuses to award
        points for a red herring."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1", "c2"], red_herrings=["c2"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[
                _evidence(clue_id="c1", confidence="Certain"),
                # c2 is a red herring — must score 0 even though Certain helps.
                _evidence(clue_id="c2", confidence="Certain"),
            ],
        )

        # Only c1 scores. c2's 2.0 is wiped by red-herring exclusion.
        assert summary.score == pytest.approx(2.0)
        assert summary.verdict == AccusationVerdict.Circumstantial

    def test_red_herring_only_evidence_returns_circumstantial(self) -> None:
        """An accusation backed solely by red herrings scores 0 and
        lands in the Circumstantial band — never silently bumped to a
        higher band by some default-floor regression."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"], red_herrings=["c1"])

        summary = evaluator.evaluate(
            scenario=scenario,
            accused_npc="Erskine",
            evidence=[_evidence(clue_id="c1", confidence="Certain")],
        )

        assert summary.score == pytest.approx(0.0)
        assert summary.verdict == AccusationVerdict.Circumstantial


# ---------------------------------------------------------------------------
# Input validation — python lang-review #1 (silent fallbacks) + #11
# (input validation at boundaries). CLAUDE.md "No Silent Fallbacks".
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_evidence_item_rejects_empty_clue_id(self) -> None:
        """An empty clue_id is structurally meaningless — the evidence
        can't be linked to any clue node. Constructor must raise rather
        than absorb the empty string silently."""
        with pytest.raises(ValidationError):
            EvidenceItem(
                clue_id="",
                description="Was at the library",
                confidence="Certain",
            )

    def test_evidence_item_rejects_empty_description(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceItem(
                clue_id="c1",
                description="",
                confidence="Certain",
            )

    def test_evidence_item_rejects_unknown_confidence(self) -> None:
        """Confidence is a closed enum: Certain/Suspected/Rumored/Discovered.
        Unknown values must be rejected at construction — no silent
        coercion to a default."""
        with pytest.raises(ValidationError):
            EvidenceItem(
                clue_id="c1",
                description="x",
                confidence="MaybeProbably",  # not in the enum
            )

    def test_evidence_item_rejects_unknown_contribution(self) -> None:
        """Contribution is closed: helps/hurts/neutral. Unknown rejected."""
        with pytest.raises(ValidationError):
            EvidenceItem(
                clue_id="c1",
                description="x",
                confidence="Certain",
                contribution="weakens",  # not in the enum
            )

    def test_evaluate_rejects_empty_accused_npc(self) -> None:
        """Empty accused_npc is structurally invalid — there is no
        person to accuse. Refuse the call rather than silently produce
        a verdict against the empty string."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        with pytest.raises(ValueError):
            evaluator.evaluate(
                scenario=scenario,
                accused_npc="",
                evidence=[_evidence(clue_id="c1", confidence="Certain")],
            )

    def test_evaluate_rejects_empty_evidence(self) -> None:
        """An accusation backed by zero evidence cannot produce a
        verdict — the rule layer refuses rather than emitting a
        Circumstantial-by-default span. SOUL.md: 'No Silent Fallbacks'."""
        evaluator = AccusationEvaluator()
        scenario = _scenario(clue_ids=["c1"])

        with pytest.raises(ValueError):
            evaluator.evaluate(
                scenario=scenario,
                accused_npc="Erskine",
                evidence=[],
            )


# ---------------------------------------------------------------------------
# Wiring — module exports + span registry. Every test suite needs a
# wiring test (CLAUDE.md). The narration-response wiring is exercised
# in tests/server/test_scenario_accusation_intake.py.
# ---------------------------------------------------------------------------


class TestWiring:
    def test_accusation_module_exports_public_surface(self) -> None:
        """The evaluator and its support types must be reachable from
        the canonical module path AND listed in ``__all__`` so star
        imports + IDE discovery work."""
        import sidequest.game.accusation as ax

        assert hasattr(ax, "AccusationEvaluator")
        assert hasattr(ax, "EvidenceItem")
        assert hasattr(ax, "EvidenceSummary")
        assert hasattr(ax, "AccusationVerdict")
        exported = getattr(ax, "__all__", [])
        assert "AccusationEvaluator" in exported, (
            "AccusationEvaluator must appear in __all__ — consumers import via star"
        )
        assert "EvidenceItem" in exported
        assert "EvidenceSummary" in exported
        assert "AccusationVerdict" in exported

    def test_scenario_accusation_span_registered(self) -> None:
        """``SPAN_SCENARIO_ACCUSATION`` must be in the telemetry catalog
        (flat-only or routed). Otherwise the routing-completeness suite
        will trip and the GM panel will see unrouted span names."""
        from sidequest.telemetry.spans import (
            FLAT_ONLY_SPANS,
            SPAN_ROUTES,
            SPAN_SCENARIO_ACCUSATION,
        )

        assert (
            SPAN_SCENARIO_ACCUSATION in FLAT_ONLY_SPANS or SPAN_SCENARIO_ACCUSATION in SPAN_ROUTES
        ), "SPAN_SCENARIO_ACCUSATION must be registered (flat-only or routed)"

    def test_verdict_constants_match_band_strings(self) -> None:
        """``AccusationVerdict`` exposes string constants the
        ``EvidenceSummary.verdict`` field uses. A drift between the
        constant value and the Literal in the model would silently
        break the audit trail. Pin the exact lowercase tokens — the
        OTEL span attributes carry these strings."""
        assert AccusationVerdict.Circumstantial == "circumstantial"
        assert AccusationVerdict.Strong == "strong"
        assert AccusationVerdict.Airtight == "airtight"
