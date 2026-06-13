"""NativeRulesetModule — the current SideQuest dial/confrontation turn, behind the seam.

This is ADR-033's confrontation engine, relocated. It is the resolution model for packs
that bind `ruleset: native` (and, later, the Fate family). It is NOT a fallback for other
modules — it is one module among several, selected explicitly by the pack.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat as _engine_apply_beat
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.game.status import status_roll_modifier
from sidequest.genre.models.rules import BeatDef, ConfrontationDef

# Layer-inversion note: find_confrontation_def and resolve_damage_spec_from_beat_and_actor
# are pure game logic that happens to live under server.dispatch.* by historical accident.
# This game->server import is an intentional Spec-0 layering inversion; it works because
# those functions import only game/genre/protocol, so there is no load-time cycle.
# A later task may relocate them into the game layer proper.
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.damage_roll import resolve_damage_spec_from_beat_and_actor


def _stat_score(stats: dict[str, int], stat_check: str) -> int | None:
    """Look up a stat score with the case-insensitive ability-key fallback.

    This is the relocated native-dial lookup (formerly dice._stat_modifier): try the
    exact key, then any key matching case-insensitively, else None (caller maps to 0).
    """
    score = stats.get(stat_check)
    if score is None:
        for k, v in stats.items():
            if k.upper() == stat_check.upper():
                return v
        return None
    return score


class NativeRulesetModule(RulesetModule):
    slug = "native"

    def find_confrontation(
        self, confrontations: list[ConfrontationDef], encounter_type: str
    ) -> ConfrontationDef | None:
        return find_confrontation_def(confrontations, encounter_type)

    def stat_modifier(self, stats: dict[str, int], stat_check: str) -> int:
        score = _stat_score(stats, stat_check)
        if score is None:
            return 0
        return (score - 10) // 2

    def compute_dc(self, beat: BeatDef) -> int:
        return max(10, min(30, 10 + abs(beat.base) * 2))

    def apply_beat(self, *, encounter, actor, beat, outcome, turn, edge_resolver, damage_resolver):
        return _engine_apply_beat(
            encounter,
            actor,
            beat,
            outcome,
            turn=turn,
            edge_resolver=edge_resolver,
            damage_resolver=damage_resolver,
        )

    def resolve_damage(self, *, beat, actor_core, pack, world_slug=None):
        return resolve_damage_spec_from_beat_and_actor(
            beat=beat, actor_core=actor_core, pack=pack, world_slug=world_slug
        )

    def attack_params(self, *, beat, attacker_stats, attacker_core, target_core):
        # native ignores target_core: its target number is the beat DC. The modifier is
        # the stat mod plus any active status roll_modifier (e.g. fighting in the dark).
        # This reproduces the pre-generalization two-call path, now status-aware.
        attr_mod = self.stat_modifier(attacker_stats, beat.stat_check)
        status_mod = status_roll_modifier(attacker_core)
        return AttackRollParams(
            modifier=attr_mod + status_mod,
            target_number=self.compute_dc(beat),
        )
