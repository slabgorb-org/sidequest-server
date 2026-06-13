"""WwnRulesetModule — Worlds Without Number resolution behind the seam.

WWN (Sine Nomine, CC0) shares the SWN/CWN resolution engine, so this subclasses
SwnRulesetModule and inherits attack/skill/save/initiative/damage verbatim. The
WWN lethality layer (Luck save, Shock, Trauma, System Strain, Mortal Injury) is
the same "Without Number" core CWN implemented; per the WWN spec §2.1 it is
COPIED here (not hoisted to a shared base) so neon_dystopia/cwn cannot regress
WWN. WWN has no cyberspace (no net-run resolution) and no ship
gunnery (ship_attack_params fails loud). Magic is added in Plan 2. NOT a
fallback — selected explicitly by `ruleset: wwn`.
"""

from __future__ import annotations

import math
import random

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.lethality import DownedResult, LethalityResult, major_injury_entry
from sidequest.game.ruleset.resolution import AttackRollParams, CheckRollParams
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.game.status import Status, StatusSeverity, status_roll_modifier
from sidequest.game.system_strain import StrainResult
from sidequest.game.wwn_magic import (
    CastInput,
    SpellcastResult,
    VeteransLuckMode,
    VeteransLuckResult,
)
from sidequest.genre.models.inventory import _DICE_RE, DamageSpec
from sidequest.genre.models.rules import SwnConfig, WwnConfig
from sidequest.telemetry.spans.wwn import (
    wwn_killing_blow_span,
    wwn_major_injury_roll_span,
    wwn_mortal_injury_declared_span,
    wwn_shock_applied_span,
    wwn_spell_cast_span,
    wwn_system_strain_delta_span,
    wwn_trauma_roll_span,
    wwn_veterans_luck_span,
)

# Scene-scoped used-marker for Veteran's Luck (Status.text sentinel).
# StatusSeverity.Scratch → cleared by clear_scratch_on_scene_end at scene end,
# so this flag is automatically reset each scene without any new cleanup code.
VETERANS_LUCK_USED_MARKER = "Veteran's Luck used this scene"


class WwnRulesetModule(SwnRulesetModule):
    slug = "wwn"

    def ship_attack_params(
        self, *, attacker_stats, pilot_skill, attack_bonus, geometry_modifier, target_ac, cfg
    ) -> AttackRollParams:
        """WWN has no ship gunnery — fail loud rather than inherit SWN's dogfight math."""
        raise NotImplementedError("wwn ruleset has no ship-gunnery resolution")

    def save_params(
        self, *, stats, save, level, label, cfg, character_core: object | None = None
    ) -> CheckRollParams:
        """WWN saves: three attribute saves inherited from SWN, plus Luck (no attribute)."""
        if save == "luck":
            return CheckRollParams(
                sides=20,
                count=1,
                modifier=status_roll_modifier(character_core),
                difficulty=int(cfg.save_base) - (int(level) - 1),
                label=label,
            )
        return super().save_params(
            stats=stats, save=save, level=level, label=label, cfg=cfg, character_core=character_core
        )

    def resolve_shock(
        self,
        *,
        spec: DamageSpec,
        target_melee_ac: int,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """WWN Shock ("Shock X/AC Y"): a melee weapon with shock>0 chips `shock`
        damage on a MISS when the target's Melee AC <= `spec.shock_ac`. Returns
        the chip damage (0 when not applicable). Emits wwn.shock.applied only
        when damage is actually chipped."""
        if spec.shock <= 0 or spec.shock_ac is None or target_melee_ac > spec.shock_ac:
            return 0
        wwn_shock_applied_span(
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
        """WWN Trauma: if the weapon has a Trauma Die, roll it; on a result that
        meets/exceeds the Trauma Target, multiply total damage by trauma_rating.
        Emits wwn.trauma.roll on every WWN strike that has a trauma_die."""
        if spec.trauma_die is None:
            return LethalityResult(
                base_total=base_total,
                final_total=base_total,
                traumatic=False,
                trauma_roll=0,
                trauma_target=0,
            )
        if not isinstance(cfg, WwnConfig):
            raise ValueError(f"resolve_trauma requires a WwnConfig; got {type(cfg).__name__!r}")
        target = (
            spec.trauma_target
            if spec.trauma_target is not None
            else cfg.trauma.default_trauma_target
        )
        trauma_roll = DamageSpec(dice=spec.trauma_die).roll(rng)
        traumatic = trauma_roll >= target
        final = base_total * spec.trauma_rating if traumatic else base_total
        wwn_trauma_roll_span(
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
        """Apply a System Strain change under WWN rules; emit wwn.system_strain.delta.

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
        if not isinstance(cfg, WwnConfig):
            raise ValueError(
                f"apply_system_strain requires a WwnConfig; got {type(cfg).__name__!r}"
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

        wwn_system_strain_delta_span(
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
        _tracer: trace.Tracer | None = None,
    ) -> DownedResult:
        """Resolve a WWN character dropped to 0 HP.

        Always declares a Mortal Injury (Scar status; dies at the end of
        cfg.trauma.mortal_injury_rounds unless stabilized). If a Traumatic Hit
        landed this scene, additionally rolls a Physical save (1d20 vs
        save_target); on failure, rolls 1d12 on the Major Injury table and
        attaches a second Scar. Emits wwn.mortal_injury.declared and (when
        rolled) wwn.major_injury.roll.
        """
        if not isinstance(cfg, WwnConfig):
            raise ValueError(f"resolve_downed requires a WwnConfig; got {type(cfg).__name__!r}")
        rounds = cfg.trauma.mortal_injury_rounds
        core.statuses.append(
            Status(
                text=f"Mortal Injury — dies in {rounds} rounds unless stabilized",
                severity=StatusSeverity.Scar,
            )
        )
        wwn_mortal_injury_declared_span(actor=core.name, rounds_to_die=rounds, _tracer=_tracer)

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
                core.statuses.append(
                    Status(text=f"Major Injury — {major_text}", severity=StatusSeverity.Scar)
                )
            wwn_major_injury_roll_span(
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
    # Effort engine (SRD §1.4.4) + psionic discipline activation (§6) are lifted
    # to ``SwnRulesetModule`` (Story 102-6 — shared SWN-family crunch); WWN
    # inherits ``commit_effort`` / ``reclaim_effort`` / ``reclaim_scene_effort`` /
    # ``reclaim_day_and_refresh`` / ``activate_discipline`` unchanged. The
    # slug-namespaced spans read ``wwn.effort.*`` here via ``self.slug``.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Cast spine (WWN SRD §4.2 / spec §C-§D)
    # ------------------------------------------------------------------

    def resolve_spellcast(
        self,
        *,
        caster_core: CreatureCore,
        spell: CastInput,
        target_core: CreatureCore | None = None,
        target_stats: dict[str, int] | None = None,
        cfg: SwnConfig | None,
        rng: random.Random,
        _tracer: trace.Tracer | None = None,
    ) -> SpellcastResult:
        """WWN cast spine (SRD §4.2 / spec §C-§D). Approach C: the engine
        guarantees the economy + the rolls; the bespoke effect is narrator prose.

        Validate (prepared, casts_remaining > 0, level <= max_spell_level). A
        failed validation REFUSES the cast — ``cast=False``, ``casts_remaining``
        UNCHANGED, ``reason`` set — recorded loudly on ``wwn.spell.cast``
        (``refused=True``); it is never a silent no-op and never raises.

        On a valid cast: spend exactly one cast, force the DEFENDER's own save
        via the inherited ``save_params`` when the spell offers one (the spell
        names the category, else ``cfg.magic.default_spell_save``), and roll
        ``caster_level x die`` for a damage spell — halved (round down) on a made
        save. Emits ``wwn.spell.cast`` on every call.

        ``target_stats`` carries the defender's ability scores (CreatureCore
        carries none — dispatch resolves them the same way ``_physical_save_target_for``
        does and passes them in). Without a target/stats a save spell resolves no
        save (``save_made=None``) but still spends the cast.
        """
        if not isinstance(cfg, WwnConfig):
            raise ValueError(f"resolve_spellcast requires a WwnConfig; got {type(cfg).__name__!r}")

        state = caster_core.spellcasting
        if state is None:
            raise ValueError(f"{caster_core.name!r} has no spellcasting state; seed one at chargen")

        save_category = spell.save if spell.save is not None else cfg.magic.default_spell_save

        def _refuse(reason: str) -> SpellcastResult:
            wwn_spell_cast_span(
                actor=caster_core.name,
                spell_id=spell.id,
                level=spell.level,
                refused=True,
                casts_remaining=state.casts_remaining,
                save=save_category if spell.save is not None else "",
                save_made=None,  # refusal: no save resolved (refused=True disambiguates)
                damage="0",
                _tracer=_tracer,
            )
            return SpellcastResult(
                cast=False,
                spell_id=spell.id,
                casts_remaining=state.casts_remaining,
                reason=reason,
            )

        if state.casts_remaining <= 0:
            return _refuse("no casts remaining")
        if spell.id not in state.prepared:
            return _refuse(f"{spell.id!r} is not prepared")
        if spell.level > state.max_spell_level:
            return _refuse(
                f"spell level {spell.level} exceeds max castable level {state.max_spell_level}"
            )

        # Spend exactly one cast.
        state.casts_remaining -= 1

        # Force the defender's own save (only when the spell offers one and a
        # defender + stats are available; otherwise the save is unresolved).
        save_made: bool | None = None
        if spell.save is not None and target_core is not None and target_stats is not None:
            params = self.save_params(
                stats=target_stats,
                save=save_category,
                level=int(target_core.level),
                label=f"spell-save:{spell.id}",
                cfg=cfg,
            )
            save_roll = rng.randint(1, params.sides)
            save_made = (save_roll + params.modifier) >= params.difficulty

        # Roll damage (caster_level x die for a scaling spell), save-for-half.
        damage = 0
        if spell.damage_die is not None:
            m = _DICE_RE.match(spell.damage_die.strip())
            if m is None:
                raise ValueError(
                    f"spell {spell.id!r} damage_die {spell.damage_die!r} is not NdM notation"
                )
            faces = int(m["faces"])
            count = int(caster_core.level) if spell.damage_per_level else int(m["count"])
            damage = DamageSpec(dice=f"{count}d{faces}").roll(rng)
            if save_made:
                damage //= 2

        wwn_spell_cast_span(
            actor=caster_core.name,
            spell_id=spell.id,
            level=spell.level,
            refused=False,
            casts_remaining=state.casts_remaining,
            save=save_category if spell.save is not None else "",
            # save_made stays bool | None: None = save NOT resolved (no defender/
            # stats), NOT a failed save. The span helper omits the attribute when
            # None so the GM panel never reads a misleading "failed".
            save_made=save_made,
            damage=str(damage),
            _tracer=_tracer,
        )
        return SpellcastResult(
            cast=True,
            spell_id=spell.id,
            casts_remaining=state.casts_remaining,
            save_made=save_made,
            damage=damage,
        )

    # ------------------------------------------------------------------
    # Warrior abilities (WWN SRD §1.5.18, spec §E)
    # ------------------------------------------------------------------

    def apply_killing_blow(
        self,
        *,
        base_total: int,
        level: int,
        cfg: SwnConfig | None,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """WWN Warrior Killing Blow (SRD §1.5.18): add ceil(level / divisor) to
        the damage of any attack/spell/ability (and to Shock). Returns the new
        total; emits wwn.killing_blow.

        Pure math + span — no core mutation, called by dispatch/dice.py on the
        killing_blow rider (the strike HIT path and the Shock chip path).
        cfg guard raises on non-WwnConfig, consistent with other methods.
        """
        if not isinstance(cfg, WwnConfig):
            raise ValueError(f"apply_killing_blow requires a WwnConfig; got {type(cfg).__name__!r}")
        bonus = math.ceil(int(level) / cfg.magic.killing_blow_divisor)
        total = base_total + bonus
        wwn_killing_blow_span(
            actor=actor,
            level=level,
            bonus=bonus,
            base=base_total,
            total=total,
            _tracer=_tracer,
        )
        return total

    def veterans_luck(
        self,
        core: CreatureCore,
        *,
        mode: VeteransLuckMode,
        _tracer: trace.Tracer | None = None,
    ) -> VeteransLuckResult:
        """WWN Warrior Veteran's Luck (SRD §1.5.18): once per scene Instant action.

        First call this scene → applied=True, sets a scene-scoped Scratch Status
        on ``core`` (cleared automatically by ``clear_scratch_on_scene_end`` at
        scene end, so the ability refreshes each scene without any new cleanup).
        Subsequent calls same scene → applied=False (reason set). Emits
        wwn.veterans_luck on EVERY call (applied True and False both recorded —
        fail-loud-but-recorded, consistent with commit_effort).

        ``mode`` must be ``"force_hit"`` or ``"force_miss"`` (declared by the
        caller; narrator tool contract lives in agents/tools/veterans_luck.py).
        """
        already_used = any(s.text == VETERANS_LUCK_USED_MARKER for s in core.statuses)
        if already_used:
            wwn_veterans_luck_span(
                actor=core.name,
                mode=mode,
                applied=False,
                _tracer=_tracer,
            )
            return VeteransLuckResult(
                applied=False,
                mode=mode,
                reason="Veteran's Luck already used this scene",
            )

        # Mark as used — Scratch severity so the existing scene-end sweep clears it.
        core.statuses.append(
            Status(
                text=VETERANS_LUCK_USED_MARKER,
                severity=StatusSeverity.Scratch,
            )
        )
        wwn_veterans_luck_span(
            actor=core.name,
            mode=mode,
            applied=True,
            _tracer=_tracer,
        )
        return VeteransLuckResult(applied=True, mode=mode)
