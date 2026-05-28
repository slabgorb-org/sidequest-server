"""CwnRulesetModule — Cities Without Number resolution behind the seam.

CWN (Sine Nomine, CC0) shares SWN's resolution engine, so this subclasses
SwnRulesetModule and inherits attack/skill/save/initiative/damage verbatim.
The only core divergence is the CWN Luck saving throw: target = save_base -
(level - 1), unmodified by any attribute. NOT a fallback — selected explicitly
by `ruleset: cwn`.
"""

from __future__ import annotations

from sidequest.game.ruleset.resolution import CheckRollParams
from sidequest.game.ruleset.swn import SwnRulesetModule


class CwnRulesetModule(SwnRulesetModule):
    slug = "cwn"

    def save_params(self, *, stats, save, level, label, cfg) -> CheckRollParams:
        """CWN saves: three attribute saves inherited from SWN, plus Luck (no attribute)."""
        if save == "luck":
            return CheckRollParams(
                sides=20,
                count=1,
                modifier=0,
                difficulty=int(cfg.save_base) - (int(level) - 1),
                label=label,
            )
        return super().save_params(stats=stats, save=save, level=level, label=label, cfg=cfg)
