"""WwnRulesetModule — Worlds Without Number resolution behind the seam.

WWN (Sine Nomine, CC0) shares the "Without Number" resolution engine, so this
reparents directly onto ``WithoutNumberRulesetModule`` (ADR-142) and inherits
attack/skill/save/initiative/damage AND the WWN lethality layer (Luck save,
Shock, Trauma, System Strain, Mortal/Major Injury) from the shared core. Those
core methods emit ``wwn.*`` spans via ``self.slug``. WWN has no cyberspace (no
net-run resolution) and no ship gunnery (the base ABC's ``ship_attack_params``
fails loud with the wwn slug). What remains here is the WWN-specific surface:
spellcasting (Plan 2), the Warrior Killing Blow, and Veteran's Luck. NOT a
fallback — selected explicitly by `ruleset: wwn`.
"""

from __future__ import annotations

import math
import random

from opentelemetry import trace

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
from sidequest.game.status import Status, StatusSeverity
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
    wwn_spell_cast_span,
    wwn_veterans_luck_span,
)

# Scene-scoped used-marker for Veteran's Luck (Status.text sentinel).
# StatusSeverity.Scratch → cleared by clear_scratch_on_scene_end at scene end,
# so this flag is automatically reset each scene without any new cleanup code.
VETERANS_LUCK_USED_MARKER = "Veteran's Luck used this scene"


class WwnRulesetModule(WithoutNumberRulesetModule):
    slug = "wwn"

    # ------------------------------------------------------------------
    # Inherited from ``WithoutNumberRulesetModule`` (ADR-142): the d20/2d6
    # resolution engine, the Luck + attribute ``save_params``, the Effort engine,
    # and the full lethality stack (``resolve_shock`` / ``resolve_trauma`` /
    # ``apply_system_strain`` / ``resolve_downed``) — all emitting ``wwn.*`` spans
    # via ``self.slug``. WWN has no ship gunnery, so the base ABC's
    # ``ship_attack_params`` fails loud with the wwn slug. What remains here is the
    # WWN-specific surface below.
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
