"""Conformance test for the space_opera dogfight confrontation content (T6).

T1 added the ``ResolutionMode`` enum and confirmed ``rules.yaml`` declares
``resolution_mode: sealed_letter_lookup`` for the dogfight confrontation.
T3 added a wiring test (``tests/server/dispatch/test_sealed_letter.py``)
that loads space_opera and resolves a real (straight, bank) cell.

This test is the focused content-shape conformance for T6: it pins the
*expected shape* of the loaded ``ConfrontationDef`` so future drift in
``rules.yaml`` or ``dogfight/interactions_mvp.yaml`` fails loudly rather
than silently degrading the sealed-letter pipeline.

What this guards (CLAUDE.md "no silent fallbacks"):
  - dogfight ConfrontationDef exists and has ``resolution_mode``
    set to ``sealed_letter_lookup`` (not the default ``beat_selection``)
  - ``interaction_table`` is fully resolved through the ``_from:`` loader
    (i.e. the side-file actually loaded, not left as a pointer dict)
  - the table has all 16 cells of the 4x4 (red, blue) cross product
  - ``maneuvers_consumed`` matches the MVP set
  - every cell has populated narration metadata (no empty narration_hint)
  - every cell pair only references legal maneuvers (no orphans)
  - the loaded table is structurally compatible with
    ``resolve_sealed_letter_lookup`` — i.e. you can take the production
    pack, hand it to the dispatch handler, and it resolves cleanly.

Skips when ``sidequest-content`` is not checked out alongside
``sidequest-server`` (matches the pattern in ``test_resolution_mode.py``).
"""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    ConfrontationDef,
    InteractionTable,
    ResolutionMode,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

# The MVP maneuver set authored in
# sidequest-content/genre_packs/space_opera/dogfight/interactions_mvp.yaml.
# Pinned here so a content-side change shows up as a failing test rather
# than a silently-broken sealed-letter resolution.
EXPECTED_MANEUVERS_MVP: list[str] = ["straight", "bank", "loop", "kill_rotation"]


def _has_real_content() -> bool:
    return CONTENT_ROOT.is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def space_opera_pack() -> GenrePack:
    return load_genre_pack(CONTENT_ROOT / "space_opera")


@pytest.fixture(scope="module")
def dogfight_conf(space_opera_pack: GenrePack) -> ConfrontationDef:
    assert space_opera_pack.rules is not None, "space_opera has no rules.yaml"
    matches = [
        c for c in space_opera_pack.rules.confrontations if c.confrontation_type == "dogfight"
    ]
    assert len(matches) == 1, (
        f"expected exactly one 'dogfight' confrontation in space_opera, "
        f"found {len(matches)}: {[c.confrontation_type for c in matches]}"
    )
    return matches[0]


@pytest.fixture(scope="module")
def dogfight_table(dogfight_conf: ConfrontationDef) -> InteractionTable:
    # ADR-153 §3: the state graph loads into interaction_tables (dict).
    # The entry state is the FIRST declared table's starting_state.
    # Per rules.yaml comment: "FIRST entry is the duel's entry state (merge)".
    if not dogfight_conf.interaction_tables:
        raise AssertionError(
            "space_opera dogfight has no interaction_tables — "
            "rules.yaml is missing the `interaction_tables:` list of `_from:` pointers "
            "or the loader did not resolve it"
        )
    # Get the "merge" entry state table
    table = dogfight_conf.interaction_tables.get("merge")
    assert table is not None, (
        f"space_opera dogfight interaction_tables missing 'merge' entry state. "
        f"Available states: {sorted(dogfight_conf.interaction_tables.keys())}"
    )
    return table


# --- ConfrontationDef shape -------------------------------------------------


def test_dogfight_confrontation_uses_sealed_letter_lookup(
    dogfight_conf: ConfrontationDef,
) -> None:
    """The dogfight opts into sealed-letter resolution (not legacy beats)."""
    assert dogfight_conf.resolution_mode is ResolutionMode.sealed_letter_lookup, (
        f"dogfight resolution_mode should be sealed_letter_lookup, "
        f"got {dogfight_conf.resolution_mode!r}"
    )
    assert dogfight_conf.category == "combat"
    assert dogfight_conf.label  # non-empty


def test_dogfight_carries_no_native_dial(dogfight_conf: ConfrontationDef) -> None:
    """ADR-153 §2 firewall (158-31): the native energy dial was DELETED from
    the dogfight — the duel resolves via SWN hp_depletion. Replaces the old
    ``test_dogfight_has_dual_track_metrics``, which asserted the pre-firewall
    dial shape and had been failing against live content since content #508
    merged (2026-06-27) — 158-40 removes it as stale doctrine."""
    assert dogfight_conf.player_metric is None
    assert dogfight_conf.opponent_metric is None
    assert dogfight_conf.win_condition.value == "hp_depletion"


# --- InteractionTable shape -------------------------------------------------


def test_interaction_table_loaded_via_from_pointer(
    dogfight_table: InteractionTable,
) -> None:
    """The `_from: dogfight/interactions_mvp.yaml` pointer resolved into a
    real ``InteractionTable``, not a left-over dict."""
    assert isinstance(dogfight_table, InteractionTable)
    assert dogfight_table.version  # non-empty
    assert dogfight_table.starting_state == "merge"


def test_interaction_table_has_full_4x4_cross_product(
    dogfight_table: InteractionTable,
) -> None:
    """4 maneuvers × 4 maneuvers = 16 cells, no duplicates, no holes."""
    assert len(dogfight_table.cells) == 16, (
        f"expected 16 cells (4x4 cross product), got {len(dogfight_table.cells)}"
    )

    seen_pairs: set[tuple[str, str]] = set()
    for cell in dogfight_table.cells:
        key = (cell.pair[0], cell.pair[1])
        assert key not in seen_pairs, f"duplicate cell pair {key!r}"
        seen_pairs.add(key)

    expected_pairs: set[tuple[str, str]] = {
        (r, b) for r, b in product(EXPECTED_MANEUVERS_MVP, EXPECTED_MANEUVERS_MVP)
    }
    missing = expected_pairs - seen_pairs
    extra = seen_pairs - expected_pairs
    assert not missing, f"missing cells: {sorted(missing)}"
    assert not extra, f"unexpected cells: {sorted(extra)}"


def test_maneuvers_consumed_matches_mvp_set(
    dogfight_table: InteractionTable,
) -> None:
    """The header list pins which maneuvers the table covers."""
    assert dogfight_table.maneuvers_consumed == EXPECTED_MANEUVERS_MVP, (
        f"maneuvers_consumed drift: expected {EXPECTED_MANEUVERS_MVP}, "
        f"got {dogfight_table.maneuvers_consumed}"
    )


def test_no_orphan_maneuvers_in_cell_pairs(
    dogfight_table: InteractionTable,
) -> None:
    """Every cell pair references only maneuvers in maneuvers_consumed."""
    legal = set(dogfight_table.maneuvers_consumed)
    for cell in dogfight_table.cells:
        for slot, maneuver in zip(("red", "blue"), cell.pair, strict=True):
            assert maneuver in legal, (
                f"cell {cell.pair!r} {slot}={maneuver!r} not in maneuvers_consumed {sorted(legal)}"
            )


def test_every_cell_has_populated_narration_metadata(
    dogfight_table: InteractionTable,
) -> None:
    """No silent fallback: empty fields here would surface as blank GM panel
    rows and unbacked narration. Fail loudly at load time instead."""
    for cell in dogfight_table.cells:
        pair = tuple(cell.pair)
        assert cell.name, f"cell {pair!r} has empty name"
        assert cell.shape, f"cell {pair!r} has empty shape"
        assert cell.narration_hint, f"cell {pair!r} has empty narration_hint"
        assert cell.red_view, f"cell {pair!r} has empty red_view"
        assert cell.blue_view, f"cell {pair!r} has empty blue_view"


def test_dogfight_maneuvers_metadata_covers_every_consumed_maneuver(
    dogfight_conf: ConfrontationDef,
    dogfight_table: InteractionTable,
) -> None:
    """ADR-153 §2/§4: the ``beats:`` list was DELETED with the native dial
    (158-31) — the maneuver menu comes from ``maneuvers_consumed`` and the
    opponent brain's metadata from ``maneuvers`` (158-39, content #511).
    Replaces the old ``test_dogfight_beats_cover_every_consumed_maneuver``
    (stale since content #508; asserted the deleted beats shape). Every
    consumed maneuver must carry brain metadata or the ace can't be gated
    or motivated for it — fail loudly."""
    assert dogfight_conf.beats == [], (
        "the dogfight beats list was deleted by the ADR-153 firewall — its "
        f"reappearance is a doctrine regression, got {len(dogfight_conf.beats)} beats"
    )
    maneuver_ids = {m.id for m in dogfight_conf.maneuvers}
    missing = set(dogfight_table.maneuvers_consumed) - maneuver_ids
    assert not missing, (
        f"maneuvers_consumed {sorted(missing)} have no ManeuverDef metadata in "
        f"the dogfight confrontation (have: {sorted(maneuver_ids)})"
    )


# --- Wiring: handler accepts the real loaded table --------------------------


def test_loaded_table_resolves_through_sealed_letter_handler(
    dogfight_table: InteractionTable,
) -> None:
    """Pin the wiring: the *real, loaded* InteractionTable must be a valid
    input to ``resolve_sealed_letter_lookup``. We pick a non-trivial cell
    (loop, kill_rotation — the "mutual gunline" knife fight) to prove the
    handler reads through to per-actor view application.

    This complements T3 test 10 by adding a second cell coordinate and a
    structural assertion on the per-actor state merge.
    """
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.server.dispatch.sealed_letter import (
        SealedLetterOutcome,
        resolve_sealed_letter_lookup,
    )

    encounter = StructuredEncounter(
        encounter_type="dogfight",
        player_metric=EncounterMetric(name="hits", current=0, threshold=3),
        opponent_metric=EncounterMetric(name="hits", current=0, threshold=3),
        actors=[
            EncounterActor(name="Red Pilot", role="red", side="player"),
            EncounterActor(name="Blue Pilot", role="blue", side="opponent"),
        ],
    )

    outcome = resolve_sealed_letter_lookup(
        encounter,
        {"red": "loop", "blue": "kill_rotation"},
        dogfight_table,
    )

    assert isinstance(outcome, SealedLetterOutcome)
    assert outcome.red_maneuver == "loop"
    assert outcome.blue_maneuver == "kill_rotation"
    assert outcome.cell_name  # the "mutual gunline" cell, non-empty
    assert outcome.narration_hint  # non-empty narration

    red = next(a for a in encounter.actors if a.role == "red")
    blue = next(a for a in encounter.actors if a.role == "blue")
    # The mutual-gunline cell stamps a gun_solution on both pilots.
    assert red.per_actor_state.get("gun_solution") is True, (
        f"red view did not apply, per_actor_state={red.per_actor_state!r}"
    )
    assert blue.per_actor_state.get("gun_solution") is True, (
        f"blue view did not apply, per_actor_state={blue.per_actor_state!r}"
    )
