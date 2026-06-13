"""AwnRulesetModule — Ashes Without Number resolution behind the seam.

AWN (Sine Nomine, CC0) is post-apocalyptic, and it shares the "Without Number"
resolution engine: the same d20 attack vs AC, the same 2d6 skill ladder, the
same four saves (Physical/Evasion/Mental/Luck), and the same Shock / Trauma /
System Strain / Mortal Injury / Major Injury lethality stack. So this reparents
directly onto ``WithoutNumberRulesetModule`` (ADR-142) — a clean WN sibling, no
longer ``Awn(Cwn)``. It inherits the lethality skeleton from the WN CORE (not
from CWN), and those core methods emit ``awn.*`` spans via ``self.slug`` — the
honest-slug guarantee. An AWN game now reads ``awn.trauma.roll`` /
``awn.system_strain.delta`` in OTEL and the GM panel, never the inherited
``cwn.*`` mislabel the old ``Awn(Cwn)`` hierarchy produced.

There are no overrides in Plan 1 — AWN's combat IS the WN core. AWN does NOT
carry ``resolve_hacking`` (that is a CWN-only surface; AWN never used it —
``AwnConfig.hacking`` defaults None, so AWN inherits the base no-op default,
which is never invoked). AWN-only mechanics (radiation, mutation crunch,
wasteland survival) land here as the project adds them. NOT a fallback —
selected explicitly by ``ruleset: awn``.
"""

from __future__ import annotations

from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule


class AwnRulesetModule(WithoutNumberRulesetModule):
    slug = "awn"
