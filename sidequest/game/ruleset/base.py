"""The RulesetModule seam — a genre pack binds one module that owns turn resolution.

Spec 0 surface only: the five resolution operations the native turn already performs.
Character-shape / advancement / narrator-contract surfaces are added when the SWN
module plan needs them (YAGNI).
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from sidequest.game.ruleset.resolution import AttackRollParams, JumpAdjudication
from sidequest.genre.models.rules import BeatDef, ConfrontationDef
from sidequest.protocol.models import InitiativeEntry

if TYPE_CHECKING:
    from sidequest.game.beat_kinds import ApplyResult
    from sidequest.game.lethality import DownedResult, LethalityResult
    from sidequest.game.table.types import TableCommit, TableResolutionOutcome, TableState
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.world import Route


class UnknownRulesetError(ValueError):
    """Raised at pack load when rules.ruleset names no registered module. Fail loud."""


class RulesetModule(ABC):
    """Authority for how a turn resolves. One bound per session; no fallback between modules."""

    #: registry key, also the value authors write in rules.yaml `ruleset:`
    slug: str

    @property
    def awards_native_turn_xp(self) -> bool:
        """Whether the ADR-021 native per-turn XP tick (``award_turn_xp``)
        applies under this ruleset.

        ``True`` for the native dial engine, whose four-track progression
        grants 10 (calm) / 25 (combat) XP every turn. The Without Number family
        overrides this to ``False``: WN uses small-integer, GM-awarded
        expedition/goal XP (WWN L2 ≈ 3 XP), NOT a continuously-ticking
        OSR/D&D-scale counter. Without this gate a WWN-bound L1 Warrior ticked
        to 135 XP with no award event (sq-playtest 2026-06-13, beneath_sunden).
        See ``.pennyfarthing/sidecars/gm-decisions.md`` (2026-06-13: the WWN
        SRD is the authority for any WWN-bound mechanical value)."""
        return True

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
    def apply_beat(
        self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver
    ) -> ApplyResult:
        """Apply a resolved beat's deltas to the encounter. Returns the engine ApplyResult."""

    @abstractmethod
    def resolve_damage(self, *, beat, actor_core, pack, world_slug=None) -> DamageSpec | None:
        """Resolve the DamageSpec for a strike beat (weapon or override), or None.

        ``world_slug`` drives the world-tier item-catalog resolution (epic 94:
        world inventory REPLACES genre); None falls through to the genre tier.
        """

    @abstractmethod
    def attack_params(
        self,
        *,
        beat: BeatDef,
        attacker_stats: dict[str, int],
        attacker_core: object | None,
        target_core: object | None,
    ) -> AttackRollParams:
        """Modifier + target number for one attack. native: stat mod vs beat DC.
        SWN: attack_bonus + skill + attr-mod vs target AC."""

    def offer_difficulty(self, *, beat: BeatDef, target_core: object | None) -> int:
        """The pre-roll target number to advertise on the beat OFFER (Story 97-3).

        This is the number the TARGET banner shows the player BEFORE they
        roll, so it MUST equal the ``attack_params(...).target_number``
        resolution will later use — the server is the only DC author; the
        client renders this and computes nothing. Unlike ``attack_params``
        it needs no attacker stats (the target number never depends on the
        attacker in any module), so the offer path can author it with only
        the beat + the opposing side's core in hand.

        Default: the ruleset's beat DC (native dial formula). SWN-family
        overrides with the target's armor class.
        """
        return self.compute_dc(beat)

    def ship_attack_params(
        self,
        *,
        attacker_stats: dict[str, int],
        pilot_skill: int,
        attack_bonus: int,
        geometry_modifier: int,
        target_ac: int,
        cfg,
    ) -> AttackRollParams:
        """Modifier + target number for one ship-gunnery shot (dogfight SWN layer)."""
        raise NotImplementedError(f"{self.slug} ruleset has no ship-gunnery resolution")

    def adjudicate_jump(
        self,
        *,
        route: Route | None,
        drive_rating: int,
        rng: random.Random,
    ) -> JumpAdjudication:
        """Adjudicate one campaign-scale inter-system jump (Story 98-5, ADR-141).

        ``route`` is the authored ``routes`` entry for the edge, or None for a
        bare adjacency (the ruleset computes its own default). ``drive_rating``
        is the ship's spike-drive rating; ``rng`` rolls the hazard check.

        Base default fails loud (mirrors ``ship_attack_params``): a ruleset with
        no jump model declines rather than inventing a cost. The slug appears in
        the message so the GM/dev sees which module had no inter-system drive
        model. SWN overrides with the spike-drive computation."""
        raise NotImplementedError(f"{self.slug} ruleset has no inter-system jump adjudication")

    def resolve_opponent_attack(
        self,
        *,
        attacker_stats: dict[str, int],
        stat_check: str,
        attack_bonus: int,
        combat_skill: int,
        target_ac: int,
        d20: int,
    ):
        """The enemy turn for hp_depletion combat: opponent rolls vs player AC.
        Ruleset-specific (SWN); rulesets whose combat is dial/opposed_check (e.g.
        native) resolve the opponent through the opposed-check branch instead and
        never call this."""
        raise NotImplementedError(f"{self.slug} ruleset has no server-driven enemy-attack turn")

    def check_params(
        self,
        *,
        stats,
        attribute,
        skill_level,
        difficulty_key,
        label,
        cfg,
        character_core: object | None = None,
    ):
        raise NotImplementedError(f"{self.slug} ruleset has no non-beat skill-check resolution")

    def save_params(self, *, stats, save, level, label, cfg, character_core: object | None = None):
        raise NotImplementedError(f"{self.slug} ruleset has no saving-throw resolution")

    def roll_initiative(
        self,
        *,
        actor_dex_scores: dict[str, int],
        rng: random.Random,
    ) -> list[InitiativeEntry] | None:
        """Resolution order (descending) for a confrontation, or None for no ordering.

        Default: None (no turn ordering). SWN overrides with 1d8 + DEX mod.
        `actor_dex_scores` maps actor name -> raw DEX score; the dispatch seam
        resolves it (PC from Character.stats, opponent from the dexterity
        reserved key) because CreatureCore carries no ability scores.
        """
        return None

    def resolve_trauma(
        self, *, spec, base_total, cfg, rng, actor="", _tracer=None
    ) -> LethalityResult:
        """Per-hit lethality multiplier. Default: identity passthrough (no Trauma).

        Only CWN overrides. Returns a LethalityResult. Imported lazily to avoid
        a base→game.lethality dependency at module import for the lean rulesets."""
        from sidequest.game.lethality import LethalityResult

        return LethalityResult(
            base_total=base_total,
            final_total=base_total,
            traumatic=False,
            trauma_roll=0,
            trauma_target=0,
        )

    def resolve_shock(self, *, spec, target_melee_ac, actor="", _tracer=None) -> int:
        """Chip damage applied on a MISS. Default: 0 (no Shock). Only CWN overrides."""
        return 0

    def resolve_downed(
        self, *, core, save_target, scene_traumatic, cfg, rng, _tracer=None
    ) -> DownedResult | None:
        """Resolve a 0-HP character. Default: no special consequence (None).

        CWN overrides to declare Mortal Injury and (if a Traumatic Hit landed
        this scene) roll the Major Injury table. Returns DownedResult | None."""
        return None

    def resolve_hacking(
        self, *, verb, tier, base_dc, alert_modifier, outcome, actor="", _tracer=None
    ) -> int:
        """CWN cyberspace security check. Default: compute the effective DC,
        emit nothing (parallels resolve_shock returning 0). Only CWN overrides
        to emit cwn.hacking.security_check."""
        return int(base_dc) + int(alert_modifier)

    def deal_table(self, state: TableState, *, rng: random.Random) -> None:
        """Seat-deal an N-seat table (poker / auction). Genre-general; the
        per-kind deal is dispatched through the table-game registry. Concrete
        here (not abstract) because table resolution is orthogonal to combat
        resolution — every ruleset inherits it. See game/table/engine.py."""
        from sidequest.game.table.engine import deal_table as _deal

        _deal(state, rng=rng)

    def resolve_table(
        self,
        state: TableState,
        *,
        commits: dict[str, TableCommit],
        rng: random.Random,
    ) -> TableResolutionOutcome:
        """Resolve one decision point of an N-seat table. Delegates to the
        generic engine; kind-specifics dispatch through the registry. Concrete
        on the base — table resolution is orthogonal to combat resolution, so
        every ruleset inherits it. See game/table/engine.py."""
        from sidequest.game.table.engine import resolve_table as _resolve

        return _resolve(state, commits=commits, rng=rng)
