"""Story 153-15 — SWN chargen recap grammar.

Playtest finding [SWN-CHARGEN-RECAP-GRAMMAR]: the space_opera confirmation
recap stitched the origin into ``The {class} from {race} space``. With the
``origins`` scene setting ``race_hint`` to a demonym (Spacer / Coreworlder /
Colonial / Uplifted), that rendered "The Pilot from Spacer space" — the demonym
is already a noun, so "from Spacer space" is a redundant, ungrammatical stitch.

The fix (content) reorders the origin to read as a noun before the calling and
drops the "from … space" scaffold: ``The {race} {class}`` → "The Spacer Pilot".

Two guards:

1. **Content guard** — the three live SWN worlds' confirmation recaps no longer
   carry the ``from {race} space`` stitch and use the ``{race} {class}`` order.
   Loaded through the real ``GenreLoader`` so it tracks the shipped packs.
2. **Render wiring** — the REAL aureate_span recap, interpolated through the
   production ``CharacterBuilder.interpolate_scene_narration`` with a
   space_opera-shaped accumulator (race_hint=Spacer, class_hint=Pilot), renders
   "The Spacer Pilot" — no "from", no dangling " space".
"""

from __future__ import annotations

import pytest

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.loader import GenreLoader
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

PACK_SLUG = "space_opera"
SWN_WORLDS = ("aureate_span", "coyote_star", "perseus_cloud")


def _confirmation_narration(world_slug: str) -> str:
    pack = GenreLoader().load(PACK_SLUG)
    world = pack.worlds[world_slug]
    confirmation = next(s for s in world.char_creation if s.id == "confirmation")
    return confirmation.narration


@pytest.mark.parametrize("world_slug", SWN_WORLDS)
def test_recap_drops_from_race_space_stitch(world_slug: str) -> None:
    """No SWN confirmation recap stitches the origin as 'from {race} space'."""
    narration = _confirmation_narration(world_slug)
    assert "from {race} space" not in narration, (
        f"{world_slug} confirmation recap still carries the from-race-space stitch"
    )
    # The bare 'from {race}' fragment is the stitch's signature; guard it too so
    # a partial revert ('from {race} sector') can't slip back in.
    assert "from {race}" not in narration, (
        f"{world_slug} confirmation recap still leads the origin with 'from {{race}}'"
    )


@pytest.mark.parametrize("world_slug", SWN_WORLDS)
def test_recap_uses_race_class_noun_order(world_slug: str) -> None:
    """Each SWN recap renders the origin as a noun before the calling."""
    narration = _confirmation_narration(world_slug)
    assert "{race} {class}" in narration, (
        f"{world_slug} confirmation recap should read 'The {{race}} {{class}}'"
    )


def _space_opera_shaped_builder() -> CharacterBuilder:
    """origins (race_hint) + crucible (class_hint) choice scenes, mirroring the
    live space_opera chargen shape, with the choices already applied so the
    accumulator carries race_hint='Spacer' / class_hint='Pilot'."""
    scenes = [
        CharCreationScene(
            id="origins",
            title="Where Are You From?",
            narration="Scene narration.",
            choices=[
                CharCreationChoice(
                    label="The Void",
                    description="Born in transit.",
                    mechanical_effects=MechanicalEffects(
                        race_hint="Spacer", background="Void-born"
                    ),
                )
            ],
            allows_freeform=True,
        ),
        CharCreationScene(
            id="crucible",
            title="What Do You Do?",
            narration="Scene narration.",
            choices=[
                CharCreationChoice(
                    label="Pilot",
                    description="You fly the ship.",
                    mechanical_effects=MechanicalEffects(class_hint="Pilot"),
                )
            ],
            allows_freeform=True,
        ),
    ]
    rules = RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Pilot",
        default_race="Spacer",
    )
    builder = CharacterBuilder(scenes=scenes, rules=rules)
    builder.apply_choice(0)  # origins -> race_hint=Spacer
    builder.apply_choice(0)  # crucible -> class_hint=Pilot
    return builder


def test_real_aureate_span_recap_renders_spacer_pilot() -> None:
    """End-to-end: the shipped aureate_span recap, interpolated through the
    production engine with a Spacer Pilot, reads 'The Spacer Pilot' — no
    'from', no dangling ' space'."""
    narration = _confirmation_narration("aureate_span")
    builder = _space_opera_shaped_builder()
    builder.with_lobby_name("Kara")

    rendered = builder.interpolate_scene_narration(narration)

    assert "The Spacer Pilot" in rendered
    assert "from Spacer space" not in rendered
    assert "Spacer space" not in rendered
    # {race}/{class} fully resolved — no leftover placeholders.
    assert "{race}" not in rendered
    assert "{class}" not in rendered
