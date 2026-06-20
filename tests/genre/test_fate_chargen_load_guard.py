"""Fate-chargen LOAD guard — ``_validate_fate_chargen_steps`` (playtest 2026-06-17).

Keith's "Both" decision (guard + content): a ``ruleset: fate`` pack whose EFFECTIVE
char_creation omits any of the three interactive ``fate_chargen_step`` scenes
(aspects / pyramid / stunts) must FAIL LOUD at load — not silently hand every
traveler the pack-default Fate sheet (the Oz default-sheet collapse). The
world-replaces-genre rule means each world runs its OWN char_creation when it
declares one, else the genre default, so the guard checks each world's effective list.

Synthetic fixtures only (No content in unit tests): the real-content invariant
("every shipped Fate pack carries the steps") is the pack validator's job, not a unit
test. Here we drive the guard with hand-built scenes and lightweight world stand-ins.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import (
    _validate_fate_chargen_seed_coverage,
    _validate_fate_chargen_steps,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.pack import World
from sidequest.genre.models.rules import FateHintSeed


def _step(step: str) -> CharCreationScene:
    """A scene that declares itself a Fate chargen step (aspects/pyramid/stunts)."""
    return CharCreationScene(
        id=f"fate_{step}",
        title="T",
        narration="N",
        choices=[],
        mechanical_effects=MechanicalEffects(fate_chargen_step=step),  # type: ignore[call-arg]
    )


def _plain(scene_id: str) -> CharCreationScene:
    """A narrative funnel scene with no fate_chargen_step (the pre-fix shape)."""
    return CharCreationScene(id=scene_id, title="T", narration="N", choices=[])


def _world(char_creation: list[CharCreationScene]) -> World:
    """The guard reads only ``world.char_creation``; a SimpleNamespace stands in for
    the heavy World model (its config/lore/cartography are irrelevant to this guard)."""
    return cast(World, SimpleNamespace(char_creation=char_creation))


_ALL = [_step("aspects"), _step("pyramid"), _step("stunts")]


class TestFateChargenLoadGuard:
    def test_noop_for_non_fate_ruleset(self) -> None:
        # A WN/native pack with a step-less chargen must NOT raise — Fate-only contract.
        _validate_fate_chargen_steps(
            ruleset="wwn",
            worlds={"barsoom": _world([_plain("origin")])},
            genre_char_creation=[_plain("origin")],
            path=Path("/packs/heavy_metal"),
        )

    def test_fate_world_with_all_three_steps_passes(self) -> None:
        _validate_fate_chargen_steps(
            ruleset="fate",
            worlds={"dust_and_lead": _world(list(_ALL))},
            genre_char_creation=[],
            path=Path("/packs/spaghetti_western"),
        )

    def test_fate_world_inheriting_genre_steps_passes(self) -> None:
        # World declares no own chargen -> falls to the genre default, which HAS the
        # steps (the pulp_noir / fixed-wry_whimsy shape).
        _validate_fate_chargen_steps(
            ruleset="fate",
            worlds={"oz": _world([])},
            genre_char_creation=list(_ALL),
            path=Path("/packs/wry_whimsy"),
        )

    def test_world_tier_steps_cover_a_stepless_genre(self) -> None:
        # spaghetti_western shape: genre tier empty, every world carries its own steps.
        _validate_fate_chargen_steps(
            ruleset="fate",
            worlds={
                "dust_and_lead": _world(list(_ALL)),
                "five_points": _world(list(_ALL)),
            },
            genre_char_creation=[],
            path=Path("/packs/spaghetti_western"),
        )

    def test_fate_world_missing_one_step_fails_loud(self) -> None:
        with pytest.raises(GenreLoadError) as exc:
            _validate_fate_chargen_steps(
                ruleset="fate",
                worlds={"oz": _world([_step("aspects"), _step("pyramid")])},  # no stunts
                genre_char_creation=[],
                path=Path("/packs/wry_whimsy"),
            )
        detail = str(exc.value)
        assert "stunts" in detail
        assert "oz" in detail

    def test_world_relying_on_stepless_genre_fails_loud(self) -> None:
        # The collapse case: world has no own chargen and the genre default lacks the
        # steps (pre-fix wry_whimsy) -> every traveler would get the default sheet.
        with pytest.raises(GenreLoadError) as exc:
            _validate_fate_chargen_steps(
                ruleset="fate",
                worlds={"oz": _world([])},
                genre_char_creation=[_plain("the_threshold"), _plain("the_keepsake")],
                path=Path("/packs/wry_whimsy"),
            )
        detail = str(exc.value)
        assert "aspects" in detail and "pyramid" in detail and "stunts" in detail
        # No world override -> the error names the GENRE tier, not a world file.
        assert "genre-tier" in detail
        assert "worlds/oz" not in detail

    def test_error_names_the_offending_world_tier_path(self) -> None:
        # World DECLARES its own (incomplete) chargen -> the error points at the world
        # file, not the genre file (even though the genre default is complete).
        with pytest.raises(GenreLoadError) as exc:
            _validate_fate_chargen_steps(
                ruleset="fate",
                worlds={"glenross": _world([_step("aspects")])},
                genre_char_creation=list(_ALL),
                path=Path("/packs/tea_and_murder"),
            )
        detail = str(exc.value)
        assert "glenross" in detail
        assert "worlds/glenross/char_creation.yaml" in detail


# ---------------------------------------------------------------------------
# Seed-coverage guard — ``_validate_fate_chargen_seed_coverage``
# ---------------------------------------------------------------------------
#
# The sibling of the steps guard above (DRIVER/GM Dev follow-up, five_points playtest
# 2026-06-20). Story 126-24 shipped the generic ``chargen_seed_table`` seam + pulp_noir
# content; the seam is keyed by the funnel's ``class_hint`` VALUE and falls back to a
# BLANK pyramid when no entry matches (No Silent Fallbacks at lookup — ``select_chargen_seed``
# returns None). If an author adds a calling to the crucible but forgets its seed entry,
# every traveler taking that calling silently gets the blank-pyramid on-ramp. This guard
# makes that a LOAD-time error: every ``class_hint`` the effective char_creation emits MUST
# have an entry in the effective (genre ∪ world, world-wins) seed table.
#
# Synthetic fixtures only (No content in unit tests): the real-content invariant ("every
# shipped Fate calling has a seed") is proven by load against the real seeded packs, not
# here. These drive the guard with hand-built crucibles + seed tables.


def _choice(class_hint: str | None) -> CharCreationChoice:
    """A crucible choice carrying (or not) a ``class_hint`` calling."""
    return CharCreationChoice(
        label="L",
        description="D",
        mechanical_effects=MechanicalEffects(class_hint=class_hint),  # type: ignore[call-arg]
    )


def _crucible(*class_hints: str | None) -> CharCreationScene:
    """A class-selecting scene whose choices emit the given callings."""
    return CharCreationScene(
        id="crucible",
        title="T",
        narration="N",
        choices=[_choice(h) for h in class_hints],
    )


def _seed(*names: str) -> dict[str, FateHintSeed]:
    """A seed table covering each named calling (the pyramid/aspects are irrelevant here —
    coverage is keyed on presence, not legality, which is the steps guard's concern)."""
    return {n: FateHintSeed(pyramid={"Notice": 1}, aspects=[f"{n} aspect"]) for n in names}


def _seedworld(
    char_creation: list[CharCreationScene],
    seed_table: dict[str, FateHintSeed] | None = None,
) -> World:
    """A world stand-in carrying both the override surfaces this guard reads:
    ``char_creation`` (effective funnel) and ``chargen_seed_table`` (world override)."""
    return cast(
        World,
        SimpleNamespace(char_creation=char_creation, chargen_seed_table=seed_table or {}),
    )


class TestFateChargenSeedCoverageGuard:
    def test_noop_for_non_fate_ruleset(self) -> None:
        # A WN pack emits class_hints + ships no seed table (WN has no Fate pyramid).
        # The seed-coverage contract is Fate-only — must NOT raise.
        _validate_fate_chargen_seed_coverage(
            ruleset="wwn",
            worlds={"barsoom": _seedworld([_crucible("Fighter")])},
            genre_char_creation=[_crucible("Fighter")],
            genre_seed_table={},
            path=Path("/packs/heavy_metal"),
        )

    def test_genre_table_covers_inherited_class_hints_passes(self) -> None:
        # World inherits the genre funnel; the genre seed table covers every calling.
        _validate_fate_chargen_seed_coverage(
            ruleset="fate",
            worlds={"the_big_sleep": _seedworld([])},
            genre_char_creation=[_crucible("Detective", "Brawler")],
            genre_seed_table=_seed("Detective", "Brawler"),
            path=Path("/packs/pulp_noir"),
        )

    def test_world_tier_funnel_covered_by_genre_table_passes(self) -> None:
        # spaghetti_western shape: genre funnel empty, the world carries its own crucible,
        # the genre seed table covers it.
        _validate_fate_chargen_seed_coverage(
            ruleset="fate",
            worlds={"five_points": _seedworld([_crucible("Gunslinger", "Outlaw")])},
            genre_char_creation=[],
            genre_seed_table=_seed("Gunslinger", "Outlaw"),
            path=Path("/packs/spaghetti_western"),
        )

    def test_world_table_supplements_genre_table_passes(self) -> None:
        # Union/world-wins: the genre table covers one calling, the world table the other.
        _validate_fate_chargen_seed_coverage(
            ruleset="fate",
            worlds={"glenross": _seedworld([_crucible("Doctor", "Detective")], _seed("Detective"))},
            genre_char_creation=[],
            genre_seed_table=_seed("Doctor"),
            path=Path("/packs/tea_and_murder"),
        )

    def test_choice_without_class_hint_is_ignored(self) -> None:
        # A non-class choice (class_hint=None) needs no seed — an empty table is fine.
        _validate_fate_chargen_seed_coverage(
            ruleset="fate",
            worlds={"oz": _seedworld([_crucible(None, None)])},
            genre_char_creation=[],
            genre_seed_table={},
            path=Path("/packs/wry_whimsy"),
        )

    def test_uncovered_class_hint_fails_loud(self) -> None:
        # The blank-pyramid bug: a calling the funnel emits with NO seed entry.
        with pytest.raises(GenreLoadError) as exc:
            _validate_fate_chargen_seed_coverage(
                ruleset="fate",
                worlds={"five_points": _seedworld([_crucible("Gunslinger", "Marshal")])},
                genre_char_creation=[],
                genre_seed_table=_seed("Gunslinger"),  # Marshal missing
                path=Path("/packs/spaghetti_western"),
            )
        detail = str(exc.value)
        assert "Marshal" in detail
        assert "five_points" in detail

    def test_error_names_world_tier_path_when_world_declares_chargen(self) -> None:
        # World DECLARES its own crucible with an uncovered calling -> the error points at
        # the world file (even though the genre funnel is covered — it's replaced wholesale).
        with pytest.raises(GenreLoadError) as exc:
            _validate_fate_chargen_seed_coverage(
                ruleset="fate",
                worlds={"five_points": _seedworld([_crucible("Outlaw")])},
                genre_char_creation=[_crucible("Detective")],
                genre_seed_table=_seed("Detective"),
                path=Path("/packs/spaghetti_western"),
            )
        detail = str(exc.value)
        assert "Outlaw" in detail
        assert "worlds/five_points/char_creation.yaml" in detail

    def test_error_names_genre_tier_when_world_inherits(self) -> None:
        # World declares no crucible -> it inherits the genre funnel; the uncovered calling
        # lives at the genre tier, so the error names the genre file, not a world file.
        with pytest.raises(GenreLoadError) as exc:
            _validate_fate_chargen_seed_coverage(
                ruleset="fate",
                worlds={"oz": _seedworld([])},
                genre_char_creation=[_crucible("Curious Child")],
                genre_seed_table={},
                path=Path("/packs/wry_whimsy"),
            )
        detail = str(exc.value)
        assert "Curious Child" in detail
        assert "genre-tier" in detail
        assert "worlds/oz" not in detail
