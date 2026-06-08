"""Player-facing visibility whitelist for reference pages.

Every (file_stem, key_path) reachable from a reference page is classified
as PUBLIC (rendered), KEEPER (intentionally hidden), or UNKNOWN. UNKNOWN
fires a WARN OTEL span and drops the field so content drift surfaces in
the GM panel.

Classification mechanism (single):

1. KEEPER pattern match (wildcards allowed) — return KEEPER.
2. PUBLIC pattern match (wildcards allowed) — return PUBLIC.
3. If file_stem is in PUBLIC_STEMS (the stems the renderer actually reads)
   — return PUBLIC.
4. Otherwise — return UNKNOWN.

Wildcard rule:
    A '*' segment in a PATTERN matches any single segment of the QUERY.
    Patterns are validated at import time to reject ≥3 wildcards (the YAML
    shape is too complex if you need that — decompose the data).

key_path shape:
    ()                              — the whole file
    ("history",)                    — top-level dict key
    ("factions", "*", "name")       — any list-of-dict item's "name" field
"""

from __future__ import annotations

from enum import Enum, auto

KeyPath = tuple[str, ...]
Entry = tuple[str, KeyPath]


class Visibility(Enum):
    PUBLIC = auto()
    KEEPER = auto()
    UNKNOWN = auto()


def _validate_pattern(path: KeyPath) -> None:
    """Reject path patterns with three or more wildcards."""
    wildcard_count = sum(1 for seg in path if seg == "*")
    if wildcard_count >= 3:
        raise ValueError(
            f"reference_visibility pattern depth too deep ({wildcard_count} '*' segments): {path}. "
            "If you need three or more wildcards, the YAML shape is too complex for "
            "the whitelist — decompose the data instead."
        )


# Stems the reference renderer actually reads. Files outside this set are
# never reached and need no classification. Default visibility for a stem
# in this set is PUBLIC; explicit KEEPER entries below carve out
# spoiler-bearing paths.
PUBLIC_STEMS: frozenset[str] = frozenset(
    {
        # RULES_FILES stems
        "archetypes",
        "classes",
        "rules",
        "progression",
        "magic",
        "power_tiers",
        "achievements",
        "tropes",  # NOTE: file-level KEEPER (see KEEPER below) — stem-default
        # never triggers because the file-root ('tropes', ()) is in KEEPER.
        "equipment_tables",
        "inventory",
        "beat_vocabulary",
        # LORE_WORLD_FILES stems
        "world",
        "cultures",
        "history",
        "calendar",
        "demographics",
        "legends",
        "openings",
        "lore",
        "locations",
        # factions is a PUBLIC (non-spoiler) stem in its own right — this
        # allowlist is independent of where/whether factions renders. (The
        # lore-page pack-flavor merge that once surfaced it was removed in
        # Story 63-10; the stem stays PUBLIC regardless.)
        "factions",
    }
)


# Explicit overrides. PUBLIC is reserved for paths that need to be visible
# inside a stem whose default is NOT PUBLIC (rare — currently none, kept
# for symmetry and future-extensibility).
PUBLIC: frozenset[Entry] = frozenset()


# KEEPER carves out specific spoiler-bearing paths within otherwise-PUBLIC
# stems. Wildcards allowed (depth ≤ 2).
KEEPER: frozenset[Entry] = frozenset(
    {
        # Whole tropes / seed_tropes files — spoiler-bearing trigger graphs.
        ("tropes", ()),
        ("seed_tropes", ()),
        # beat_vocabulary obstacles subtree — narrator-only obstacle stats.
        ("beat_vocabulary", ("obstacles",)),
        ("beat_vocabulary", ("obstacles", "*", "description")),
        ("beat_vocabulary", ("obstacles", "*", "failure_penalty")),
        ("beat_vocabulary", ("obstacles", "*", "name")),
        ("beat_vocabulary", ("obstacles", "*", "stat_check")),
        ("beat_vocabulary", ("obstacles", "*", "tags")),
        # rules narrator_hint fields across confrontation beats, edge
        # thresholds, and resource thresholds — narrator-only escalation cues.
        ("rules", ("confrontations", "*", "beats", "*", "narrator_hint")),
        ("rules", ("edge_config", "thresholds", "*", "narrator_hint")),
        ("rules", ("resources", "*", "thresholds", "*", "narrator_hint")),
        # power_tiers: per-class `npc` field carries the narrator's sizing for
        # NPC versions of class abilities. Class name is at any position,
        # tier slot is the list-of-dict wildcard.
        ("power_tiers", ("*", "*", "npc")),
        # history.points_of_interest keeper fields (Story 100-4). The POI section
        # (build_poi_section) projects POIs through a public allowlist, but the
        # SAME history.yaml is also projected as a generic-YAML node-tree, where
        # classify() is the only gate. These spoiler-bearing POI fields must be
        # KEEPER so they never cross via the generic path (spec C1). Top-level
        # points_of_interest shape; the chapters-nested variant is a separate
        # pre-existing gap (see Delivery Findings).
        ("history", ("points_of_interest", "*", "gm_notes")),
        ("history", ("points_of_interest", "*", "secret")),
        ("history", ("points_of_interest", "*", "trap")),
        ("history", ("points_of_interest", "*", "hidden_exit")),
        ("history", ("points_of_interest", "*", "draft")),
    }
)


# Validate all patterns at import time.
for _entry in PUBLIC | KEEPER:
    _validate_pattern(_entry[1])


def _match_pattern(pattern: KeyPath, query: KeyPath) -> bool:
    """True iff PATTERN matches QUERY; '*' in pattern matches any one segment.

    Lengths must equal — patterns don't elide segments. Both pattern and
    query may carry literal '*' (the renderer substitutes '*' for
    list-of-dict slots; the classifier sees that as a literal segment
    that also happens to satisfy a '*' pattern segment via the wildcard
    rule).
    """
    if len(pattern) != len(query):
        return False
    for p_seg, q_seg in zip(pattern, query, strict=False):
        if p_seg == "*":
            continue
        if p_seg != q_seg:
            return False
    return True


def classify(file_stem: str, key_path: KeyPath) -> Visibility:
    """Return the Visibility for a given (file_stem, key_path)."""
    # KEEPER first — explicit hides outrank stem-default PUBLIC.
    for pat_stem, pat_path in KEEPER:
        if pat_stem == file_stem and _match_pattern(pat_path, key_path):
            return Visibility.KEEPER
    # PUBLIC explicit override (currently empty).
    for pat_stem, pat_path in PUBLIC:
        if pat_stem == file_stem and _match_pattern(pat_path, key_path):
            return Visibility.PUBLIC
    # Stem-level default PUBLIC.
    if file_stem in PUBLIC_STEMS:
        return Visibility.PUBLIC
    return Visibility.UNKNOWN
