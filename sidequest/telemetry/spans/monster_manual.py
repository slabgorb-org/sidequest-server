"""Monster-manual spans — pre-generated NPC injection."""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS

SPAN_MONSTER_MANUAL_INJECTED = "monster_manual.injected"

# BUG 2b (eh-opp-damage): emitted when a per-turn re-injection merge keeps a
# damaged creature's live ``hp.current`` instead of resetting it to the patch's
# full-pool claim — the GM-panel lie-detector that the enemy was NOT silently
# healed back up between combat turns.
SPAN_MONSTER_MANUAL_HP_PRESERVED = "monster_manual.hp_preserved"

FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_INJECTED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_HP_PRESERVED)
