"""LethalityArbiter — deterministic lethality synthesis (Group C).

Spec: docs/superpowers/specs/2026-04-23-local-dm-decomposer-design.md §4

Runs AFTER run_dispatch_bank and BEFORE narrator_directives registration.
Reads:
  - LethalityPolicy (loaded from the active genre pack)
  - Player-character cores (`pc_cores_by_player`)
  - NPC cores present in the scene (`npc_cores_by_name`)
  - BankResult (for future subsystems that emit `data["fatal_hit"]` etc.)

Phase A trigger is HP-based (ADR-114): any core with `hp.current == 0` fires
the policy's `verdicts_on_zero_hp` entry. Confrontation-beat-failure and
resource-pool-depletion triggers land in Group E when the subsystems that
produce those signals exist on the Python port.

The arbiter is deterministic and synchronous — no LLM call. The decomposer
may still emit `LethalityVerdict` entries in `DispatchPackage.per_player[*].
lethality` for paper-trail purposes; arbiter output is authoritative on
conflict (see Task 8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sidequest.agents.subsystems import BankResult
from sidequest.game.creature_core import CreatureCore
from sidequest.genre.models.lethality import LethalityPolicy
from sidequest.protocol.dispatch import (
    DispatchPackage,
    LethalityVerdict,
    LethalityVerdictKind,
    NarratorDirective,
    VisibilityTag,
)
from sidequest.telemetry.spans import intent_router_lethality_arbitrate_span

logger = logging.getLogger(__name__)


@dataclass
class LethalityResult:
    """Arbiter output: authoritative verdicts + paired narrator directives."""

    verdicts: list[LethalityVerdict] = field(default_factory=list)
    directives: list[NarratorDirective] = field(default_factory=list)


class LethalityArbiter:
    """Synthesise lethality verdicts from post-bank state + genre policy."""

    def __init__(self, policy: LethalityPolicy) -> None:
        self._policy = policy

    def arbitrate(
        self,
        *,
        package: DispatchPackage,
        bank_result: BankResult,
        pc_cores_by_player: dict[str, CreatureCore],
        npc_cores_by_name: dict[str, CreatureCore],
    ) -> LethalityResult:
        with intent_router_lethality_arbitrate_span(
            turn_id=package.turn_id,
            genre_key=self._policy.genre_key,
        ) as span:
            result = LethalityResult()
            for player_id, core in pc_cores_by_player.items():
                if core.hp.current == 0:
                    self._emit(
                        result,
                        entity=f"player:{player_id}",
                        verdict_kind=self._policy.verdicts_on_zero_hp.pc,
                        core=core,
                    )
            for npc_name, core in npc_cores_by_name.items():
                if core.hp.current == 0:
                    self._emit(
                        result,
                        entity=f"npc:{npc_name}",
                        verdict_kind=self._policy.verdicts_on_zero_hp.npc,
                        core=core,
                    )
            # Merge decomposer-authored verdicts. Arbiter wins on entity
            # conflict; decomposer-only entities pass through.
            arbiter_entities = {v.entity for v in result.verdicts}
            for pd in package.per_player:
                for decomposer_v in pd.lethality:
                    if decomposer_v.entity not in arbiter_entities:
                        result.verdicts.append(decomposer_v)
            span.set_attribute("verdict_count", len(result.verdicts))
            return result

    def _emit(
        self,
        result: LethalityResult,
        *,
        entity: str,
        verdict_kind: LethalityVerdictKind,
        core: CreatureCore,
    ) -> None:
        """Append one verdict + its paired must/must-not directives."""
        cause = f"{core.name} reduced to zero HP (0/{core.hp.max})"
        result.verdicts.append(
            self._build_verdict(
                entity=entity,
                verdict_kind=verdict_kind,
                cause=cause,
            )
        )
        # Paired directives — narrator reads them as one constraint envelope.
        shared_viz = VisibilityTag(
            visible_to="all",
            perception_fidelity={},
            secrets_for=[],
            redact_from_narrator_canonical=False,
        )
        result.directives.append(
            NarratorDirective(
                kind="must_narrate",
                payload=f"{entity} verdict={verdict_kind}. {self._policy.must_narrate}",
                visibility=shared_viz,
            )
        )
        result.directives.append(
            NarratorDirective(
                kind="must_not_narrate",
                payload=self._policy.must_not_narrate,
                visibility=shared_viz,
            )
        )

    def _build_verdict(
        self,
        *,
        entity: str,
        verdict_kind: LethalityVerdictKind,
        cause: str,
    ) -> LethalityVerdict:
        policy = self._policy
        directive = (
            f"{entity} verdict={verdict_kind}. "
            f"{policy.must_narrate} "
            f"Do NOT: {policy.must_not_narrate}"
        )
        return LethalityVerdict(
            entity=entity,
            verdict=verdict_kind,
            cause=cause,
            reversibility=policy.default_reversibility,
            narrator_directive=directive,
            soul_md_constraint=policy.soul_md_constraint,
            witness_scope={},  # Group G fills this in from VisibilityTag pipeline
        )


__all__ = ["LethalityArbiter", "LethalityResult"]
