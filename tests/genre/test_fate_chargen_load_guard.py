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
from sidequest.genre.loader import _validate_fate_chargen_steps
from sidequest.genre.models.character import CharCreationScene, MechanicalEffects
from sidequest.genre.models.pack import World


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
