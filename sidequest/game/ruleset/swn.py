"""SwnRulesetModule — faithful Stars Without Number resolution behind the seam.

Attacks: d20 + attack_bonus + combat_skill + attribute_mod vs target Armor Class.
Skill checks / saves: later task (non-beat dice path).
Universal SWN constants come from RulesConfig.swn; per-class/per-item numbers
(attack bonus progression, weapon dice, armor AC) come from pack content.
NOT a fallback — selected explicitly by `ruleset: swn`.
"""

from __future__ import annotations

import random

from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.resolution import AttackRollParams, CheckRollParams
from sidequest.protocol.models import InitiativeEntry


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

    def attack_params(
        self, *, beat, attacker_stats, attacker_core, target_core
    ) -> AttackRollParams:
        attr_mod = self.stat_modifier(attacker_stats, beat.stat_check)
        combat_skill = int(getattr(beat, "combat_skill", 0) or 0)
        attack_bonus = int(getattr(beat, "attack_bonus", 0) or 0)
        target_ac = int(getattr(target_core, "armor_class", 10)) if target_core is not None else 10
        return AttackRollParams(
            modifier=attack_bonus + combat_skill + attr_mod,
            target_number=target_ac,
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

    def resolve_damage(self, *, beat, actor_core, pack):
        from sidequest.server.dispatch.damage_roll import resolve_damage_spec_from_beat_and_actor

        return resolve_damage_spec_from_beat_and_actor(beat=beat, actor_core=actor_core, pack=pack)

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
