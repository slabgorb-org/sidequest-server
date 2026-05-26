"""The RulesetModule seam — a genre pack binds one module that owns turn resolution.

Spec 0 surface only: the five resolution operations the native turn already performs.
Character-shape / advancement / narrator-contract surfaces are added when the SWN
module plan needs them (YAGNI).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.genre.models.rules import BeatDef, ConfrontationDef


class UnknownRulesetError(ValueError):
    """Raised at pack load when rules.ruleset names no registered module. Fail loud."""


class RulesetModule(ABC):
    """Authority for how a turn resolves. One bound per session; no fallback between modules."""

    #: registry key, also the value authors write in rules.yaml `ruleset:`
    slug: str

    @abstractmethod
    def find_confrontation(
        self, confrontations: list[ConfrontationDef], encounter_type: str
    ) -> ConfrontationDef | None:
        """Locate the ConfrontationDef governing this encounter type."""

    @abstractmethod
    def stat_modifier(self, stats: dict[str, int], stat_check: str) -> int:
        """The check modifier this ruleset derives from an ability/skill value."""

    @abstractmethod
    def compute_dc(self, beat: BeatDef) -> int:
        """The difficulty class / target number this ruleset assigns a beat."""

    @abstractmethod
    def apply_beat(self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver):
        """Apply a resolved beat's deltas to the encounter. Returns the engine ApplyResult."""

    @abstractmethod
    def resolve_damage(self, *, beat, actor_core, pack):
        """Resolve the DamageSpec for a strike beat (weapon or override), or None."""

    @abstractmethod
    def attack_params(
        self,
        *,
        beat: BeatDef,
        attacker_stats: dict[str, int],
        attacker_core: object | None,
        target_core: object | None,
    ) -> "AttackRollParams":
        """Modifier + target number for one attack. native: stat mod vs beat DC.
        SWN: attack_bonus + skill + attr-mod vs target AC."""
