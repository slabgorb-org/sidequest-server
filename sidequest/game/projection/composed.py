"""ComposedFilter — the production ProjectionFilter.

Pipeline: CoreInvariantStage (GM / targeted / self-authored / gm-only)
    → GenreRuleStage (projection.yaml rules)
    → default pass-through.
"""

from __future__ import annotations

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.genre_stage import GenreRuleStage
from sidequest.game.projection.invariants import CoreInvariantStage
from sidequest.game.projection.rules import ProjectionRules, load_rules_from_yaml_str
from sidequest.game.projection.view import GameStateView
from sidequest.game.projection_filter import FilterDecision
from sidequest.telemetry.spans import projection_decide_span


class ComposedFilter:
    """Implements the ProjectionFilter Protocol."""

    def __init__(
        self,
        *,
        rules: ProjectionRules,
        invariants: CoreInvariantStage | None = None,
        pack_slug: str | None = None,
    ) -> None:
        """Construct a ComposedFilter.

        ``pack_slug`` is the genre pack slug (e.g. ``"mutant_wasteland"``)
        used to compose the OTEL ``rule.source`` attribute in the
        ``genre:<pack>/<kind>/<rule_index>`` form the GM panel consumes.
        Pass ``None`` when no pack-level rules can possibly fire (e.g.
        ``with_no_genre_rules``) — the attribute falls back to
        ``default:pass_through``.
        """
        self._invariants = invariants or CoreInvariantStage()
        self._genre = GenreRuleStage(rules)
        self._pack_slug = pack_slug

    def project(
        self,
        *,
        envelope: MessageEnvelope,
        view: GameStateView,
        player_id: str,
    ) -> FilterDecision:
        with projection_decide_span(
            event_kind=envelope.kind,
            event_seq=envelope.origin_seq,
            player_id=player_id,
        ) as span:
            outcome = self._invariants.evaluate(envelope=envelope, view=view, player_id=player_id)
            if outcome.terminal:
                assert outcome.decision is not None
                assert outcome.source is not None, (
                    "CoreInvariantStage returned terminal outcome without "
                    "a source — this is a bug: every terminal branch must "
                    "populate InvariantOutcome.source."
                )
                decision = outcome.decision
                source = outcome.source
            else:
                genre_result = self._genre.evaluate(
                    envelope=envelope, view=view, player_id=player_id
                )
                decision = genre_result.decision
                source = self._format_genre_source(
                    kind=envelope.kind,
                    matched_rule_index=genre_result.matched_rule_index,
                )
            span.set_attribute("decision.include", decision.include)
            span.set_attribute("rule.source", source)
            return decision

    def _format_genre_source(self, *, kind: str, matched_rule_index: int | None) -> str:
        """Compose the OTEL ``rule.source`` attribute for a genre-stage result.

        Spec format: ``genre:<pack>/<kind>/<rule_index>``. Falls back to
        ``default:pass_through`` when no genre rule altered the decision
        (no rule exists for this kind, or rules existed but all gates
        passed without redactions).
        """
        if matched_rule_index is None:
            return "default:pass_through"
        pack = self._pack_slug or "<unknown>"
        return f"genre:{pack}/{kind}/{matched_rule_index}"

    @classmethod
    def with_no_genre_rules(cls) -> ComposedFilter:
        """Convenience for sessions whose genre pack has no projection.yaml."""
        return cls(rules=load_rules_from_yaml_str("rules: []"), pack_slug=None)
