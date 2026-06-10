"""SwnRulesetModule — faithful Stars Without Number resolution behind the seam.

Attacks: d20 + attack_bonus + combat_skill + attribute_mod vs target Armor Class.
Skill checks / saves: later task (non-beat dice path).
Universal SWN constants come from RulesConfig.swn; per-class/per-item numbers
(attack bonus progression, weapon dice, armor AC) come from pack content.
NOT a fallback — selected explicitly by `ruleset: swn`.
"""

from __future__ import annotations

import random

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.resolution import (
    AttackRollParams,
    CheckRollParams,
    JumpAdjudication,
    OpponentAttackOutcome,
)
from sidequest.game.wwn_magic import (
    DisciplineActivationResult,
    EffortCommitment,
    EffortDuration,
    EffortResult,
)
from sidequest.genre.models.psionics import PsionicDiscipline
from sidequest.genre.models.rules import BeatDef, SwnConfig
from sidequest.genre.models.world import Route
from sidequest.protocol.models import InitiativeEntry
from sidequest.telemetry.spans.psionics import (
    discipline_activated_span,
    effort_commit_span,
    effort_reclaim_span,
)

# Source key for the SWN psionic Effort pool. SWN psionics draw every discipline
# from ONE Effort pool (SRD §6), so the pool keys ``core.effort`` under this slug.
PSIONIC_EFFORT_SOURCE = "psionic"

# SWN spike-drive jump model (SRD Revised, Sine Nomine 2017, "Spike Drives" p.211):
# a spike drill crosses up to ``rating`` hexes and takes roughly six days of
# subjective transit regardless of distance, burning one fuel load per jump.
SPIKE_TRANSIT_DAYS = 6  # subjective days per spike drill (one jump)
SPIKE_FUEL_PER_JUMP = 1  # one fuel load consumed per jump
# A drive under a route's authored minimum makes the jump under strain: it still
# crosses (a bare adjacency is navigable), but burns an extra fuel load. Below-min
# is a mechanical cost, not a block — No Silent Fallbacks, and the field gets teeth.
UNDERRATED_DRIVE_FUEL_PENALTY = 1


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


class SwnRulesetModule(RulesetModule):
    slug = "swn"

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
        return AttackRollParams(
            modifier=attack_bonus + combat_skill + attr_mod,
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

    def ship_attack_params(
        self, *, attacker_stats, pilot_skill, attack_bonus, geometry_modifier, target_ac, cfg
    ) -> AttackRollParams:
        """SWN strike-craft gunnery: d20 + attack_bonus + pilot_skill +
        better-of(INT,DEX) mod + geometry_modifier vs target fighter AC.
        Pilot stands in for Shoot on a fighter-class ship (SRD ship combat)."""
        amap = cfg.attribute_map
        flavor_attrs = []
        for swn_attr in ("DEXTERITY", "INTELLIGENCE"):
            flavor = amap.get(swn_attr)
            if flavor is None:
                raise KeyError(
                    f"attribute_map missing {swn_attr!r} for ship gunnery "
                    "(RulesConfig validator should have caught this)"
                )
            flavor_attrs.append(flavor)
        best_mod = max(self.stat_modifier(attacker_stats, f) for f in flavor_attrs)
        return AttackRollParams(
            modifier=int(attack_bonus) + int(pilot_skill) + best_mod + int(geometry_modifier),
            target_number=int(target_ac),
        )

    def adjudicate_jump(
        self,
        *,
        route: Route | None,
        drive_rating: int,
        rng: random.Random,
    ) -> JumpAdjudication:
        """SWN spike-drive inter-system jump (Story 98-5, ADR-141 campaign scale).

        With an authored ``routes`` entry, the cost reflects its fields
        (``jump_fuel`` / ``transit_days`` / ``hazard``), falling back per-field to
        the spike-drive default when a field is unauthored. A route's
        ``drive_rating_min`` gates the jump: a ship below it makes the crossing
        under strain (one extra fuel load), never a block — a bare adjacency is
        always navigable.

        With no route (``None``), the spike-drive model computes an EXPLICIT
        default — one fuel load, ~six days, no narrative hazard — labelled
        ``ruleset_default`` so the caller emits the default-cost span (No Silent
        Fallbacks: a named computation, never a swallowed zero)."""
        hazard_roll = rng.randint(1, 6)  # d6 hazard check, recorded on the span every jump
        if route is None:
            return JumpAdjudication(
                fuel_spent=SPIKE_FUEL_PER_JUMP,
                transit_days=SPIKE_TRANSIT_DAYS,
                hazard=None,
                hazard_roll=hazard_roll,
                source="ruleset_default",
            )
        fuel_spent = route.jump_fuel if route.jump_fuel is not None else SPIKE_FUEL_PER_JUMP
        transit_days = route.transit_days if route.transit_days is not None else SPIKE_TRANSIT_DAYS
        if route.drive_rating_min is not None and drive_rating < route.drive_rating_min:
            # Underrated drive: strained jump costs an extra fuel load (not a block).
            fuel_spent += UNDERRATED_DRIVE_FUEL_PENALTY
        return JumpAdjudication(
            fuel_spent=fuel_spent,
            transit_days=transit_days,
            hazard=route.hazard,
            hazard_roll=hazard_roll,
            source="route",
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
        self, *, stats, attribute, skill_level, difficulty_key, label, cfg
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
            modifier=attr_mod + int(skill_level),
            difficulty=int(cfg.difficulties[difficulty_key]),
            label=label,
        )

    def save_params(self, *, stats, save, level, label, cfg) -> CheckRollParams:
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
            modifier=best_mod,
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
        Effort pool raises ValueError (No Silent Fallbacks)."""
        pool = core.effort.get(source)
        if pool is None:
            raise ValueError(
                f"{core.name!r} has no {source!r} Effort pool; a psychic seeds one at chargen"
            )
        cost = int(discipline.effort_cost)
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
            strain_cost = int(discipline.strain_cost or 0)
            if strain_cost > 0:
                # apply_system_strain lives on the strain-bearing WWN module; a
                # strain-costing discipline therefore requires a strain ruleset
                # (SWN proper has none). AttributeError here is the correct loud
                # failure for a strain discipline authored on a strainless pack.
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
