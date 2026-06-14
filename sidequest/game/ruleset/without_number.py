"""WithoutNumberRulesetModule — the shared "Without Number" resolution core.

The four WN-family modules (SWN/WWN/CWN/AWN) share Kevin Crawford's Without
Number resolution engine: the d20 attack vs ascending AC, the 2d6 skill ladder,
the d20 saving throws (three attribute saves + the WWN/CWN/AWN Luck save), 1d8
initiative, the Effort economy (SRD §1.4.4 / §6), and the WWN/CWN/AWN lethality
stack (Shock, Trauma, System Strain, Mortal Injury, Major Injury).

This module is the single home for that core (ADR-142). It sits BETWEEN the
``RulesetModule`` ABC and the four siblings: it overrides the ABC's no-op
lethality defaults with the real WN implementations, and the siblings reparent
onto it. It is abstract-by-convention — it declares ``slug: str`` but is never
registered directly; every concrete subclass sets its own slug, and all spans
namespace by ``self.slug`` so a wwn pack emits ``wwn.*`` and an awn pack
``awn.*`` (the slug-honesty invariant — CLAUDE.md OTEL Observability Principle).

The lethality methods guard ``isinstance(cfg, (CwnConfig, WwnConfig))`` (DD-2):
those two configs (and ``AwnConfig(CwnConfig)`` transitively) carry the
``trauma`` / ``system_strain`` blocks the methods read. SWN carries no such
config, so an SWN pack that reached these methods would raise loudly — which it
never does (SWN authors no lethality surface).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.lethality import DownedResult, LethalityResult, major_injury_entry
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.resolution import (
    AttackRollParams,
    CheckRollParams,
    OpponentAttackOutcome,
)
from sidequest.game.status import Status, StatusSeverity, status_roll_modifier
from sidequest.game.system_strain import StrainResult
from sidequest.game.wwn_magic import (
    DisciplineActivationResult,
    EffortCommitment,
    EffortDuration,
    EffortResult,
)
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.psionics import PsionicDiscipline
from sidequest.genre.models.rules import BeatDef, CwnConfig, SwnConfig, WwnConfig
from sidequest.protocol.models import InitiativeEntry
from sidequest.telemetry.spans.psionics import (
    discipline_activated_span,
    effort_commit_span,
    effort_reclaim_span,
)
from sidequest.telemetry.spans.wn import (
    chargen_attributes_assigned_span,
    major_injury_roll_span,
    mortal_injury_declared_span,
    shock_applied_span,
    system_strain_delta_span,
    trauma_roll_span,
)

if TYPE_CHECKING:
    from sidequest.genre.models.character import ClassDef

# Source key for the WN psionic Effort pool. WN psionics draw every discipline
# from ONE Effort pool (SWN SRD §6), so the pool keys ``core.effort`` under this
# slug. Homed on the WN core (ADR-142) because the psionic surface is shared by
# every WN sibling that ships a discipline catalog (e.g. swn space_opera AND wwn
# heavy_metal — see test_psionics_dispatch_wiring_102_6), not SWN-only.
PSIONIC_EFFORT_SOURCE = "psionic"


def swn_attribute_modifier(score: int) -> int:
    """SWN Revised tight curve (NOT D&D's (score-10)//2).

    3→-2, 4-7→-1, 8-13→0, 14-17→+1, 18→+2
    """
    if score <= 3:
        return -2
    if score <= 7:
        return -1
    if score <= 13:
        return 0
    if score <= 17:
        return 1
    return 2


def _stat(stats: dict[str, int], key: str) -> int:
    """Look up a stat score by exact or case-insensitive key. Fail loud if absent (no neutral-10)."""
    v = stats.get(key)
    if v is not None:
        return v
    for k, val in stats.items():
        if k.upper() == key.upper():
            return val
    raise KeyError(
        f"stat {key!r} not in stat block {sorted(stats)} — content/attribute_map bug "
        "(SWN module no longer falls back to a neutral 10)"
    )


class WithoutNumberRulesetModule(RulesetModule):
    #: set by every concrete subclass; the core itself is never registered.
    slug: str

    @property
    def awards_native_turn_xp(self) -> bool:
        """The Without Number family (SWN/WWN/CWN/AWN) does NOT use the native
        ADR-021 per-turn XP tick. WN advancement is small-integer, GM-awarded
        expedition/goal XP — not an OSR/D&D-scale per-turn counter. Suppress
        ``award_turn_xp`` under any WN binding (sq-playtest 2026-06-13: a WWN L1
        Warrior ticked to 135 XP). All four concrete WN siblings inherit this."""
        return False

    # SWN save categories → the two attributes whose better modifier applies (SRD p.46).
    _SAVE_ATTRS = {
        "physical": ("STRENGTH", "CONSTITUTION"),
        "evasion": ("DEXTERITY", "INTELLIGENCE"),
        "mental": ("WISDOM", "CHARISMA"),
    }

    def find_confrontation(self, confrontations, encounter_type):
        from sidequest.server.dispatch.confrontation import find_confrontation_def

        return find_confrontation_def(confrontations, encounter_type)

    def stat_modifier(self, stats: dict[str, int], stat_check: str) -> int:
        return swn_attribute_modifier(_stat(stats, stat_check))

    def compute_dc(self, beat) -> int:
        raise NotImplementedError(
            "SWN resolves attacks vs target AC via attack_params; compute_dc is native-only."
        )

    def offer_difficulty(self, *, beat: BeatDef, target_core: object | None) -> int:
        """SWN attacks resolve vs the target's armor class — advertise exactly
        that on the beat offer (Story 97-3: server is the only DC author).
        Single-sourced with ``attack_params`` below, so the TARGET banner and
        the resolution can never disagree."""
        return int(getattr(target_core, "armor_class", 10)) if target_core is not None else 10

    def attack_params(
        self, *, beat, attacker_stats, attacker_core, target_core
    ) -> AttackRollParams:
        attr_mod = self.stat_modifier(attacker_stats, beat.stat_check)
        combat_skill = int(getattr(beat, "combat_skill", 0) or 0)
        attack_bonus = int(getattr(beat, "attack_bonus", 0) or 0)
        status_mod = status_roll_modifier(attacker_core)
        return AttackRollParams(
            modifier=attack_bonus + combat_skill + attr_mod + status_mod,
            target_number=self.offer_difficulty(beat=beat, target_core=target_core),
        )

    def resolve_opponent_attack(
        self,
        *,
        attacker_stats: dict[str, int],
        stat_check: str,
        attack_bonus: int,
        combat_skill: int,
        target_ac: int,
        d20: int,
    ) -> OpponentAttackOutcome:
        """The enemy turn (SWN beat_selection hp_depletion combat): the opponent
        rolls d20 + attack_bonus + combat_skill + attribute mod vs the player's
        AC. Symmetric to ``attack_params`` but with the opponent as attacker and
        a concrete d20 supplied by the caller (server-rolled). Pure — the caller
        applies damage + emits OTEL."""
        attr_mod = self.stat_modifier(attacker_stats, stat_check)
        modifier = int(attack_bonus) + int(combat_skill) + attr_mod
        total = int(d20) + modifier
        return OpponentAttackOutcome(
            hit=total >= int(target_ac),
            attack_total=total,
            modifier=modifier,
            d20=int(d20),
            target_ac=int(target_ac),
        )

    def apply_beat(self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver):
        from sidequest.game.beat_kinds import apply_beat as _engine_apply_beat

        return _engine_apply_beat(
            encounter,
            actor,
            beat,
            outcome,
            turn=turn,
            edge_resolver=edge_resolver,
            damage_resolver=damage_resolver,
        )

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
    ) -> CheckRollParams:
        if attribute is None:
            raise ValueError(
                "check_params requires a non-None attribute; "
                "CheckThrowPayload validator should have caught this upstream"
            )
        attr_mod = self.stat_modifier(stats, attribute)
        return CheckRollParams(
            sides=6,
            count=2,
            modifier=attr_mod + int(skill_level) + status_roll_modifier(character_core),
            difficulty=int(cfg.difficulties[difficulty_key]),
            label=label,
        )

    def save_params(
        self, *, stats, save, level, label, cfg, character_core: object | None = None
    ) -> CheckRollParams:
        # Luck save (WWN/CWN/AWN): no attribute modifier; target = save_base -
        # (level - 1) (DD-3). Hoisted into the core so AWN reparents cleanly;
        # SWN inherits it but never authors a `luck` save category (unreachable).
        if save == "luck":
            return CheckRollParams(
                sides=20,
                count=1,
                modifier=status_roll_modifier(character_core),
                difficulty=int(cfg.save_base) - (int(level) - 1),
                label=label,
            )
        if save not in self._SAVE_ATTRS:
            raise ValueError(
                f"unknown save category {save!r}, expected one of {list(self._SAVE_ATTRS)}"
            )
        amap = cfg.attribute_map
        flavor_attrs = []
        for swn_attr in self._SAVE_ATTRS[save]:
            flavor = amap.get(swn_attr)
            if flavor is None:
                raise KeyError(
                    f"attribute_map missing {swn_attr!r} for save {save!r} "
                    "(RulesConfig validator should have caught this)"
                )
            flavor_attrs.append(flavor)
        best_mod = max(self.stat_modifier(stats, f) for f in flavor_attrs)
        return CheckRollParams(
            sides=20,
            count=1,
            modifier=best_mod + status_roll_modifier(character_core),
            difficulty=int(cfg.save_base)
            - (int(level) - 1),  # target; SRD p.46: 15 at level 1, -1/level
            label=label,
        )

    def resolve_damage(self, *, beat, actor_core, pack, world_slug=None):
        from sidequest.server.dispatch.damage_roll import resolve_damage_spec_from_beat_and_actor

        return resolve_damage_spec_from_beat_and_actor(
            beat=beat, actor_core=actor_core, pack=pack, world_slug=world_slug
        )

    def roll_initiative(
        self,
        *,
        actor_dex_scores: dict[str, int],
        rng: random.Random,
    ) -> list[InitiativeEntry] | None:
        """SWN initiative: 1d8 + DEX modifier per actor, sorted descending.

        Faithful SWN (SRD): rolled once at combat start; the seam persists the
        result and reuses it each round. Tie-break: stable sort preserves the
        caller's actor order (TODO: confirm SRD tie-break and pin to SwnConfig).
        """
        entries = [
            InitiativeEntry(
                token_id=name,
                value=rng.randint(1, 8) + swn_attribute_modifier(score),
            )
            for name, score in actor_dex_scores.items()
        ]
        entries.sort(key=lambda e: e.value, reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Lethality stack (WWN/CWN/AWN SRD) — Shock, Trauma, System Strain,
    # Mortal Injury, Major Injury. Hoisted to the WN core (ADR-142) from the
    # canonical WWN copy; the config guard is broadened to (CwnConfig, WwnConfig)
    # (DD-2) and the spans are slug-parameterized via ``self.slug`` (DD-4) so an
    # awn pack emits ``awn.*`` rather than the mislabelled ``cwn.*``.
    # ------------------------------------------------------------------

    def resolve_shock(
        self,
        *,
        spec: DamageSpec,
        target_melee_ac: int,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """WN Shock ("Shock X/AC Y"): a melee weapon with shock>0 chips `shock`
        damage on a MISS when the target's Melee AC <= `spec.shock_ac`. Returns
        the chip damage (0 when not applicable). Emits {slug}.shock.applied only
        when damage is actually chipped."""
        if spec.shock <= 0 or spec.shock_ac is None or target_melee_ac > spec.shock_ac:
            return 0
        shock_applied_span(
            ruleset=self.slug,
            actor=actor,
            amount=spec.shock,
            melee_ac=target_melee_ac,
            shock_rating=spec.shock,
            shock_ac=spec.shock_ac,
            _tracer=_tracer,
        )
        return spec.shock

    def resolve_trauma(
        self,
        *,
        spec: DamageSpec,
        base_total: int,
        cfg: SwnConfig | None,
        rng: random.Random,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> LethalityResult:
        """WN Trauma: if the weapon has a Trauma Die, roll it; on a result that
        meets/exceeds the Trauma Target, multiply total damage by trauma_rating.
        Emits {slug}.trauma.roll on every WN strike that has a trauma_die."""
        if spec.trauma_die is None:
            return LethalityResult(
                base_total=base_total,
                final_total=base_total,
                traumatic=False,
                trauma_roll=0,
                trauma_target=0,
            )
        if not isinstance(cfg, (CwnConfig, WwnConfig)):
            raise ValueError(
                f"resolve_trauma requires a CwnConfig/WwnConfig; got {type(cfg).__name__!r}"
            )
        target = (
            spec.trauma_target
            if spec.trauma_target is not None
            else cfg.trauma.default_trauma_target
        )
        trauma_roll = DamageSpec(dice=spec.trauma_die).roll(rng)
        traumatic = trauma_roll >= target
        final = base_total * spec.trauma_rating if traumatic else base_total
        trauma_roll_span(
            ruleset=self.slug,
            actor=actor,
            weapon_die=spec.trauma_die,
            roll=trauma_roll,
            target=target,
            traumatic=traumatic,
            rating=spec.trauma_rating,
            base=base_total,
            final=final,
            _tracer=_tracer,
        )
        return LethalityResult(
            base_total=base_total,
            final_total=final,
            traumatic=traumatic,
            trauma_roll=trauma_roll,
            trauma_target=target,
        )

    def apply_system_strain(
        self,
        *,
        core: CreatureCore,
        kind: str,
        amount: int,
        source: str,
        cfg: SwnConfig | None,
        _tracer: trace.Tracer | None = None,
    ) -> StrainResult:
        """Apply a System Strain change under WN rules; emit {slug}.system_strain.delta.

        kind: "temporary" | "first_aid" | "permanent" | "rest". Over-max
        temporary/permanent adds are REFUSED (applied=False). Rest recovers
        toward (never below) the permanent floor. Fails loud if the character has
        no strain pool or kind is unknown.
        """
        pool = core.system_strain
        if pool is None:
            raise ValueError(
                f"{core.name!r} has no system_strain pool; wwn characters must seed one at chargen"
            )
        if not isinstance(cfg, (CwnConfig, WwnConfig)):
            raise ValueError(
                f"apply_system_strain requires a CwnConfig/WwnConfig; got {type(cfg).__name__!r}"
            )
        scfg = cfg.system_strain

        before = pool.current
        applied = True
        reason = ""
        requested: int = 0

        if kind == "first_aid":
            requested = scfg.first_aid_cost
            new_current = pool.current + requested
            if new_current > pool.max:
                applied = False
                reason = f"would exceed max ({pool.max})"
                new_current = pool.current
        elif kind == "temporary":
            requested = amount
            new_current = pool.current + amount
            if new_current > pool.max:
                applied = False
                reason = f"would exceed max ({pool.max})"
                new_current = pool.current
        elif kind == "permanent":
            requested = amount
            new_perm = max(0, pool.permanent + amount)
            new_current = pool.current + amount
            if amount > 0 and new_current > pool.max:
                applied = False
                reason = f"would exceed max ({pool.max})"
                new_current = pool.current
                new_perm = pool.permanent
            else:
                new_current = max(new_perm, new_current)
            if applied:
                pool.permanent = new_perm
        elif kind == "rest":
            requested = -scfg.rest_recovery_per_night * max(0, amount)
            new_current = max(pool.permanent, pool.current + requested)
        else:
            raise ValueError(f"unknown system_strain kind {kind!r}")

        if applied:
            pool.current = new_current
        delta = pool.current - before

        system_strain_delta_span(
            ruleset=self.slug,
            actor=core.name,
            source=source,
            amount=requested,
            new_total=pool.current,
            max=pool.max,
            applied=applied,
            _tracer=_tracer,
        )
        return StrainResult(
            applied=applied,
            current=pool.current,
            max=pool.max,
            permanent=pool.permanent,
            delta=delta,
            reason=reason,
        )

    def resolve_downed(
        self,
        *,
        core: CreatureCore,
        save_target: int,
        scene_traumatic: bool,
        cfg: SwnConfig | None,
        rng: random.Random,
        created_turn: int = 0,
        created_in_encounter: str | None = None,
        superseded_by_terminal: bool = False,
        _tracer: trace.Tracer | None = None,
    ) -> DownedResult:
        """Resolve a WN character dropped to 0 HP.

        Declares a Mortal Injury (Scar status; dies at the end of
        cfg.trauma.mortal_injury_rounds unless stabilized) stamped with the
        caller's ``created_turn`` / ``created_in_encounter`` provenance. If a
        Traumatic Hit landed this scene, additionally rolls a Physical save
        (1d20 vs save_target); on failure, rolls 1d12 on the Major Injury table
        and attaches a second Scar. Emits {slug}.mortal_injury.declared and
        (when rolled) {slug}.major_injury.roll.

        ``superseded_by_terminal`` (sq-playtest #239 death dual-status): when the
        genre lethality policy has ALREADY ruled this actor terminally dead (an
        ``incapacitating`` "Downed — ... (mortally wounded)" status is present),
        a coexisting non-incapacitating "dies in N rounds unless stabilized"
        window is a CONTRADICTORY second status — terminal-dead vs. stabilizable.
        The real WWN dying window is deferred to story 106-5 (and is unactionable
        in solo per .pennyfarthing/sidecars/gm-decisions.md). So we SUPERSEDE:
        the WN lethality span still fires (GM-panel lie-detector — WN lethality
        IS engaged), carrying ``superseded_by_terminal=True``, but the
        contradictory window status (and any Major Injury scar) is NOT appended.
        A terminally-dead PC then shows exactly ONE coherent status.
        """
        if not isinstance(cfg, (CwnConfig, WwnConfig)):
            raise ValueError(
                f"resolve_downed requires a CwnConfig/WwnConfig; got {type(cfg).__name__!r}"
            )
        rounds = cfg.trauma.mortal_injury_rounds
        if not superseded_by_terminal:
            core.statuses.append(
                Status(
                    text=f"Mortal Injury — dies in {rounds} rounds unless stabilized",
                    severity=StatusSeverity.Scar,
                    created_turn=created_turn,
                    created_in_encounter=created_in_encounter,
                )
            )
        mortal_injury_declared_span(
            ruleset=self.slug,
            actor=core.name,
            rounds_to_die=rounds,
            superseded_by_terminal=superseded_by_terminal,
            _tracer=_tracer,
        )

        major = False
        major_roll = 0
        major_text = ""
        save_made = True
        if scene_traumatic:
            save_roll = rng.randint(1, 20)
            save_made = save_roll >= save_target
            if not save_made:
                major = True
                major_roll = rng.randint(1, 12)
                major_text = major_injury_entry(major_roll)
                # A maiming Major Injury scar is NOT the #239 contradiction — a
                # dead-AND-maimed body is coherent. Only the stabilizable "dies in
                # N rounds" death-clock conflicts with a terminal-dead verdict, so
                # the Major Injury is appended even when the Mortal Injury window
                # above is superseded. Provenance stamped like every status.
                core.statuses.append(
                    Status(
                        text=f"Major Injury — {major_text}",
                        severity=StatusSeverity.Scar,
                        created_turn=created_turn,
                        created_in_encounter=created_in_encounter,
                    )
                )
            major_injury_roll_span(
                ruleset=self.slug,
                actor=core.name,
                save_made=save_made,
                roll=major_roll,
                text=major_text,
                _tracer=_tracer,
            )

        return DownedResult(
            mortal=True,
            major=major,
            major_roll=major_roll,
            major_text=major_text,
            save_made=save_made,
        )

    # ------------------------------------------------------------------
    # Chargen resource seeding (ADR-143) — migrated from builder.seed_system_strain
    # + builder.seed_wwn_magic onto the module surface.
    # ------------------------------------------------------------------

    def seed_chargen_resources(self, *, rules, stats, class_def):
        """WN-family Effort pools + spellcasting + system strain (migrated from
        builder.seed_wwn_magic + seed_system_strain, ADR-143)."""
        from sidequest.game.chargen_contribution import ChargenResources
        from sidequest.game.system_strain import SystemStrainPool
        from sidequest.game.wwn_magic import EffortPool, SpellcastingState

        # CwnConfig is imported at module level (~line 48); no local re-import.
        # SystemStrainPool seeding (CWN/AWN): max == CONSTITUTION-flavor score.
        # Gates on isinstance(cfg, CwnConfig) (covers CWN + AWN + future subclasses)
        # rather than a slug string — consistent with the legacy seed_system_strain.
        system_strain = None
        cfg = rules.ruleset_config()
        if isinstance(cfg, CwnConfig):
            con_flavor = cfg.attribute_map["CONSTITUTION"]
            body_score = int(stats.get(con_flavor, 10))
            system_strain = SystemStrainPool(current=0, max=max(1, body_score), permanent=0)

        # WWN Effort pools + spellcasting state (wwn packs, magic classes).
        # Non-wwn / non-magic classes get ({}, None) — no silent partial state.
        effort: dict[str, EffortPool] = {}
        spellcasting: SpellcastingState | None = None
        if (
            rules.ruleset == "wwn"
            and rules.wwn is not None
            and class_def is not None
            and class_def.wwn_magic is not None
        ):
            cm = class_def.wwn_magic
            effort_base = rules.wwn.magic.effort_base
            attr_map = rules.wwn.attribute_map

            for src in cm.effort_sources:
                flavor = attr_map[src.governing_attr]
                score = int(stats.get(flavor, 10))
                pool_max = effort_base + src.starting_skill_level + swn_attribute_modifier(score)
                if cm.partial:
                    pool_max -= 1
                pool_max = max(1, pool_max)
                effort[src.source] = EffortPool(source=src.source, max=pool_max)

            level_key = "1"
            if cm.casts_per_day_by_level:
                casts_per_day = cm.casts_per_day_by_level.get(level_key, 0)
                max_spell_level = cm.max_spell_level_by_level.get(level_key, 0)
                capacity = cm.prepared_by_level.get(level_key, len(cm.starting_prepared))
                prepared = cm.starting_prepared[:capacity]
                spellcasting = SpellcastingState(
                    prepared=prepared,
                    casts_remaining=casts_per_day,
                    casts_per_day=casts_per_day,
                    max_spell_level=max_spell_level,
                )

        return ChargenResources(effort=effort, spellcasting=spellcasting, system_strain=system_strain)

    # ------------------------------------------------------------------
    # Prime-aware attribute assignment (ADR-143 Step 2).
    #
    # The WN SRD (WWN §1.5 / SWN §1.2) specifies that a character's prime
    # requisite — the key ability for their Calling — should be their
    # highest score. The native hint-derivation heuristic (base class
    # assign_attributes) infers this from chargen hints; the WN override
    # does it directly and unconditionally when class_def is provided.
    #
    # Supersedes the native hint-derivation heuristic: every WN-bound pack
    # gets prime-aware placement regardless of race/mutation/training hints.
    # ------------------------------------------------------------------

    def assign_attributes(
        self,
        *,
        pool: list[int],
        ability_names: list[str],
        class_def: ClassDef | None,
        acc: object | None = None,
    ) -> dict[str, int]:
        """Prime-aware: the chosen Calling's prime_requisite gets the highest pool
        value; remaining values fill the other stats high-to-low by declaration
        order (ADR-143 Step 2). Supersedes the native hint-derivation heuristic.

        When class_def is None or prime is not in ability_names, falls through to
        high-to-low fill in declaration order (explicit fall-through, not a masked
        error — no class hint at chargen time is a valid chargen path). Emits
        ``{slug}.chargen.attributes_assigned`` on every call (the GM-panel lie-
        detector confirming which prime, if any, drove the placement).

        acc is accepted for signature compatibility with the base but is not used:
        prime placement is unconditional and the hint-derivation heuristic is
        fully superseded for the WN family."""
        ordered = sorted(pool, reverse=True)
        stats: dict[str, int] = {}

        prime: str | None = None
        if class_def is not None and class_def.prime_requisite in ability_names:
            prime = class_def.prime_requisite

        if prime is not None:
            stats[prime] = ordered[0]
            rest = list(ordered[1:])
        else:
            rest = list(ordered)

        for name in ability_names:
            if name == prime:
                continue
            stats[name] = rest.pop(0)

        chargen_attributes_assigned_span(
            ruleset=self.slug,
            prime=prime,
            top=ordered[0],
            stats=stats,
        )
        return stats

    # ------------------------------------------------------------------
    # Effort engine (SWN/WWN SRD §1.4.4 / §6) — shared SWN-family crunch.
    #
    # Lifted to the family base in Story 102-6 so a swn-bound psychic commits
    # Effort exactly as a wwn caster does; WWN inherits it unchanged. Spans are
    # namespaced by the resolved slug (``{ruleset}.effort.*``) via ``self.slug``
    # so a swn commit reads ``swn.effort.commit`` and a wwn commit stays
    # ``wwn.effort.commit`` (backward compatible).
    # ------------------------------------------------------------------

    def commit_effort(
        self,
        *,
        core: CreatureCore,
        source: str,
        points: int = 1,
        duration: EffortDuration = "scene",
        label: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> EffortResult:
        """Commit Effort from one source pool (SRD §1.4.4 / §6).

        Over-commit is REFUSED (applied=False) — fail loud, never silently clamp.
        A missing source pool raises ValueError immediately. Emits
        ``{slug}.effort.commit`` on every call (applied=True or False)."""
        pool = core.effort.get(source)
        if pool is None:
            raise ValueError(f"{core.name!r} has no {source!r} Effort pool; seed it at chargen")
        applied = points <= pool.available
        reason = "" if applied else f"only {pool.available} of {points} Effort available"
        if applied:
            pool.commitments.append(EffortCommitment(points=points, duration=duration, label=label))
        effort_commit_span(
            ruleset=self.slug,
            actor=core.name,
            source=source,
            points=points,
            duration=duration,
            available=pool.available,
            applied=applied,
            _tracer=_tracer,
        )
        return EffortResult(
            applied=applied,
            source=source,
            available=pool.available,
            max=pool.max,
            reason=reason,
        )

    def reclaim_effort(
        self,
        *,
        core: CreatureCore,
        source: str,
        trigger: str = "maintained",
        _tracer: trace.Tracer | None = None,
    ) -> EffortResult:
        """Reclaim Effort from one pool by dropping commitments matching
        ``trigger`` (duration). Emits ``{slug}.effort.reclaim`` only when points
        are actually returned; an empty reclaim is applied=False, no span."""
        pool = core.effort.get(source)
        if pool is None:
            raise ValueError(f"{core.name!r} has no {source!r} Effort pool; seed it at chargen")
        matching = [c for c in pool.commitments if c.duration == trigger]
        returned = sum(c.points for c in matching)
        if not matching:
            return EffortResult(
                applied=False,
                source=source,
                available=pool.available,
                max=pool.max,
                reason=f"no {trigger!r} commitments to reclaim",
            )
        pool.commitments = [c for c in pool.commitments if c.duration != trigger]
        effort_reclaim_span(
            ruleset=self.slug,
            actor=core.name,
            source=source,
            points=returned,
            trigger=trigger,
            available=pool.available,
            _tracer=_tracer,
        )
        return EffortResult(
            applied=True,
            source=source,
            available=pool.available,
            max=pool.max,
        )

    def reclaim_scene_effort(
        self,
        *,
        core: CreatureCore,
        _tracer: trace.Tracer | None = None,
    ) -> None:
        """Drop all ``scene`` commitments across every Effort pool, emitting one
        ``{slug}.effort.reclaim`` per pool that had scene commitments. Pools with
        nothing to reclaim produce no span."""
        for source, pool in core.effort.items():
            matching = [c for c in pool.commitments if c.duration == "scene"]
            returned = sum(c.points for c in matching)
            if not matching:
                continue
            pool.commitments = [c for c in pool.commitments if c.duration != "scene"]
            effort_reclaim_span(
                ruleset=self.slug,
                actor=core.name,
                source=source,
                points=returned,
                trigger="scene",
                available=pool.available,
                _tracer=_tracer,
            )

    def reclaim_day_and_refresh(
        self,
        *,
        core: CreatureCore,
        comfortable: bool = True,
        cfg: SwnConfig | None,
        _tracer: trace.Tracer | None = None,
    ) -> None:
        """Drop ``scene`` commitments (always) and ``day`` commitments (when
        comfortable, or when the config's ``magic.day_reclaim_requires_comfort``
        is False). Refreshes ``core.spellcasting.casts_remaining`` to
        ``casts_per_day`` when spellcasting is seeded (WWN casters; a SWN psychic
        carries no spellcasting, so that block no-ops).

        Fails loud if cfg is not a SwnConfig (WwnConfig extends it) — day-rest
        reclaim is an SWN-family mechanic that requires the bound config."""
        if not isinstance(cfg, SwnConfig):
            raise ValueError(
                f"reclaim_day_and_refresh requires a SwnConfig; got {type(cfg).__name__!r}"
            )
        # SwnConfig carries no ``magic`` block (SWN psionics has no day-comfort
        # gate); WwnConfig does. Default to "comfort required" when absent.
        magic = getattr(cfg, "magic", None)
        requires_comfort = getattr(magic, "day_reclaim_requires_comfort", True)
        drop_day = comfortable or not requires_comfort
        durations_to_drop = {"scene"}
        if drop_day:
            durations_to_drop.add("day")

        for source, pool in core.effort.items():
            matching = [c for c in pool.commitments if c.duration in durations_to_drop]
            returned = sum(c.points for c in matching)
            if not matching:
                continue
            pool.commitments = [c for c in pool.commitments if c.duration not in durations_to_drop]
            # trigger reflects the duration ACTUALLY reclaimed for this pool, not
            # the intent — the GM panel is the lie detector and must not read
            # "day" when only scene Effort was swept on a comfortable rest.
            dropped = {c.duration for c in matching}
            trigger = "day" if "day" in dropped else "scene"
            effort_reclaim_span(
                ruleset=self.slug,
                actor=core.name,
                source=source,
                points=returned,
                trigger=trigger,
                available=pool.available,
                _tracer=_tracer,
            )

        if core.spellcasting is not None:
            core.spellcasting.casts_remaining = core.spellcasting.casts_per_day

    # ------------------------------------------------------------------
    # Psionic discipline activation (SWN SRD §6) — the cast-spine mirror.
    #
    # Homed on the WN core (ADR-142): a discipline commits Effort (WN-core) and,
    # on a push, routes System Strain through the WN-core strain seam. Psionics
    # is NOT SWN-exclusive — a WWN pack that ships a discipline catalog gives its
    # psychics psionics too (Story 102-6: "WWN has both psionics and a strain
    # seam"). Hoisting this off ``SwnRulesetModule`` is what lets a wwn-bound
    # psychic activate a strain-costing discipline after the WN siblings were
    # flattened (WWN no longer inherits SWN).
    # ------------------------------------------------------------------

    def activate_discipline(
        self,
        *,
        core: CreatureCore,
        discipline: PsionicDiscipline,
        source: str = PSIONIC_EFFORT_SOURCE,
        cfg: SwnConfig | None = None,
        _tracer: trace.Tracer | None = None,
    ) -> DisciplineActivationResult:
        """Activate a psionic discipline: commit its ``effort_cost`` from the
        psychic's Effort pool and, on a push (``strain_cost`` > 0), route the
        System Strain through the SAME ``core.system_strain`` counter the
        lethality seam uses (AC3 — no forked strain field).

        Zero free Effort → REFUSED loudly (``applied=False``, pool unchanged) —
        never a silent success. Emits ``{slug}.discipline.activated`` on EVERY
        call (``refused`` reflects the outcome), plus ``{slug}.effort.commit``
        and (on a push) ``{slug}.system_strain.delta`` when applied. A missing
        Effort pool raises ValueError (No Silent Fallbacks).

        A ``strain_cost`` discipline requires a seeded ``core.system_strain``
        pool (only the strain-bearing rulesets — WWN/CWN/AWN — carry one; SWN
        psionics is Effort-only). This precondition is checked BEFORE any Effort
        is committed: a strain push on a strainless core is REFUSED loudly
        (``applied=False``, pool unchanged), never a partial Effort spend and
        never an opaque ``AttributeError`` from the missing strain engine."""
        pool = core.effort.get(source)
        if pool is None:
            raise ValueError(
                f"{core.name!r} has no {source!r} Effort pool; a psychic seeds one at chargen"
            )
        cost = int(discipline.effort_cost)
        strain_cost = int(discipline.strain_cost or 0)

        # Precondition FIRST, before any mutation: a push needs a strain pool.
        # Checked here so a content/config mismatch (a strain discipline on a
        # strainless ruleset) is a clean loud refusal, not a half-committed
        # Effort spend that then AttributeErrors on the absent strain engine.
        if strain_cost > 0 and core.system_strain is None:
            reason = (
                f"{discipline.id!r} costs {strain_cost} System Strain but "
                f"{core.name!r} has no System Strain pool — this ruleset has no "
                "Strain engine (SWN psionics is Effort-only); author the strain "
                "discipline on a WWN/CWN/AWN pack"
            )
            discipline_activated_span(
                ruleset=self.slug,
                actor=core.name,
                discipline_id=discipline.id,
                refused=True,
                _tracer=_tracer,
            )
            return DisciplineActivationResult(
                applied=False,
                discipline_id=discipline.id,
                available=pool.available,
                strained=0,
                reason=reason,
            )

        applied = cost <= pool.available
        strained = 0
        if applied:
            self.commit_effort(
                core=core,
                source=source,
                points=cost,
                duration=discipline.duration,
                label=discipline.name,
                _tracer=_tracer,
            )
            if strain_cost > 0:
                # Precondition above guarantees core.system_strain is seeded here,
                # which only the strain-bearing rulesets (WWN/CWN/AWN) do — and
                # those define apply_system_strain. Safe to route the push.
                self.apply_system_strain(
                    core=core,
                    kind="temporary",
                    amount=strain_cost,
                    source=f"psionic:{discipline.id}",
                    cfg=cfg,
                    _tracer=_tracer,
                )
                strained = strain_cost
        discipline_activated_span(
            ruleset=self.slug,
            actor=core.name,
            discipline_id=discipline.id,
            refused=not applied,
            _tracer=_tracer,
        )
        return DisciplineActivationResult(
            applied=applied,
            discipline_id=discipline.id,
            available=pool.available,
            strained=strained,
            reason="" if applied else f"only {pool.available} of {cost} Effort available",
        )
