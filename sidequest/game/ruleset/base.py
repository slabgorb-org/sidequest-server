"""The RulesetModule seam — a genre pack binds one module that owns turn resolution.

Spec 0 surface only: the five resolution operations the native turn already performs.
Character-shape / advancement / narrator-contract surfaces are added when the SWN
module plan needs them (YAGNI).
"""

from __future__ import annotations

import json
import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from sidequest.game.ruleset.resolution import AttackRollParams, JumpAdjudication
from sidequest.genre.models.rules import BeatDef, ConfrontationDef
from sidequest.protocol.models import InitiativeEntry

if TYPE_CHECKING:
    from sidequest.game.beat_kinds import ApplyResult
    from sidequest.game.builder import AccumulatedChoices
    from sidequest.game.lethality import DownedResult, LethalityResult
    from sidequest.game.table.types import TableCommit, TableResolutionOutcome, TableState
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.world import Route


# Legacy D&D 5e standard array, used when a pack authors `stat_generation:
# standard_array` (or `standard_array_arrange`, ADR-143) but does not author its
# own `rules.standard_array`. Single source of truth for both the non-arrange
# path (generate_attributes, below) and the builder's arrange-pool seeder.
_DEFAULT_STANDARD_ARRAY: list[int] = [15, 14, 13, 12, 10, 8]


def _roll_3d6_stats(ability_names: list[str], rng: random.Random) -> list[tuple[str, int]]:
    """Roll 3d6 for each ability score in order. Returns ``(name, total)``
    pairs in ``ability_names`` order, emitting one ``SPAN_CHARGEN_STAT_ROLL``
    per stat.

    Extracted from CharacterBuilder._roll_3d6_stats (ADR-143) so both the
    builder and the ABC default generate_attributes share one implementation.
    Uses the passed seedable RNG so tests can drive deterministic outputs.
    """
    from sidequest.telemetry.spans import SPAN_CHARGEN_STAT_ROLL, Emitter

    results: list[tuple[str, int]] = []
    for name in ability_names:
        dice = (rng.randint(1, 6), rng.randint(1, 6), rng.randint(1, 6))
        total = sum(dice)
        Emitter.fire(
            SPAN_CHARGEN_STAT_ROLL,
            {
                "stat": name,
                "dice": list(dice),
                "total": total,
            },
        )
        results.append((name, total))
    return results


def _allocate_point_buy(n: int, budget: int) -> list[int]:
    """Allocate a point-buy budget across `n` stats.

    All stats start at 8. Points distributed round-robin, raising each
    stat by 1 at a time (cheapest-first) until budget is spent. No
    stat can exceed 15. Cost table (cumulative from 8):
      8→9..12: 1pt each; 13→14..15: 2pt each.

    Extracted from CharacterBuilder._allocate_point_buy (ADR-143 Step 1)
    so both the ABC default and future overrides can call it.
    """

    def marginal_cost(value: int) -> int:
        if 9 <= value <= 13:
            return 1
        if value in (14, 15):
            return 2
        # Outside [9, 15] — effectively infinite; callers filter via
        # the next_val > 15 guard before reaching this branch.
        return 1 << 30

    stats = [8] * n
    remaining = budget
    while True:
        any_raised = False
        for i in range(n):
            next_val = stats[i] + 1
            if next_val > 15:
                continue
            cost = marginal_cost(next_val)
            if cost <= remaining:
                stats[i] = next_val
                remaining -= cost
                any_raised = True
        if not any_raised or remaining == 0:
            break
    return stats


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

    def seed_chargen_resources(self, *, rules, stats, class_def):
        """Effort/spellcasting/system-strain seeded at chargen. Default: none.
        Only WithoutNumberRulesetModule overrides (Effort + Strain are WN-family).
        Returns ChargenResources. Imported lazily to keep lean rulesets free of
        the wwn_magic/system_strain import at module load."""
        from sidequest.game.chargen_contribution import ChargenResources
        return ChargenResources()

    def contribute_background_skills(self, *, background_def, rng: random.Random) -> dict[str, int]:
        """Background → skill-level dict. Default: none. WN-core reads the def."""
        return {}

    def contribute_foci(self, *, focus_defs):
        """Foci → skills + abilities. Default: none. WN-core reads the defs."""
        from sidequest.game.chargen_contribution import FociContribution
        return FociContribution()

    def _generate_attribute_values(
        self,
        *,
        method: str,
        ability_names: list[str],
        standard_array: list[int] | None,
        point_buy_budget: int,
        rolled_stats: list[tuple[str, int]] | None,
        rng: random.Random,
    ) -> list[int]:
        """Produce the raw attribute value pool for the given generation method.

        Returns a list of ints in the pool's native order for every path — no
        sorting happens here. The roll paths (3d6/bones) return values in
        ability_names order via rolled_stats; standard_array returns the array
        exactly as authored; point_buy returns the allocator's order. Any
        sorting/placement is the responsibility of the assign_attributes
        implementation that consumes this pool (the WN override sorts descending
        for prime-aware placement; the base default zips in declaration order).

        Extracted from generate_attributes (ADR-143 Task 4) so the value-generation
        logic is reusable across the base and any override that wants to call super()
        value-gen then do its own assignment.
        """
        if method == "roll_3d6_strict":
            if rolled_stats is not None:
                # Return values in ability_names order (paired with names).
                return [v for _, v in rolled_stats]
            else:
                # Defensive re-roll — shouldn't fire in practice because
                # the eager construction roll covers this path.
                return [v for _, v in _roll_3d6_stats(ability_names, rng)]

        elif method == "roll_the_bones":
            if rolled_stats is None:
                # No silent re-roll: the mode rolls eagerly at adoption, so
                # a missing array is a programmer error, not a fallback case.
                raise RuntimeError(
                    "roll_the_bones mode active but no rolled stats recorded — "
                    "_enter_roll_the_bones must run at mode adoption"
                )
            return [v for _, v in rolled_stats]

        elif method == "standard_array":
            # ADR-142 Step 2A: pack-authored array overrides the legacy
            # D&D 5e default when set; None preserves existing behavior.
            return (
                list(standard_array)
                if standard_array is not None
                else list(_DEFAULT_STANDARD_ARRAY)
            )

        elif method == "point_buy":
            return _allocate_point_buy(len(ability_names), point_buy_budget)

        else:
            from sidequest.game.builder import UnknownStatGenerationError

            raise UnknownStatGenerationError(method=method)

    def assign_attributes(
        self,
        *,
        pool: list[int],
        ability_names: list[str],
        class_def: object | None,
        acc: AccumulatedChoices | None = None,
    ) -> dict[str, int]:
        """Place pool values onto stats. Default: declaration order (historical),
        plus the standard-array hint-derivation heuristic when acc is provided.

        WN-core overrides with prime-aware placement (ADR-143 DD-4: heuristic
        stays native-only; the WN override supersedes it entirely).

        NOTE: acc is needed here ONLY for the hint-derivation heuristic that gates
        on ``not acc.stat_bonuses`` and ``method == "standard_array"``. Since this
        default runs on all non-WN rulesets (native), the heuristic is preserved
        verbatim. The WN override ignores acc — prime placement is unconditional
        when class_def is provided.

        IMPORTANT: acc.stat_bonuses are applied by generate_attributes AFTER this
        call returns, not inside assign_attributes. The hint-derivation guard
        ``not acc.stat_bonuses`` is safe because bonuses haven't been applied yet
        at call time — acc carries the *authored* bonus set, not post-apply values.
        """
        stats = dict(zip(ability_names, pool, strict=False))

        # Standard-array derivation: when no explicit bonuses were
        # authored and we have at least 3 stats, differentiate the
        # spread using accumulated hints.
        # This block is NATIVE/DEFAULT only — WN overrides assign_attributes
        # entirely and never reaches this branch.
        if (
            acc is not None
            and not acc.stat_bonuses
            and len(ability_names) >= 3
        ):
            names = ability_names
            # Origin/race → boost first stat
            if acc.race_hint is not None:
                stats[names[0]] = stats[names[0]] + 3
            # Mutation/affinity → boost second stat, reduce last
            if acc.mutation_hint is not None or acc.affinity_hint is not None:
                stats[names[1]] = stats[names[1]] + 2
                stats[names[-1]] = stats[names[-1]] - 1
            # Class/training → boost third stat (floor at last index if
            # fewer than 3 names, though the guard above already rejects
            # that case).
            if acc.class_hint is not None or acc.training_hint is not None:
                idx = min(2, len(names) - 1)
                stats[names[idx]] = stats[names[idx]] + 2

        return stats

    def generate_attributes(
        self,
        *,
        method: str,
        ability_names: list[str],
        standard_array: list[int] | None,
        point_buy_budget: int,
        rolled_stats: list[tuple[str, int]] | None,
        acc: AccumulatedChoices,
        rng: random.Random,
        class_def: object | None = None,
    ) -> dict[str, int]:
        """Generate the ability-score dict per `method`. Splits into value-gen
        (_generate_attribute_values) + assignment (assign_attributes) so
        WithoutNumberRulesetModule can override only the assignment step.

        acc.stat_bonuses are applied HERE (after assign_attributes returns) so
        every ruleset gets them regardless of assignment strategy. The hint-
        derivation heuristic lives in the DEFAULT assign_attributes; the WN
        override supersedes it with prime-aware placement (ADR-143 Task 4).

        class_def is forwarded to assign_attributes. CharacterBuilder resolves
        it from the builder's class roster + acc.class_hint before delegating."""
        from sidequest.telemetry.spans import SPAN_CHARGEN_STATS_GENERATED, Emitter

        pool = self._generate_attribute_values(
            method=method,
            ability_names=ability_names,
            standard_array=standard_array,
            point_buy_budget=point_buy_budget,
            rolled_stats=rolled_stats,
            rng=rng,
        )

        # The hint-derivation heuristic only fires on standard_array when no
        # explicit stat bonuses were set. Pass the method context via acc
        # introspection: the heuristic already guards ``not acc.stat_bonuses``
        # and the native default gates on standard_array via the caller context.
        # For the heuristic to fire correctly on non-standard_array paths we
        # add an explicit guard here — pass acc only when method == standard_array.
        assign_acc = acc if method == "standard_array" else None

        stats = self.assign_attributes(
            pool=pool,
            ability_names=ability_names,
            class_def=class_def,
            acc=assign_acc,
        )

        # Apply explicit stat bonuses from chargen choices (origin,
        # mutation, artifact). Applied AFTER assignment so the bonuses
        # stack on top of the placed values, not the pool values.
        for stat, bonus in acc.stat_bonuses.items():
            if stat in stats:
                stats[stat] += bonus

        Emitter.fire(
            SPAN_CHARGEN_STATS_GENERATED,
            {
                "method": method,
                "stat_count": len(stats),
                "stats_json": json.dumps(dict(stats), sort_keys=True),
            },
        )
        return stats

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
        self,
        *,
        core,
        save_target,
        scene_traumatic,
        cfg,
        rng,
        created_turn=0,
        created_in_encounter=None,
        superseded_by_terminal=False,
        _tracer=None,
    ) -> DownedResult | None:
        """Resolve a 0-HP character. Default: no special consequence (None).

        CWN/WWN override to declare Mortal Injury and (if a Traumatic Hit landed
        this scene) roll the Major Injury table. The ``created_turn`` /
        ``created_in_encounter`` provenance and ``superseded_by_terminal`` flag
        are consumed by the WN override (#239); the default ignores them.
        Returns DownedResult | None."""
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
