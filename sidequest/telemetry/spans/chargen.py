"""Character generation spans — stat rolls, backstory, archetype gate."""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute

SPAN_CHARGEN_STAT_ROLL = "chargen.stat_roll"
SPAN_CHARGEN_STATS_GENERATED = "chargen.stats_generated"
SPAN_CHARGEN_BACKSTORY_COMPOSED = "chargen.backstory_composed"

FLAT_ONLY_SPANS.update(
    {
        SPAN_CHARGEN_STAT_ROLL,
        SPAN_CHARGEN_STATS_GENERATED,
        SPAN_CHARGEN_BACKSTORY_COMPOSED,
    }
)


# ---------------------------------------------------------------------------
# Chargen archetype-resolution gate (Story 45-6).
#
# The chargen-confirmation seam wraps ``_resolve_character_archetype`` with a
# gate that distinguishes three states:
#
# - ``ok_resolved``      — resolver wrote a display name (pass).
# - ``ok_no_axes``       — pack opted out of the archetype system (pass).
# - ``blocked_partial``  — character would ship with a raw ``"j/r"`` pair, a
#                          missing-axes-with-pack-axes mismatch, or a
#                          resolver-raised state (fail; chargen rejected).
#
# The evaluator span fires on every chargen-confirm so Sebastien's GM panel
# (CLAUDE.md "OTEL Observability Principle") gets the negative confirmation
# that the gate ran. The blocked span fires only on the failure branch and
# carries ``block_reason`` so the dashboard can distinguish the three
# pumblestone-style failure modes.
# ---------------------------------------------------------------------------
SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED = "chargen.archetype_gate_evaluated"
SPAN_ROUTES[SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED] = SpanRoute(
    event_type="state_transition",
    component="character_creation",
    extract=lambda span: {
        "field": "archetype_gate",
        "op": "evaluated",
        "state": (span.attributes or {}).get("state", ""),
        "resolved_archetype": (span.attributes or {}).get("resolved_archetype", ""),
        "pack_has_axes": (span.attributes or {}).get("pack_has_axes", False),
        # Per-pair granularity: the gate has no access to the builder
        # accumulator post-build, so ``had_both_hints`` is the most
        # granular signal it can emit. False ⇒ at least one of jungian
        # or rpg_role hint was missing during chargen.
        "had_both_hints": (span.attributes or {}).get("had_both_hints", False),
        "provenance_set": (span.attributes or {}).get("provenance_set", False),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)
SPAN_CHARGEN_ARCHETYPE_GATE_BLOCKED = "chargen.archetype_gate_blocked"
SPAN_ROUTES[SPAN_CHARGEN_ARCHETYPE_GATE_BLOCKED] = SpanRoute(
    event_type="state_transition",
    component="character_creation",
    extract=lambda span: {
        "field": "archetype_gate",
        "op": "blocked",
        "state": "blocked_partial",
        "block_reason": (span.attributes or {}).get("block_reason", ""),
        "resolved_archetype": (span.attributes or {}).get("resolved_archetype", ""),
        "pack_has_axes": (span.attributes or {}).get("pack_has_axes", False),
        "had_both_hints": (span.attributes or {}).get("had_both_hints", False),
        "provenance_set": (span.attributes or {}).get("provenance_set", False),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)


# ---------------------------------------------------------------------------
# Chargen starting-kit dedup (Story 45-12).
#
# The chargen confirmation seam wires class-specific starting equipment from
# ``pack.inventory.starting_equipment`` after the builder has populated
# ``character.core.inventory.items`` with stub-form items rolled from
# ``equipment_tables``. Both extractors are legitimate and both write
# without identity check — the canonical write-back-symmetry failure that
# shipped Blutka with 24 items where the catalogue specifies 13.
#
# Two spans cover the dedup pass:
#
# - ``chargen.starting_kit_dedup_evaluated`` fires on EVERY chargen-confirm,
#   including the no-overlap and ``inventory_config=None`` paths. Per
#   CLAUDE.md OTEL Observability Principle, Sebastien's GM-panel needs
#   negative confirmation that the dedup pass ran (without it, "no
#   duplicates" is indistinguishable from "no overlap existed").
#
# - ``chargen.starting_kit_dedup_fired`` fires only when ``skipped_count
#   > 0`` and carries the ``skipped_ids`` list verbatim so the GM panel
#   can render which catalogue ids were collapsed.
# ---------------------------------------------------------------------------
SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED = "chargen.starting_kit_dedup_evaluated"
SPAN_ROUTES[SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED] = SpanRoute(
    event_type="state_transition",
    component="character_creation",
    extract=lambda span: {
        "field": "starting_kit_dedup",
        "op": "evaluated",
        "class_name": (span.attributes or {}).get("class_name", ""),
        "pre_dedup_count": (span.attributes or {}).get("pre_dedup_count", 0),
        "equipment_ids_count": (span.attributes or {}).get("equipment_ids_count", 0),
        "skipped_count": (span.attributes or {}).get("skipped_count", 0),
        "items_added": (span.attributes or {}).get("items_added", 0),
        "items_upgraded": (span.attributes or {}).get("items_upgraded", 0),
        "final_count": (span.attributes or {}).get("final_count", 0),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)
SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED = "chargen.starting_kit_dedup_fired"
SPAN_ROUTES[SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED] = SpanRoute(
    event_type="state_transition",
    component="character_creation",
    extract=lambda span: {
        "field": "starting_kit_dedup",
        "op": "fired",
        "class_name": (span.attributes or {}).get("class_name", ""),
        "skipped_count": (span.attributes or {}).get("skipped_count", 0),
        # ``skipped_ids`` is the load-bearing payload — GM panel renders
        # the list verbatim. OTEL serializes sequence attributes; the
        # extractor passes whatever shape the SDK stored.
        "skipped_ids": (span.attributes or {}).get("skipped_ids", ""),
        "items_added": (span.attributes or {}).get("items_added", 0),
        "final_count": (span.attributes or {}).get("final_count", 0),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)


# Story 2026-05-10: Class abilities seeding.
# See docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md.
# Fires when chargen wires class-specific abilities (innate actives, taunts, etc)
# into character.abilities after the builder populates the base pool.
SPAN_CHARGEN_CLASS_ABILITIES_SEEDED = "chargen.class_abilities.seeded"
SPAN_ROUTES[SPAN_CHARGEN_CLASS_ABILITIES_SEEDED] = SpanRoute(
    event_type="state_transition",
    component="character_creation",
    extract=lambda span: {
        "field": "chargen.class_abilities",
        "op": "seeded",
        "class_name": (span.attributes or {}).get("class_name", ""),
        "abilities_seeded": (span.attributes or {}).get("abilities_seeded", 0),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)
