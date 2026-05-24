"""Player-facing visibility whitelist for reference pages.

Every (file_stem, key_path) reachable from a reference page must be classified
as either PUBLIC (rendered) or KEEPER (intentionally hidden). Any field not in
either set is UNKNOWN — the dispatcher drops it AND fires a WARN OTEL span so
content drift surfaces in the GM panel rather than leaking silently.

key_path semantics:
    ()                              — the whole file
    ("history",)                    — top-level dict key
    ("factions", "*", "name")       — any list-of-dict item's "name" field
    Depth-3 or deeper "*" patterns are rejected at import time.
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
    """Reject path patterns with three or more wildcards. Two-level is the cap."""
    wildcard_count = sum(1 for seg in path if seg == "*")
    if wildcard_count >= 3:
        raise ValueError(
            f"reference_visibility pattern depth too deep ({wildcard_count} '*' segments): {path}. "
            "If you need three or more wildcards, the YAML shape is too complex for "
            "the whitelist — decompose the data instead."
        )


# Bootstrap entries — Task 8 will extend these by running the validator over
# every live pack. Keep entries grouped by file_stem for readability.
PUBLIC: frozenset[Entry] = frozenset(
    {
        # --- lore.yaml ---
        ("lore", ("world_name",)),
        ("lore", ("setting_anchor",)),
        ("lore", ("history",)),
        ("lore", ("cosmology",)),
        ("lore", ("geography",)),
        ("lore", ("factions",)),
        ("lore", ("factions", "*", "name")),
        ("lore", ("factions", "*", "summary")),
        ("lore", ("factions", "*", "description")),
        ("lore", ("factions", "*", "disposition")),
    }
)

KEEPER: frozenset[Entry] = frozenset(
    {
        ("tropes", ()),
        ("seed_tropes", ()),
    }
)


# Validate all patterns at import so a typo can't slip through.
for _entry in PUBLIC | KEEPER:
    _validate_pattern(_entry[1])

# Set of stems that have AT LEAST ONE registered entry (PUBLIC or KEEPER).
# Used by the renderer to skip classification for stems not yet enumerated
# by Task 8 — avoids dropping all content from files whose vocabulary has
# not yet been reviewed. Once Task 8 populates PUBLIC fully, every
# rendered stem will appear here and this bypass will be unreachable.
_REGISTERED_STEMS: frozenset[str] = frozenset(stem for stem, _ in PUBLIC | KEEPER)


def has_registered_entries(file_stem: str) -> bool:
    """Return True iff ``file_stem`` has at least one entry in PUBLIC or KEEPER.

    The renderer uses this to skip the visibility gate for stems whose
    vocabulary has not yet been enumerated (Task 8). Once the full PUBLIC
    set is populated, every stem reachable from a reference page will
    appear in ``_REGISTERED_STEMS`` and this function will always return
    True for rendered files.
    """
    return file_stem in _REGISTERED_STEMS


def classify(file_stem: str, key_path: KeyPath) -> Visibility:
    """Return the Visibility for a given (file_stem, key_path)."""
    entry: Entry = (file_stem, key_path)
    if entry in PUBLIC:
        return Visibility.PUBLIC
    if entry in KEEPER:
        return Visibility.KEEPER
    return Visibility.UNKNOWN
