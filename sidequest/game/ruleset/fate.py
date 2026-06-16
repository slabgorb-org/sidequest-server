"""FateRulesetModule — Fate Core behind the RulesetModule seam (ADR-144).

Resolution is 4dF + skill vs opposition on the ladder (see fate_resolution.py).
The d20/beat-shaped abstract methods are NOT part of Fate's paradigm; they fail
loud here (No Silent Fallbacks) until ADR-144 F5 demotes them to base
default-raise and these overrides are deleted. The Fate conflict engine
(fate_conflict.py) arrives in F1c; dispatch routing in F1d.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace

from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.fate_resolution import FateOutcome, Opposition, resolve_action
from sidequest.telemetry.spans.fate import (
    fate_action_resolved_span,
    fate_aspect_invoked_span,
    fate_chargen_archetype_selected_span,
    fate_chargen_aspects_authored_span,
    fate_chargen_completed_span,
    fate_chargen_pyramid_allocated_span,
    fate_chargen_seeded_span,
    fate_chargen_stunts_selected_span,
    fate_chargen_validated_span,
    fate_compel_accepted_span,
    fate_compel_offered_span,
    fate_compel_refused_span,
    fate_consequence_taken_span,
    fate_point_delta_span,
    fate_stress_applied_span,
)

if TYPE_CHECKING:
    from sidequest.game.encounter import StructuredEncounter

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

    # --- Chargen seeding (ADR-144 F4a) ----------------------------------------

    def seed_chargen_resources(self, *, rules, stats, class_def, _tracer=None):
        """Seed a populated FateSheet from the pack's FateConfig (ADR-144 F4a).

        The Fate analogue of the WN family's ``seed_chargen_resources``: it returns
        a ``ChargenResources`` carrying a ``fate_sheet`` (not effort/spellcasting/
        system_strain). It reads ONLY the FateConfig — Fate has no d20 ability
        scores and no class, so ``stats`` and ``class_def`` are intentionally
        unused (the de-d20 invariant). Emits ``fate.chargen.seeded`` so the GM panel
        can confirm the sheet was engine-seeded, not narrator-improvised.

        The interactive chargen flow (player-authored aspects / skill-pyramid
        allocation / stunt picks) is story 121-7 (F4a2); this is the default seed.
        """
        del stats, class_def  # Fate seeds from FateConfig alone (no d20/class).
        from sidequest.game.chargen_contribution import ChargenResources
        from sidequest.genre.models.rules import FateConfig

        cfg = rules.ruleset_config()
        if not isinstance(cfg, FateConfig):
            # A fate-bound pack must author rules.fate; the RulesConfig validator
            # already enforces this, so reaching here means a misconfigured module
            # binding — fail loud (No Silent Fallbacks).
            raise FateEconomyError(
                "FateRulesetModule.seed_chargen_resources requires a FateConfig "
                f"(rules.ruleset_config() returned {type(cfg).__name__}); a 'fate' "
                "pack must author rules.fate (ADR-144)"
            )

        aspects: list[Aspect] = []
        if cfg.default_high_concept:
            aspects.append(Aspect(text=cfg.default_high_concept, kind="high_concept"))
        if cfg.default_trouble:
            aspects.append(Aspect(text=cfg.default_trouble, kind="trouble"))

        sheet = FateSheet(
            skills=dict(cfg.skills),
            aspects=aspects,
            refresh=cfg.refresh,
            fate_points=cfg.refresh,  # SRD: start a session with fate points == refresh.
        )
        fate_chargen_seeded_span(
            skill_count=len(sheet.skills),
            aspect_count=len(sheet.aspects),
            refresh=sheet.refresh,
            _tracer=_tracer,
        )
        # 114-10: compile the pack's shared signature starting gear onto the sheet
        # (the coat, the badge, the hat — design K-i). Emitted AFTER the
        # chargen.seeded span so that span's aspect_count reflects the seeded
        # HC/trouble aspects only; gear aspects/stunts carry source_gear and the
        # refresh debit (if any) rides the fate.gear_compiled span. No-op when the
        # pack authors no default gear.
        if cfg.gear:
            from sidequest.game.ruleset.fate_gear import compile_gear_onto_sheet

            compile_gear_onto_sheet(
                sheet,
                archetype="(default)",
                gear_ids=list(cfg.gear),
                gear_defs=list(cfg.gear_catalog),
                base_refresh=cfg.base_refresh,
                free_stunts=cfg.free_stunts,
                _tracer=_tracer,
            )
        return ChargenResources(fate_sheet=sheet)

    def apply_fate_chargen(self, *, rules, choices, _tracer=None):
        """Build a VALIDATED FateSheet from explicit interactive choices (ADR-144
        F4a2). The Guided/Freeform analogue of ``seed_chargen_resources``: the
        player's archetype/aspects/pyramid/stunts become a sheet, the single
        ``validate_fate_sheet`` authority checks it, and an illegal sheet fails loud
        (No Silent Fallbacks) — never silently corrected. Emits the ``fate.chargen.*``
        lie-detector spans so the GM panel can confirm the sheet was engine-built
        from explicit choices, not narrator-improvised. Returns ``ChargenResources``
        carrying the validated ``fate_sheet``."""
        from sidequest.game.chargen_contribution import ChargenResources
        from sidequest.game.ruleset.fate_chargen import (
            FateChargenError,
            build_fate_sheet,
            validate_fate_sheet,
        )
        from sidequest.genre.models.rules import FateConfig

        cfg = rules.ruleset_config()
        if not isinstance(cfg, FateConfig):
            raise FateEconomyError(
                "FateRulesetModule.apply_fate_chargen requires a FateConfig "
                f"(rules.ruleset_config() returned {type(cfg).__name__}); a 'fate' pack "
                "must author rules.fate (ADR-144)"
            )

        sheet = build_fate_sheet(choices, cfg)
        violations = validate_fate_sheet(sheet, cfg)
        legal = not violations

        if choices.archetype:
            fate_chargen_archetype_selected_span(archetype=choices.archetype, _tracer=_tracer)
        fate_chargen_aspects_authored_span(
            high_concept_present=bool(choices.high_concept.strip()),
            trouble_present=bool(choices.trouble.strip()),
            free_count=len(choices.free_aspects),
            _tracer=_tracer,
        )
        placed = {name: r for name, r in sheet.skills.items() if r > 0}
        counts: dict[int, int] = {}
        for rating in placed.values():
            counts[rating] = counts.get(rating, 0) + 1
        rung_counts = ",".join(f"{r}:{counts[r]}" for r in sorted(counts, reverse=True))
        fate_chargen_pyramid_allocated_span(
            rung_counts=rung_counts, skills_placed=len(placed), legal=legal, _tracer=_tracer
        )
        fate_chargen_stunts_selected_span(
            count=len(sheet.stunts),
            refresh_before=cfg.refresh,
            refresh_after=sheet.refresh,
            _tracer=_tracer,
        )
        fate_chargen_validated_span(legal=legal, violations="; ".join(violations), _tracer=_tracer)
        if not legal:
            raise FateChargenError(
                "interactive Fate chargen produced an illegal sheet: " + "; ".join(violations)
            )
        fate_chargen_completed_span(
            aspect_count=len(sheet.aspects),
            skill_count=len(sheet.skills),
            stunt_count=len(sheet.stunts),
            refresh=sheet.refresh,
            _tracer=_tracer,
        )
        return ChargenResources(fate_sheet=sheet)

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
        self,
        *,
        aspect_text: str,
        actor: str = "",
        reason: str = "",
        encounter: StructuredEncounter | None = None,
        _tracer: trace.Tracer | None = None,
    ) -> None:
        """Surface that the narrator proposed a compel (no economy change). The
        OTEL span lets the GM panel see the offer even when the player declines —
        including ``reason``, the proposed complication, so the lie-detector sees
        WHAT was offered, not merely THAT something was.

        ADR-144 F3e closes the F2b deferral: when an active ``encounter`` is given,
        PERSIST the offer as a PendingCompel so it survives to the FATE_STATE
        projection and the player can accept/refuse it. With no active conflict
        there is nowhere to persist (the F3e player surface is conflict-scoped) —
        the offer still fires its span, it just isn't actionable in the UI."""
        fate_compel_offered_span(actor=actor, aspect=aspect_text, reason=reason, _tracer=_tracer)
        if encounter is not None and not encounter.resolved:
            encounter.add_pending_compel(target=actor, aspect=aspect_text, reason=reason)

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

    def refuse_compel(
        self,
        *,
        sheet: FateSheet,
        aspect_text: str,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Refuse a compel: pay one fate point to decline (SRD). The decline half
        of the F3e round-trip. Reuses ``spend_fate_point``, which fails loud at
        zero fate points (No Silent Fallbacks) — you cannot decline for free.
        Validate-before-emit: the span fires only AFTER the spend succeeds, so a
        rejected refusal logs no phantom decline (mirrors ``invoke_aspect``)."""
        after = self.spend_fate_point(
            sheet=sheet, reason="compel_refused", actor=actor, _tracer=_tracer
        )
        fate_compel_refused_span(
            actor=actor, aspect=aspect_text, fate_points_after=after, _tracer=_tracer
        )
        return after

    # --- Stress + consequence atomic mutators (F1c orchestrates absorption) ---

    def mark_stress(
        self,
        *,
        sheet: FateSheet,
        track: str,
        box_value: int,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Check the unused stress box of value ``box_value`` on ``track`` and
        return the shifts it absorbs (== box_value). Fails loud on an unknown
        track, a missing box value, or an already-checked box. Choosing WHICH box
        absorbs a hit is F1c's orchestration; this is the atomic mark."""
        stress_track = sheet.stress.get(track)
        if stress_track is None:
            raise FateEconomyError(
                f"{actor or 'actor'} has no '{track}' stress track (have: {sorted(sheet.stress)})"
            )
        box = next((b for b in stress_track.boxes if b.value == box_value and not b.checked), None)
        if box is None:
            raise FateEconomyError(
                f"{actor or 'actor'} has no unchecked {track} stress box of value "
                f"{box_value} to mark"
            )
        box.checked = True
        fate_stress_applied_span(actor=actor, track=track, box_value=box_value, _tracer=_tracer)
        return box_value

    def take_consequence(
        self,
        *,
        sheet: FateSheet,
        level: str,
        aspect_text: str,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Fill the ``level`` consequence slot with an aspect and return the
        shifts it absorbs (the slot value). The filled slot BECOMES an aspect with
        one free invoke for the attacker (SRD). Fails loud if the slot is already
        filled or the level is unknown."""
        slot = next((c for c in sheet.consequences if c.level == level), None)
        if slot is None:
            raise FateEconomyError(
                f"{actor or 'actor'} has no '{level}' consequence slot "
                f"(have: {[c.level for c in sheet.consequences]})"
            )
        if slot.aspect is not None:
            raise FateEconomyError(
                f"{actor or 'actor'} {level} consequence is already filled ({slot.aspect.text!r})"
            )
        slot.aspect = Aspect(text=aspect_text, kind="consequence", free_invokes=1)
        fate_consequence_taken_span(actor=actor, level=level, aspect=aspect_text, _tracer=_tracer)
        return slot.value

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
