"""Story 61-5 — architecture gate: snapshot field governance.

ADR-110 §Implementation Notes (2026-05-23 amendment) calls for the
`_PHASE_B_DROP_FIELDS` list to be "reviewed at every PR that adds a
``GameSnapshot`` field" — an un-enforced policy. Story 61-5 makes that
policy test-enforced.

**The gate.** Every top-level field on ``GameSnapshot`` MUST live in
exactly one of four named registries in
``sidequest.server.session_helpers``:

1. ``_PHASE_B_DROP_FIELDS`` — fields stripped from the per-turn
   ``<game_state>`` blob entirely (the narrator reads them from
   dedicated prompt sections, or the field has no consumer).
2. ``_PHASE_C_PROJECTIONS`` — fields that ride into the dump but are
   projected to a bounded shape by ``_apply_phase_c_projections``
   (in-scene filter, tail-K window, size cap, or nested-field drop).
3. ``_BOUNDED_BY_CONSTRUCTION`` — fields whose growth is bounded by
   their own structure (scalar primitives, enums, fixed-size
   collections, constant-cardinality dicts).
4. ``_EXCLUDED_FROM_DUMP`` — fields declared on the model but absent
   from ``model_dump()`` because their ``Field(...)`` carries
   ``exclude=True`` (transient runtime queues; reconstructed each turn
   from durable state).

A future PR that adds a new top-level ``GameSnapshot`` field without
placing it in exactly one registry fails this test with a precise
diagnostic naming the field. That is the architectural gate: a new
snapshot field must land a bounding decision, or the build breaks.

**Why reflection, not text-grep.** Per
``sidequest-server/CLAUDE.md`` "No Source-Text Wiring Tests" — tests
that grep production source are brittle (refactor-fragile) and unsafe
(``re.DOTALL`` + ``.*?`` can catastrophically backtrack inside the
GIL, defeating ``pytest-timeout``). The explicit exception called out
in CLAUDE.md is "reflection-based dataclass / type checks ...
because those interrogate runtime types, not source strings" —
i.e. the tripwire pattern at
``tests/dungeon/test_setpiece_attach_wiring.py`` assertion 4.

This test follows that pattern: pydantic
``GameSnapshot.model_fields`` is the runtime field set; the four
registries are runtime named tuples; ``Field(...).exclude`` is the
runtime metadata. No string matching.

**Story history.** Written RED against a ``session_helpers.py`` that
lacked ``_PHASE_C_PROJECTIONS``, ``_BOUNDED_BY_CONSTRUCTION``, and
``_EXCLUDED_FROM_DUMP``; story 61-5 introduced all three together so
the test ships GREEN. The ``_EXCLUDED_FROM_DUMP`` registry was added
during reviewer rework to correct a category error (the two
``Field(exclude=True)`` fields were initially mis-classified as
bounded-by-construction).
"""

from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.server import session_helpers as sh

# Registry references — module-level so all four are loaded at import
# time. If any registry is missing from session_helpers, this module
# fails to collect with a clear AttributeError naming the symbol. That
# is the loudest possible failure for a missing-registry regression —
# pytest renders the traceback pointing directly at the offending line
# and at the session_helpers attribute that's gone.

_DROP = sh._PHASE_B_DROP_FIELDS
_PROJECTIONS = sh._PHASE_C_PROJECTIONS
_BOUNDED = sh._BOUNDED_BY_CONSTRUCTION
_EXCLUDED = sh._EXCLUDED_FROM_DUMP

# Canonical name→registry mapping used by the categorization,
# overlap, and stray-entry tests. A single source of truth: adding a
# 5th registry means editing this dict (and importing it above), not
# editing every test that iterates the registries.
_REGISTRIES: dict[str, tuple[str, ...]] = {
    "_PHASE_B_DROP_FIELDS": _DROP,
    "_PHASE_C_PROJECTIONS": _PROJECTIONS,
    "_BOUNDED_BY_CONSTRUCTION": _BOUNDED,
    "_EXCLUDED_FROM_DUMP": _EXCLUDED,
}


# ---------------------------------------------------------------------------
# AC1 + AC2 — each registry MUST exist and be a tuple[str, ...].
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "registry_name",
    ["_PHASE_C_PROJECTIONS", "_BOUNDED_BY_CONSTRUCTION", "_EXCLUDED_FROM_DUMP"],
)
def test_registry_is_tuple_of_strs(registry_name: str) -> None:
    """Each new registry is ``tuple[str, ...]`` — required for reflection.

    ``_PHASE_B_DROP_FIELDS`` is excluded from the parametrize list because
    its tuple-of-str shape is implicitly tested by every consumer of the
    drop loop at ``session_helpers.py:905``; the three new registries
    introduced by story 61-5 get this dedicated structural check.
    """
    registry = _REGISTRIES[registry_name]
    assert isinstance(registry, tuple), (
        f"{registry_name} must be a tuple[str, ...] for reflection — got {type(registry).__name__}"
    )
    assert all(isinstance(name, str) for name in registry), (
        f"{registry_name} entries must all be str field names — "
        f"got {[type(n).__name__ for n in registry]}"
    )


# ---------------------------------------------------------------------------
# AC3 + AC4 — every top-level GameSnapshot field is in exactly one
# registry, discovered via pydantic reflection (not source-text grep).
# ---------------------------------------------------------------------------


def test_every_snapshot_field_is_categorized() -> None:
    """Every ``GameSnapshot`` field is in at least one bounding registry.

    Failure mode: the diagnostic lists the *unclassified* fields by name
    so Dev knows exactly what is missing a bounding decision. This is
    the architecture gate — a new field on ``GameSnapshot`` cannot land
    without an explicit category placement.
    """
    all_fields = set(GameSnapshot.model_fields.keys())
    assert all_fields, (
        "GameSnapshot.model_fields is empty — pydantic reflection returned "
        "no fields. Either the model has been gutted or pydantic version "
        "semantics changed. The architecture gate cannot vacuously pass on "
        "an empty model."
    )
    classified = set(_DROP) | set(_PROJECTIONS) | set(_BOUNDED) | set(_EXCLUDED)
    unclassified = all_fields - classified
    assert not unclassified, (
        f"GameSnapshot has {len(unclassified)} field(s) NOT placed in any "
        f"bounding registry: {sorted(unclassified)}. ADR-110 architecture "
        "gate (story 61-5) requires every top-level field to be placed in "
        "exactly one of _PHASE_B_DROP_FIELDS (strip entirely), "
        "_PHASE_C_PROJECTIONS (project to bounded shape), "
        "_BOUNDED_BY_CONSTRUCTION (growth bounded by structure), or "
        "_EXCLUDED_FROM_DUMP (Field(exclude=True), never serialized). "
        "If you just added a field to GameSnapshot, you must categorize it "
        "in sidequest/server/session_helpers.py."
    )


# ---------------------------------------------------------------------------
# AC5 — duplicate placement (field appears in more than one registry) is
# rejected loudly. A field that's both dropped and projected is a contradiction.
# ---------------------------------------------------------------------------


def test_no_field_in_multiple_registries() -> None:
    """Each ``GameSnapshot`` field is in AT MOST one bounding registry.

    Failure mode: the diagnostic names each overlapping field together
    with the two registries it appears in. Overlap means a bounding
    contradiction (e.g. "this field is both dropped AND projected") that
    will silently corrupt the projection pipeline; the gate catches it
    statically. Also catches intra-registry duplicates (a single tuple
    listing the same field name twice).
    """
    # Intra-registry duplicates: each tuple must have unique entries.
    intra_dupes: list[tuple[str, list[str]]] = []
    for reg_name, reg_tuple in _REGISTRIES.items():
        seen: dict[str, int] = {}
        for name in reg_tuple:
            seen[name] = seen.get(name, 0) + 1
        dupes = sorted(n for n, c in seen.items() if c > 1)
        if dupes:
            intra_dupes.append((reg_name, dupes))
    assert not intra_dupes, (
        "Registry tuple(s) contain duplicate entries (set() deduplication "
        "would silently mask this — a tuple is the wrong shape if "
        "duplicates are intended):\n"
        + "\n".join(f"  - {reg}: {dupes}" for reg, dupes in intra_dupes)
    )

    # Cross-registry overlap: each pair of registries must be disjoint.
    overlaps: list[tuple[str, tuple[str, str]]] = []
    names = list(_REGISTRIES)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            for shared in set(_REGISTRIES[a]) & set(_REGISTRIES[b]):
                overlaps.append((shared, (a, b)))

    assert not overlaps, (
        "The following GameSnapshot field(s) appear in MORE THAN ONE "
        "bounding registry — the categories are mutually exclusive:\n"
        + "\n".join(f"  - {name!r}: {a} AND {b}" for name, (a, b) in overlaps)
    )


# ---------------------------------------------------------------------------
# AC6 — registries don't pollute themselves with non-existent field names.
# An entry in any registry must correspond to a real model_fields key.
# ---------------------------------------------------------------------------


def test_registries_reference_only_real_snapshot_fields() -> None:
    """Every registry entry corresponds to a real ``GameSnapshot`` field.

    Failure mode: a registry contains a name that is NOT in
    ``GameSnapshot.model_fields``. Either the field was removed without
    cleaning the registry, or the registry has a typo. Either way it's
    dead config that masquerades as governance — the gate names it.
    """
    all_fields = set(GameSnapshot.model_fields.keys())
    stray: dict[str, list[str]] = {
        reg_name: sorted(set(reg_tuple) - all_fields) for reg_name, reg_tuple in _REGISTRIES.items()
    }
    stray = {k: v for k, v in stray.items() if v}
    assert not stray, (
        "Registry entries reference field name(s) that are NOT on "
        "GameSnapshot.model_fields (typo or stale entry):\n"
        + "\n".join(f"  - {registry}: {names}" for registry, names in stray.items())
    )


# ---------------------------------------------------------------------------
# Substantive correctness — fields in _EXCLUDED_FROM_DUMP must actually
# have Field(exclude=True) on their declaration. This is the teeth that
# prevent a future Dev from silently removing `exclude=True` and leaving
# the field in this registry: the assertion fires loudly.
# ---------------------------------------------------------------------------


def test_excluded_from_dump_entries_actually_have_exclude_true() -> None:
    """Every ``_EXCLUDED_FROM_DUMP`` entry has ``Field(..., exclude=True)``.

    Failure mode: an entry in ``_EXCLUDED_FROM_DUMP`` has lost its
    ``exclude=True`` metadata (or never had it). The diagnostic names
    the offending field so Dev can either restore the exclusion or move
    the field to a different registry.

    This is the architectural teeth: the gate would otherwise allow a
    future Dev to remove ``exclude=True`` from a field and leave it
    classified as "excluded" — which would be a silent regression
    (the field starts riding into the dump but the gate still calls it
    not-in-dump). Reflecting on ``Field.exclude`` at test time forces
    re-categorization.
    """
    not_excluded: list[str] = []
    for name in _EXCLUDED:
        field_info = GameSnapshot.model_fields.get(name)
        if field_info is None:
            # Caught separately by test_registries_reference_only_real_snapshot_fields;
            # skip here so the diagnostic is single-purpose.
            continue
        if field_info.exclude is not True:
            not_excluded.append(name)
    assert not not_excluded, (
        "Field(s) in _EXCLUDED_FROM_DUMP no longer have `Field(..., "
        "exclude=True)` on their GameSnapshot declaration: "
        f"{sorted(not_excluded)}. Either restore `exclude=True` (likely "
        "the right answer — these fields are transient dispatch queues "
        "by design, see session.py:798-799 for the original rationale) "
        "or move the field to one of the other three registries with a "
        "matching bounding decision."
    )


def test_excluded_from_dump_entries_actually_absent_from_model_dump() -> None:
    """Every ``_EXCLUDED_FROM_DUMP`` entry is missing from a default ``model_dump()``.

    Complementary to the ``exclude=True`` assertion above: this drives
    pydantic at runtime to verify the field is, in fact, absent from
    the dump. Catches the (unlikely) case where a custom field
    serializer or model_dump override re-introduces the field despite
    the ``exclude=True`` metadata.
    """
    # Construct a snapshot with the required scalar slugs filled in;
    # everything else defaults. world_slug and genre_slug are
    # non-defaultable str fields on GameSnapshot.
    snap = GameSnapshot(world_slug="test", genre_slug="test")
    dump_keys = set(snap.model_dump().keys())
    leaked = sorted(name for name in _EXCLUDED if name in dump_keys)
    assert not leaked, (
        f"Field(s) in _EXCLUDED_FROM_DUMP appear in model_dump() output: "
        f"{leaked}. A custom field serializer or model_dump override is "
        "re-introducing these fields despite `exclude=True` metadata; "
        "the registry's contract (these fields never reach the dump) is "
        "broken."
    )


# ---------------------------------------------------------------------------
# Legacy-regression guard: the pre-61-2 four-field drop list previously
# missed `room_states` / `npcs` / `journal` — the runaway-valley incident
# (2026-05-23, $313 burn). Plus the narrative_log entry added in story 61-5.
# A future removal of any pinned entry must trip this guard.
# ---------------------------------------------------------------------------


def test_phase_b_drop_list_pins_expected_entries() -> None:
    """``_PHASE_B_DROP_FIELDS`` contains every entry it was meant to land.

    Scope: name-presence sanity check. The runtime wiring of the drop
    list (the ``for _drop_field in _PHASE_B_DROP_FIELDS`` loop at
    ``session_helpers.py:905`` inside ``_build_turn_context``) is
    exercised end-to-end by ``test_57_5_snapshot_slimming.py`` and
    ``test_61_2_snapshot_seven_field_projection.py``. This test
    enumerates the registry contents and asserts every expected entry
    is still present, so an accidental removal in a future refactor
    trips the guard regardless of whether the wiring still works.

    If you intentionally moved a field out of the drop list (e.g.
    promoting it to ``_PHASE_C_PROJECTIONS`` with real projection
    logic), update ``expected_drop_entries`` below in the same change
    — do not silently drop the entry.
    """
    # Pinned entries: the four from story 57-5 (ADR-110 Phase B), plus
    # narrative_log added in story 61-5 (was being dropped via a
    # separate explicit pop at session_helpers.py:802 since story 49-1
    # and is now unified into the registry).
    expected_drop_entries = {
        "active_tropes",
        "axis_values",
        "genie_wishes",
        "achievement_tracker",
        "narrative_log",
    }
    missing = sorted(expected_drop_entries - set(_DROP))
    assert not missing, (
        f"_PHASE_B_DROP_FIELDS lost expected entries: {missing}. If you "
        "intentionally moved a field out of the drop list (e.g. promoting "
        "it to a projection), that's a separate story — update "
        "`expected_drop_entries` in this test in the same change."
    )
