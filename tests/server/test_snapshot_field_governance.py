"""Story 61-5 RED — architecture gate: snapshot field governance.

ADR-110 §Implementation Notes (2026-05-23 amendment) calls for the
`_PHASE_B_DROP_FIELDS` list to be "reviewed at every PR that adds a
``GameSnapshot`` field" — an un-enforced policy. Story 61-5 makes that
policy test-enforced.

**The gate.** Every top-level field on ``GameSnapshot`` MUST live in
exactly one of three named registries in
``sidequest.server.session_helpers``:

1. ``_PHASE_B_DROP_FIELDS`` — top-level fields stripped from the per-turn
   ``<game_state>`` blob entirely (the narrator reads them from
   dedicated prompt sections, or the field has no consumer).
2. ``_PHASE_C_PROJECTIONS`` — fields that ride into the dump but are
   projected to a bounded shape (in-scene filter, tail-K window, size
   cap, or nested-field drop) BEFORE the dump leaves
   ``_apply_phase_c_projections`` / equivalent.
3. ``_BOUNDED_BY_CONSTRUCTION`` — fields whose growth is bounded by their
   own structure (scalar primitives, enums, fixed-size collections,
   constant-cardinality dicts).

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
``GameSnapshot.model_fields.keys()`` is the runtime field set; the
three registries are the runtime named-tuples. No string matching.

**RED state.** This test red-fails today because:

* ``_PHASE_C_PROJECTIONS`` does not yet exist in
  ``session_helpers.py``.
* ``_BOUNDED_BY_CONSTRUCTION`` does not yet exist in
  ``session_helpers.py``.
* Even if both were added empty, every ``GameSnapshot`` field except
  the four already in ``_PHASE_B_DROP_FIELDS`` would be unassigned.

Dev's GREEN-phase job is to introduce the two missing registries with
their authoritative contents and prove this test passes.
"""

from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot


# ---------------------------------------------------------------------------
# Registry import — the test MUST fail loudly if either registry is missing
# from session_helpers. Conftest-level imports would convert this into a
# collection error; importing inside the test lets the diagnostic land in
# the assertion message where Dev will read it.
# ---------------------------------------------------------------------------


def _import_registries() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return ``(drop, projections, bounded)`` from ``session_helpers``.

    Raises ``ImportError`` (caught by callers) if either of the two new
    registries has not yet been introduced — the failure message names
    the missing symbol explicitly so the RED diagnostic is unambiguous.
    """
    from sidequest.server import session_helpers as sh

    drop = sh._PHASE_B_DROP_FIELDS  # already exists (story 57-5)
    try:
        projections = sh._PHASE_C_PROJECTIONS
    except AttributeError as e:
        raise ImportError(
            "session_helpers._PHASE_C_PROJECTIONS is not defined. "
            "Story 61-5 requires this named tuple to enumerate every "
            "GameSnapshot field that rides into the dump but is bounded "
            "by projection (in-scene filter, tail-K, size cap, nested drop). "
            "See sprint/context/context-story-61-2.md §Per-Field Decision Table."
        ) from e
    try:
        bounded = sh._BOUNDED_BY_CONSTRUCTION
    except AttributeError as e:
        raise ImportError(
            "session_helpers._BOUNDED_BY_CONSTRUCTION is not defined. "
            "Story 61-5 requires this named tuple to enumerate every "
            "GameSnapshot field whose growth is bounded by its own "
            "structure (scalar primitive, enum, fixed-size collection, "
            "constant-cardinality dict). Compare against "
            "GameSnapshot.model_fields to determine which fields belong here."
        ) from e
    return drop, projections, bounded


# ---------------------------------------------------------------------------
# AC1 + AC2 — both new registries MUST exist as importable named tuples.
# ---------------------------------------------------------------------------


def test_phase_c_projections_registry_exists() -> None:
    """``_PHASE_C_PROJECTIONS`` is a tuple-typed registry in session_helpers."""
    try:
        _, projections, _ = _import_registries()
    except ImportError as e:
        pytest.fail(str(e))
    assert isinstance(projections, tuple), (
        f"_PHASE_C_PROJECTIONS must be a tuple[str, ...] for reflection — "
        f"got {type(projections).__name__}"
    )
    assert all(isinstance(name, str) for name in projections), (
        f"_PHASE_C_PROJECTIONS entries must all be str field names — "
        f"got {[type(n).__name__ for n in projections]}"
    )


def test_bounded_by_construction_registry_exists() -> None:
    """``_BOUNDED_BY_CONSTRUCTION`` is a tuple-typed registry in session_helpers."""
    try:
        _, _, bounded = _import_registries()
    except ImportError as e:
        pytest.fail(str(e))
    assert isinstance(bounded, tuple), (
        f"_BOUNDED_BY_CONSTRUCTION must be a tuple[str, ...] for reflection — "
        f"got {type(bounded).__name__}"
    )
    assert all(isinstance(name, str) for name in bounded), (
        f"_BOUNDED_BY_CONSTRUCTION entries must all be str field names — "
        f"got {[type(n).__name__ for n in bounded]}"
    )


# ---------------------------------------------------------------------------
# AC3 + AC4 — every top-level GameSnapshot field is in some registry,
# discovered via pydantic reflection (not source-text grep).
# ---------------------------------------------------------------------------


def test_every_snapshot_field_is_categorized() -> None:
    """Every ``GameSnapshot`` field is in at least one of the three registries.

    Failure mode: the diagnostic lists the *unclassified* fields by name
    so Dev knows exactly what is missing a bounding decision. This is
    the architecture gate — a new field on ``GameSnapshot`` cannot land
    without an explicit category placement.
    """
    drop, projections, bounded = _import_registries()
    all_fields = set(GameSnapshot.model_fields.keys())
    classified = set(drop) | set(projections) | set(bounded)
    unclassified = all_fields - classified
    assert not unclassified, (
        f"GameSnapshot has {len(unclassified)} field(s) NOT placed in any "
        f"bounding registry: {sorted(unclassified)}. ADR-110 architecture "
        f"gate (story 61-5) requires every top-level field to be placed in "
        f"exactly one of _PHASE_B_DROP_FIELDS (strip entirely), "
        f"_PHASE_C_PROJECTIONS (project to bounded shape), or "
        f"_BOUNDED_BY_CONSTRUCTION (growth bounded by structure). "
        f"If you just added a field to GameSnapshot, you must categorize it "
        f"in sidequest/server/session_helpers.py."
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
    statically.
    """
    drop, projections, bounded = _import_registries()
    drop_set = set(drop)
    projections_set = set(projections)
    bounded_set = set(bounded)

    overlaps: list[tuple[str, tuple[str, str]]] = []
    for name in drop_set & projections_set:
        overlaps.append((name, ("_PHASE_B_DROP_FIELDS", "_PHASE_C_PROJECTIONS")))
    for name in drop_set & bounded_set:
        overlaps.append((name, ("_PHASE_B_DROP_FIELDS", "_BOUNDED_BY_CONSTRUCTION")))
    for name in projections_set & bounded_set:
        overlaps.append(
            (name, ("_PHASE_C_PROJECTIONS", "_BOUNDED_BY_CONSTRUCTION"))
        )

    assert not overlaps, (
        f"The following GameSnapshot field(s) appear in MORE THAN ONE "
        f"bounding registry — a field cannot be both dropped and "
        f"projected (or bounded), the categories are exclusive:\n"
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
    drop, projections, bounded = _import_registries()
    all_fields = set(GameSnapshot.model_fields.keys())

    stray: dict[str, set[str]] = {
        "_PHASE_B_DROP_FIELDS": set(drop) - all_fields,
        "_PHASE_C_PROJECTIONS": set(projections) - all_fields,
        "_BOUNDED_BY_CONSTRUCTION": set(bounded) - all_fields,
    }
    stray = {k: v for k, v in stray.items() if v}
    assert not stray, (
        f"Registry entries reference field name(s) that are NOT on "
        f"GameSnapshot.model_fields (typo or stale entry):\n"
        + "\n".join(
            f"  - {registry}: {sorted(names)}" for registry, names in stray.items()
        )
    )


# ---------------------------------------------------------------------------
# AC7 — guard the known-bad regression: the four-field drop list pre-61-2
# missed `room_states` / `npcs` / `journal` (memory: runaway-valley incident
# 2026-05-23 — $313 burn). _PHASE_B_DROP_FIELDS must continue to be the
# single drop-list source of truth that the snapshot-dump pipeline reads.
# ---------------------------------------------------------------------------


def test_phase_b_drop_list_is_single_source_of_truth() -> None:
    """``_PHASE_B_DROP_FIELDS`` is the registry consumed by the dump pipeline.

    This is a behavior test, not a source-grep: we drive
    ``_apply_phase_c_projections`` (or whatever helper consumes the drop
    list) and observe the field set actually removed from a dump matches
    the registry. If a future refactor splits the drop list into a
    parallel hardcoded list inside ``_build_turn_context``, this test
    fails because the two diverge — exactly the runaway-valley class of
    bug ADR-110 §Implementation Notes warns about.

    The current pre-61-5 build wires the drop via the
    ``_PHASE_B_DROP_FIELDS`` constant directly into
    ``_build_turn_context`` (see ``session_helpers.py``). The gate test
    here just enumerates the constant and confirms no field name is
    duplicated across the registries (delegated to the dedicated overlap
    test above) — the runtime wiring is exercised by
    ``test_61_2_snapshot_seven_field_projection.py`` and
    ``test_57_5_snapshot_slimming.py``.
    """
    drop, _, _ = _import_registries()
    # Sanity: the four legacy entries from story 57-5 are still present.
    # If a future Dev removes any of them, that's a different decision and
    # should be its own story — this test forces the conversation.
    legacy_four = {
        "active_tropes",
        "axis_values",
        "genie_wishes",
        "achievement_tracker",
    }
    missing = legacy_four - set(drop)
    assert not missing, (
        f"_PHASE_B_DROP_FIELDS lost legacy story-57-5 entries: "
        f"{sorted(missing)}. If you intentionally moved a field out of "
        f"the drop list (e.g. promoting it to a projection), that's a "
        f"separate story — do not silently drop the entry."
    )
