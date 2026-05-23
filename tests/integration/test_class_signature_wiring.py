"""Story 2026-05-10 — full end-to-end wiring for class mechanical surface.

Mandatory wiring test per CLAUDE.md "Every Test Suite Needs a Wiring Test".

Drives a real Cleric chargen against the actual caverns_and_claudes genre
pack content and asserts the full AbilityDefinition + class_moves contract
holds end-to-end through the protocol shape that the WS state-mirror sends.

Chain exercised:
    classes.yaml
    → genre loader (load_genre_pack)
    → CharacterBuilder._seed_class_abilities (builder.build())
    → Character.abilities
    → party_member_from_character (views.py)
    → CharacterSheetDetails.abilities + class_moves

If _seed_class_abilities is broken, or the views wiring for class_moves is
missing, or classes.yaml lacks the Cleric abilities block — this test fails.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.builder import CharacterBuilder, StoryInput
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.models import AbilitySource
from sidequest.server.session_handler import _SessionData
from sidequest.server.views import party_member_from_character

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _build_character(pack, *, target_class: str, rng_seed: int = 42):
    """Walk the 6-scene C&C chargen flow for the named class.

    Forces all stats to 18 so every class qualifies (deterministic).
    Returns the finalised Character object.
    """
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
            rng=random.Random(rng_seed),
        )
        .with_lobby_name("Wiring")
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )
    stat_order = list(pack.rules.ability_score_names)
    builder._arrangement_pool = [18] * 6
    for stat in stat_order:
        builder.assign_stat(stat, 18)

    # Scene 0: the_roll — auto-advance (pool pre-loaded above).
    builder.apply_auto_advance()
    # Scene 1: the_arrangement — confirm all-18 assignment.
    builder.apply_arrangement_confirm()
    # Scene 2: the_calling — pick target class by class_hint.
    scene = builder.current_scene()
    idx = next(
        (i for i, c in enumerate(scene.choices) if c.mechanical_effects.class_hint == target_class),
        None,
    )
    assert idx is not None, (
        f"target_class {target_class!r} not in qualifying choices: "
        f"{[c.mechanical_effects.class_hint for c in scene.choices]}"
    )
    builder.apply_choice(idx)
    # Scene 3: the_story — pronouns + freeform background/description.
    builder.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Raised in the caverns.",
            description="Steadfast, candlelit, scarred.",
        )
    )
    # Scene 4: the_kit — auto-advance (class kit equipment generation).
    builder.apply_auto_advance()
    # Scene 5: the_mouth — auto-advance (display only).
    builder.apply_auto_advance()

    return builder.build("Wiring")


def _make_session_data(pack, character) -> _SessionData:
    """Construct a minimal _SessionData around a real pack + character.

    ``store`` and ``orchestrator`` are mocked — party_member_from_character
    touches neither. ``snapshot`` carries the character so party_location()
    resolves cleanly (returns None — no location set yet, which is fine).
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        characters=[character],
    )
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="Wiring Player",
        player_id="player:wiring",
        snapshot=snapshot,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )


def _build_sheet(pack, *, target_class: str):
    """Drive full chain: pack → chargen → session data → protocol sheet."""
    character = _build_character(pack, target_class=target_class)
    sd = _make_session_data(pack, character)
    # handler is typed but party_member_from_character does not read from it
    # in its body — MagicMock is the safe stand-in.
    party_member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="player:wiring",
        player_name="Wiring Player",
    )
    return party_member.sheet


# ---------------------------------------------------------------------------
# Cleric wiring test
# ---------------------------------------------------------------------------


def test_cleric_chargen_yields_turn_undead_in_state_mirror(cc_pack):
    """A Cleric created in caverns_and_claudes shows Turn Undead in the
    protocol-shaped CharacterSheetDetails with source=Class and real prose.

    Failure modes caught:
    - _seed_class_abilities not called → no abilities
    - classes.yaml Turn Undead block absent → no abilities
    - source discriminator wrong → assertion on AbilitySource.Class fails
    - genre_description is placeholder → prose assertion fails
    - class_moves not filtered → attack/defend/flee present
    - class_moves not populated → turn_undead/pray/shield_bash absent
    """
    sheet = _build_sheet(cc_pack, target_class="Cleric")

    assert sheet.abilities, (
        "Expected at least one ability for Cleric — _seed_class_abilities may not be wired"
    )

    turn_undead_entries = [a for a in sheet.abilities if a.name == "Turn Undead"]
    assert len(turn_undead_entries) == 1, (
        f"Expected exactly one Turn Undead entry; got {[a.name for a in sheet.abilities]}"
    )
    tu = turn_undead_entries[0]
    assert tu.source == AbilitySource.Class, (
        f"Turn Undead source should be AbilitySource.Class, got {tu.source!r}"
    )
    assert tu.genre_description, "Turn Undead genre_description must not be empty"
    assert "{writer agent" not in tu.genre_description, (
        "Turn Undead genre_description contains placeholder text — real prose must ship in classes.yaml"
    )
    # Spot-check a few real-prose tokens from the actual classes.yaml entry
    # (guards against a trivially passing but empty/wrong description).
    assert "holy symbol" in tu.genre_description.lower() or len(tu.genre_description) > 80, (
        "Turn Undead prose looks too short or wrong — check classes.yaml Cleric abilities block"
    )

    # class_moves: class-specific beats present, universal beats filtered.
    assert "turn_undead" in sheet.class_moves, (
        "turn_undead missing from class_moves — encounter_beat_choices not wired or _filter_class_moves broken"
    )
    assert "pray" in sheet.class_moves, "pray missing from class_moves"
    assert "shield_bash" in sheet.class_moves, "shield_bash missing from class_moves"
    assert "attack" not in sheet.class_moves, (
        "attack should be filtered out by _filter_class_moves (_UNIVERSAL_BEATS)"
    )
    assert "defend" not in sheet.class_moves, (
        "defend should be filtered out by _filter_class_moves (_UNIVERSAL_BEATS)"
    )
    assert "flee" not in sheet.class_moves, (
        "flee should be filtered out by _filter_class_moves (_UNIVERSAL_BEATS)"
    )


# ---------------------------------------------------------------------------
# Mage wiring test
# ---------------------------------------------------------------------------


def test_mage_chargen_has_empty_class_signature_but_class_moves(cc_pack):
    """Mage's signature is the magic plugin — classes.yaml carries no abilities
    block, so Class-source abilities must be empty. But class_moves must be
    populated from encounter_beat_choices (cast_spell, cast_cantrip, etc.).

    Failure modes caught:
    - Mage accidentally given Class abilities → should be empty
    - class_moves missing cast_spell / cast_cantrip → views wiring broken
    - universal beats leaking into Mage class_moves → filter broken
    """
    sheet = _build_sheet(cc_pack, target_class="Mage")

    class_source = [a for a in sheet.abilities if a.source == AbilitySource.Class]
    assert class_source == [], (
        f"Mage should have no Class-source abilities; got {[a.name for a in class_source]}"
    )

    assert "cast_spell" in sheet.class_moves, (
        "cast_spell missing from Mage class_moves — encounter_beat_choices not wired"
    )
    assert "cast_cantrip" in sheet.class_moves, (
        "cast_cantrip missing from Mage class_moves — encounter_beat_choices not wired"
    )
    # Universal beats must still be filtered for Mage too.
    assert "attack" not in sheet.class_moves, "attack should be filtered out for Mage"
    assert "defend" not in sheet.class_moves, "defend should be filtered out for Mage"
    assert "flee" not in sheet.class_moves, "flee should be filtered out for Mage"


# ---------------------------------------------------------------------------
# Class-signature pronoun agnosticism (sq-playtest 2026-05-17 / [BS-BUG-LOW])
# ---------------------------------------------------------------------------
#
# Beneath Sünden 3-player MP: Katia (they/them, Cleric) saw "He lifts the
# holy symbol…" — the class-signature prose hardcodes a gendered subject
# pronoun and there is NO pronoun-substitution layer on this path, so it
# mismatches any PC whose pronouns differ from the authored gender. The
# shipped signature prose must be pronoun-agnostic (the Fighter "Taunt"
# signature already is — it is the in-pack precedent and the passing
# control here, proving this assertion discriminates rather than passing
# vacuously). Drives the real classes.yaml → loader → builder → views
# chain (the chargen helper builds with pronouns="they/them").

# Whole-word gendered 3rd-person pronouns. "they/them/their/theirs/
# themself" are intentionally NOT here — singular-they is pronoun-safe,
# and in the Cleric prose "they/them/theirs" refer to the unliving, not
# the PC. ``\b`` boundaries keep "the"/"there"/"where" from matching.
_GENDERED_PRONOUN_RE = re.compile(r"\b(?:he|she|his|him|her|hers|himself|herself)\b", re.IGNORECASE)


@pytest.mark.parametrize(
    ("target_class", "ability_name"),
    [
        ("Cleric", "Turn Undead"),
        ("Thief", "Backstab"),
        ("Fighter", "Taunt"),  # already pronoun-neutral — passing control
    ],
)
def test_class_signature_prose_is_pronoun_agnostic(cc_pack, target_class, ability_name):
    """Shipped class-signature prose must contain no gendered 3rd-person
    pronoun, so it agrees with any pronoun the player selects at chargen
    (he/him, she/her, they/them). A hardcoded "He"/"She" subject silently
    mismatches the playgroup's they/them and opposite-gender PCs.
    """
    sheet = _build_sheet(cc_pack, target_class=target_class)
    entries = [a for a in sheet.abilities if a.name == ability_name]
    assert len(entries) == 1, (
        f"Expected exactly one {ability_name} entry for {target_class}; "
        f"got {[a.name for a in sheet.abilities]}"
    )
    prose = entries[0].genre_description
    found = _GENDERED_PRONOUN_RE.findall(prose)
    assert not found, (
        f"{target_class} {ability_name!r} class signature hardcodes gendered "
        f"pronoun(s) {found} — mismatches a they/them or opposite-gender PC. "
        f"Rewrite pronoun-agnostic (2nd person, like Fighter 'Taunt'). "
        f"Prose: {prose!r}"
    )
