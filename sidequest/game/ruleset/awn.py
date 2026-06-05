"""AwnRulesetModule — Ashes Without Number resolution behind the seam.

AWN (Sine Nomine, CC0) is post-apocalyptic, and its PERSONAL combat is
mechanically identical to Cities Without Number: the same d20 attack vs AC, the
same 2d6 skill ladder, the same four saves (Physical/Evasion/Mental/Luck), and
the same Shock / Trauma / System Strain / Mortal Injury / Major Injury lethality
stack. So this subclasses ``CwnRulesetModule`` and inherits every resolution
method verbatim — there are NO overrides in Plan 1.

Why the subclass exists at all, then:
  1. An HONEST slug. A pack that plays Ashes binds ``ruleset: awn``, not
     ``ruleset: cwn``. The slug is what surfaces in OTEL spans, save files, and
     the GM panel; mislabeling AWN combat as CWN would be a lie the lie-detector
     can't catch.
  2. A HOME for future AWN-only hooks. Later plans add mechanics CWN does not
     have — radiation save modifiers, mutation crunch, wasteland survival. Those
     override points land here, on the AWN module, without disturbing CWN.

Capability binding (the engine's ``isinstance(module, CwnRulesetModule)`` /
``isinstance(cfg, CwnConfig)`` sites) covers AWN for free precisely because an
``AwnRulesetModule`` IS a ``CwnRulesetModule``. NOT a fallback — selected
explicitly by ``ruleset: awn``.
"""

from __future__ import annotations

from sidequest.game.ruleset.cwn import CwnRulesetModule


class AwnRulesetModule(CwnRulesetModule):
    slug = "awn"
