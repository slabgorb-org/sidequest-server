"""Monster-manual spans — pre-generated NPC injection."""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS

SPAN_MONSTER_MANUAL_INJECTED = "monster_manual.injected"

# BUG 2b (eh-opp-damage): emitted when a per-turn re-injection merge keeps a
# damaged creature's live ``hp.current`` instead of resetting it to the patch's
# full-pool claim — the GM-panel lie-detector that the enemy was NOT silently
# healed back up between combat turns.
SPAN_MONSTER_MANUAL_HP_PRESERVED = "monster_manual.hp_preserved"

# Story 107-2 (ADR-059 per-room binding): emitted when a room's structured
# ``encounter_creatures`` binding resolves to its authored bestiary creature(s)
# and that creature is materialized into game state. The GM-panel lie-detector
# that the narrator drew the room's AUTHORED opponent ("Gnaw-Swarm") instead of
# improvising a label ("the creature of animal musk").
SPAN_MONSTER_MANUAL_ROOM_BOUND = "monster_manual.room_bound"

FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_INJECTED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_HP_PRESERVED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_ROOM_BOUND)
