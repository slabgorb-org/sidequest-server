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

# wry_whimsy/oz fix (H1, 2026-06-14): emitted when ``ensure_loaded`` backfills a
# world's authored ``npcs.yaml`` cast into an ALREADY-seeded Manual (one that no
# longer ``needs_seeding()``, so ``seed_manual`` never re-runs). The GM-panel
# lie-detector that the canonical companions entered the pool on an existing
# save — the original bug was that they never did, so the road surfaced only
# random-minted walk-ons.
SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL = "monster_manual.authored_backfill"

# Playtest 150-20 (CWN-OTHER-SEATING, 2026-06-20): emitted when ``ensure_loaded``
# purges a stale, native-era encounter from a ruleset-module pack's persisted
# Manual cache. The cache is genre+world keyed and survives across sessions; one
# seeded under the native ``generate_enemy`` path (PLAYER-class enemies,
# ``hp=8*level``) is incoherent with a ``wwn|cwn|swn|awn`` binding (whose
# encountergen samples the bestiary and always stamps ``class="creature"``).
# Reusing it seated a 48-HP "Wheelman" against an L1 PC. The GM-panel
# lie-detector that the engine caught + dropped the stale block and will re-seed
# via the bestiary path.
SPAN_MONSTER_MANUAL_STALE_PURGED = "monster_manual.stale_encounter_purged"

# Story 158-33 (cross-world bestiary bleed, 2026-06-25): emitted when
# ``ensure_loaded`` purges an encounter whose creature(s) are absent from the
# CURRENT world's effective bestiary. The genre+world-keyed Manual was seeded
# under the pre-ADR-120 genre-tier bestiary (which mixed every world's
# creatures) and never re-validated after rosters moved to per-world
# ``bestiary.yaml``. These foreign enemies are ``class="creature"``, so the
# native-class STALE_PURGED signal above does NOT catch them; this is the
# sibling, world-membership purge. The GM-panel lie-detector that a Barsoom
# arena never seats a long_foundry "Knight of the Ashen Banner" (SOUL: Crunch
# in the Genre, Flavor in the World).
SPAN_MONSTER_MANUAL_FOREIGN_PURGED = "monster_manual.foreign_purged"

# Story 153-x (ADR-106 region population): emitted when a generated region's
# frozen procedural roster (Task 3) is injected into snapshot.npcs, region-
# stamped for region-keyed seating. The GM-panel lie-detector that procedural
# rooms field real, statted creatures instead of leaving the narrator to improvise.
SPAN_MONSTER_MANUAL_REGION_POPULATION = "monster_manual.region_population"

FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_INJECTED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_HP_PRESERVED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_ROOM_BOUND)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_STALE_PURGED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_FOREIGN_PURGED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_REGION_POPULATION)
