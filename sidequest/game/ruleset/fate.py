"""FateRulesetModule — Fate Core behind the RulesetModule seam (ADR-144).

Resolution is 4dF + skill vs opposition on the ladder (see fate_resolution.py).
The d20/beat-shaped abstract methods are NOT part of Fate's paradigm; they fail
loud here (No Silent Fallbacks) until ADR-144 F5 demotes them to base
default-raise and these overrides are deleted. The Fate conflict engine
(fate_conflict.py) arrives in F1c; dispatch routing in F1d.
"""

from __future__ import annotations

import random
from typing import Literal

from opentelemetry import trace

from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.fate_resolution import FateOutcome, Opposition, resolve_action
from sidequest.telemetry.spans.fate import (
    fate_action_resolved_span,
    fate_aspect_invoked_span,
    fate_compel_accepted_span,
    fate_compel_offered_span,
    fate_point_delta_span,
)

_NO_D20_SURFACE = (
    "the 'fate' ruleset resolves via the Fate conflict engine (4dF + ladder), "
    "not the d20/beat surface — No Silent Fallbacks (ADR-144)"
)


class FateEconomyError(ValueError):
    """A Fate economy operation violated the rules (no fate point to spend, an
    already-checked stress box, a filled consequence slot, an unknown aspect).
    Fail loud — No Silent Fallbacks (ADR-144 / SOUL.md)."""


class FateRulesetModule(RulesetModule):
    slug = "fate"

    @property
    def awards_native_turn_xp(self) -> bool:
        # Fate advances by milestones, not the ADR-021 native XP tick.
        return False

    def resolve_action(
        self,
        *,
        skill_rating: int,
        opposition: Opposition,
        rng: random.Random,
        invoke_bonus: int = 0,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> FateOutcome:
        """Resolve one Fate action and emit the lie-detector span."""
        outcome = resolve_action(
            skill_rating=skill_rating,
            opposition=opposition,
            rng=rng,
            invoke_bonus=invoke_bonus,
        )
        fate_action_resolved_span(
            actor=actor,
            skill_rating=skill_rating,
            dice=outcome.dice,
            ladder_total=outcome.ladder_total,
            opposition=outcome.opposition,
            opposition_kind=opposition.kind,
            shifts=outcome.shifts,
            tier=outcome.tier.value,
            _tracer=_tracer,
        )
        return outcome

    # --- Fate-point economy (rules + spans; FateSheet is inert data) ----------

    def spend_fate_point(
        self, *, sheet: FateSheet, reason: str, actor: str = "", _tracer: trace.Tracer | None = None
    ) -> int:
        """Debit one fate point. Fails loud at zero (No Silent Fallbacks)."""
        if sheet.fate_points <= 0:
            raise FateEconomyError(
                f"{actor or 'actor'} has no fate point to spend (reason={reason!r})"
            )
        before = sheet.fate_points
        sheet.fate_points -= 1
        fate_point_delta_span(
            actor=actor, reason=reason, before=before, after=sheet.fate_points, _tracer=_tracer
        )
        return sheet.fate_points

    def earn_fate_point(
        self, *, sheet: FateSheet, reason: str, actor: str = "", _tracer: trace.Tracer | None = None
    ) -> int:
        """Credit one fate point (compel accepted, concession)."""
        before = sheet.fate_points
        sheet.fate_points += 1
        fate_point_delta_span(
            actor=actor, reason=reason, before=before, after=sheet.fate_points, _tracer=_tracer
        )
        return sheet.fate_points

    def refresh_fate_points(
        self, *, sheet: FateSheet, actor: str = "", _tracer: trace.Tracer | None = None
    ) -> int:
        """Session refresh: raise fate points UP to refresh; never reduce a
        higher banked total (SRD)."""
        before = sheet.fate_points
        sheet.fate_points = max(sheet.fate_points, sheet.refresh)
        fate_point_delta_span(
            actor=actor, reason="refresh", before=before, after=sheet.fate_points, _tracer=_tracer
        )
        return sheet.fate_points

    def invoke_aspect(
        self,
        *,
        sheet: FateSheet,
        aspect_text: str,
        mode: Literal["bonus", "reroll"] = "bonus",
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Invoke an aspect for ``mode`` ('bonus' → +2, 'reroll' → reroll). Uses a
        free invocation if the aspect has one; otherwise spends a fate point.
        Returns the numeric bonus (2 for 'bonus', 0 for 'reroll' — the reroll
        itself is the caller's/F1c's job). Fails loud on an unknown aspect or an
        unknown mode (No Silent Fallbacks). Validate first, then mutate, then
        emit — an invalid mode must NOT burn an invoke / spend a fate point."""
        aspect = next((a for a in sheet.all_aspects() if a.text == aspect_text), None)
        if aspect is None:
            raise FateEconomyError(
                f"{actor or 'actor'} cannot invoke unknown aspect {aspect_text!r}"
            )
        if mode not in ("bonus", "reroll"):
            raise FateEconomyError(
                f"{actor or 'actor'} invoked {aspect_text!r} with unknown mode "
                f"{mode!r} (expected 'bonus' or 'reroll')"
            )
        free = aspect.free_invokes > 0
        if free:
            aspect.free_invokes -= 1
        else:
            self.spend_fate_point(sheet=sheet, reason="invoke", actor=actor, _tracer=_tracer)
        fate_aspect_invoked_span(
            actor=actor,
            aspect=aspect_text,
            free=free,
            mode=mode,
            fate_points_after=sheet.fate_points,
            _tracer=_tracer,
        )
        return 2 if mode == "bonus" else 0

    def offer_compel(
        self, *, aspect_text: str, actor: str = "", _tracer: trace.Tracer | None = None
    ) -> None:
        """Surface that the narrator proposed a compel (no economy change). The
        OTEL span lets the GM panel see the offer even when the player declines."""
        fate_compel_offered_span(actor=actor, aspect=aspect_text, _tracer=_tracer)

    def accept_compel(
        self,
        *,
        sheet: FateSheet,
        aspect_text: str,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Accept a compel: earn one fate point + emit the compel span."""
        after = self.earn_fate_point(sheet=sheet, reason="compel", actor=actor, _tracer=_tracer)
        fate_compel_accepted_span(
            actor=actor, aspect=aspect_text, fate_points_after=after, _tracer=_tracer
        )
        return after

    # --- d20/beat surface: not Fate's paradigm (fail loud until F5 re-cut) ---

    def find_confrontation(self, confrontations, encounter_type):
        raise NotImplementedError(_NO_D20_SURFACE)

    def stat_modifier(self, stats, stat_check):
        raise NotImplementedError(_NO_D20_SURFACE)

    def compute_dc(self, beat):
        raise NotImplementedError(_NO_D20_SURFACE)

    def apply_beat(self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver):
        raise NotImplementedError(_NO_D20_SURFACE)

    def resolve_damage(self, *, beat, actor_core, pack, world_slug=None):
        raise NotImplementedError(_NO_D20_SURFACE)

    def attack_params(self, *, beat, attacker_stats, attacker_core, target_core):
        raise NotImplementedError(_NO_D20_SURFACE)
