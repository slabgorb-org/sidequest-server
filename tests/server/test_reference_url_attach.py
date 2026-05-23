"""Server-side attachment of reference_url on protocol objects.

These tests are fixture-driven — no live genre_packs assertions. Per the
no-content-coupled-tests rule, live-pack validation lives in the separate
validator (see Task 14/15).

Tests 7, 8, 9 of the reference-pages v2 plan will append to this file.
Keep imports and top-of-file structure compatible with future additions.
"""

from __future__ import annotations

from sidequest.protocol.models import AbilityDefinition, AbilitySource

# ---------------------------------------------------------------------------
# Model field acceptance
# ---------------------------------------------------------------------------


def test_ability_definition_accepts_reference_url() -> None:
    ability = AbilityDefinition(
        name="Cosh",
        genre_description="A swift bludgeon.",
        mechanical_effect="Stun on hit.",
        source=AbilitySource.Class,
        reference_url="/reference/rules/tea_and_murder#class-burglar-signature-cosh",
    )
    assert ability.reference_url == ("/reference/rules/tea_and_murder#class-burglar-signature-cosh")


def test_ability_definition_reference_url_defaults_to_none() -> None:
    ability = AbilityDefinition(
        name="Keen Senses",
        genre_description="Notice things.",
        mechanical_effect="Advantage on perception.",
        source=AbilitySource.Race,
    )
    assert ability.reference_url is None


def test_ability_definition_serialises_reference_url() -> None:
    ability = AbilityDefinition(
        name="Cosh",
        genre_description="A swift bludgeon.",
        mechanical_effect="Stun on hit.",
        source=AbilitySource.Class,
        reference_url="/reference/rules/p#class-burglar-signature-cosh",
    )
    payload = ability.model_dump(mode="json")
    assert payload["reference_url"] == "/reference/rules/p#class-burglar-signature-cosh"


def test_ability_definition_class_source_url_none_when_omitted() -> None:
    """Class-source ability without reference_url is valid — URL may be None."""
    ability = AbilityDefinition(
        name="Turn Undead",
        genre_description="Channel divine power against undead.",
        mechanical_effect="Forces undead to flee.",
        involuntary=False,
        source=AbilitySource.Class,
    )
    assert ability.reference_url is None


# ---------------------------------------------------------------------------
# Wiring test — _seed_class_abilities attaches reference_url
# ---------------------------------------------------------------------------


def test_seed_class_abilities_attaches_reference_url() -> None:
    """Integration: _seed_class_abilities passes pack_id+class_name → reference_url.

    This is the behavioural wiring test required by CLAUDE.md
    ("Every Test Suite Needs a Wiring Test"). It drives
    _seed_class_abilities with a synthetic ClassDef + pack_id and asserts
    the constructed AbilityDefinition carries a populated reference_url.
    No live genre_packs are loaded.
    """
    from sidequest.game.builder import _seed_class_abilities
    from sidequest.genre.models.character import ClassAbilityDef, ClassDef

    class_def = ClassDef(
        id="burglar",
        display_name="Burglar",
        rpg_role="rogue",
        jungian_default="trickster",
        prime_requisite="DEX",
        minimum_score=9,
        kit_table="burglar_kit",
        abilities=[
            ClassAbilityDef(
                name="Cosh",
                genre_description="A swift bludgeon.",
                mechanical_effect="Stun on hit.",
                involuntary=False,
            )
        ],
    )

    abilities: list[AbilityDefinition] = []
    _seed_class_abilities(abilities, class_def, pack_id="tea_and_murder")

    assert len(abilities) == 1
    ab = abilities[0]
    assert ab.reference_url is not None
    assert "tea_and_murder" in ab.reference_url
    assert "burglar" in ab.reference_url
    assert "cosh" in ab.reference_url


def test_seed_class_abilities_no_pack_id_leaves_url_none() -> None:
    """When pack_id is None (unknown scope), reference_url is None — no crash."""
    from sidequest.game.builder import _seed_class_abilities
    from sidequest.genre.models.character import ClassAbilityDef, ClassDef

    class_def = ClassDef(
        id="fighter",
        display_name="Fighter",
        rpg_role="warrior",
        jungian_default="hero",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="fighter_kit",
        abilities=[
            ClassAbilityDef(
                name="Shield Bash",
                genre_description="Knock enemy back.",
                mechanical_effect="+1 push.",
                involuntary=False,
            )
        ],
    )

    abilities: list[AbilityDefinition] = []
    _seed_class_abilities(abilities, class_def, pack_id=None)

    assert len(abilities) == 1
    assert abilities[0].reference_url is None


# ---------------------------------------------------------------------------
# Task 7 — PartyMember.class_reference_url
# ---------------------------------------------------------------------------


def test_party_member_accepts_class_reference_url() -> None:
    """PartyMember accepts class_reference_url as an optional str field."""
    from sidequest.protocol.models import PartyMember

    member = PartyMember(
        player_id="p1",
        name="Player One",
        character_name="Blackwood",
        current_hp=10,
        max_hp=10,
        statuses=[],
        **{"class": "Burglar"},  # type: ignore[arg-type]
        level=1,
        class_reference_url="/reference/rules/tea_and_murder#class-burglar",
    )
    assert member.class_reference_url == "/reference/rules/tea_and_murder#class-burglar"


def test_party_member_class_reference_url_defaults_to_none() -> None:
    """class_reference_url is None when not supplied."""
    from sidequest.protocol.models import PartyMember

    member = PartyMember(
        player_id="p1",
        name="Player One",
        character_name="Blackwood",
        current_hp=10,
        max_hp=10,
        statuses=[],
        **{"class": "Burglar"},  # type: ignore[arg-type]
        level=1,
    )
    assert member.class_reference_url is None


def test_party_member_class_reference_url_serialises() -> None:
    """class_reference_url appears in model_dump(mode='json') output."""
    from sidequest.protocol.models import PartyMember

    member = PartyMember(
        player_id="p1",
        name="Player One",
        character_name="Blackwood",
        current_hp=10,
        max_hp=10,
        statuses=[],
        **{"class": "Burglar"},  # type: ignore[arg-type]
        level=1,
        class_reference_url="/reference/rules/tea_and_murder#class-burglar",
    )
    payload = member.model_dump(mode="json")
    assert payload["class_reference_url"] == "/reference/rules/tea_and_murder#class-burglar"


# ---------------------------------------------------------------------------
# Task 7 — wiring test: party_member_from_character attaches class_reference_url
# ---------------------------------------------------------------------------


def test_party_member_from_character_attaches_class_reference_url() -> None:
    """Integration: party_member_from_character populates class_reference_url
    when the character's class is in classes.yaml (class_def is not None).

    Uses a synthetic _SessionData with a minimal GenrePack carrying one
    ClassDef. No live genre_packs loaded — fixture-driven per CLAUDE.md rule.
    """
    from unittest.mock import MagicMock

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.character import ClassDef
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.views import party_member_from_character

    # Build a minimal ClassDef matching the character's class name.
    class_def = ClassDef(
        id="burglar",
        display_name="Burglar",
        rpg_role="rogue",
        jungian_default="trickster",
        prime_requisite="DEX",
        minimum_score=9,
        kit_table="burglar_kit",
        abilities=[],
    )

    # Build a synthetic GenrePack with just enough fields to survive
    # party_member_from_character.
    genre_pack = MagicMock()
    genre_pack.classes = [class_def]
    genre_pack.inventory = None

    snapshot = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="the_waxford_affair",
        turn_manager=TurnManager(interaction=1),
        characters=[],
    )

    sd = _SessionData(
        genre_slug="tea_and_murder",
        world_slug="the_waxford_affair",
        player_name="Keith",
        player_id="p1",
        snapshot=snapshot,
        store=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )

    character = Character(
        core=CreatureCore(
            name="Blackwood",
            description="A cat burglar.",
            personality="Sly.",
            inventory=Inventory(),
            edge=EdgePool(current=8, max=10, base_max=10),
        ),
        backstory="Grew up in the rookeries.",
        char_class="Burglar",
        race="Human",
    )

    member = party_member_from_character(
        MagicMock(),  # handler — only sd is used in this path
        sd,
        character,
        player_id="p1",
        player_name="Keith",
    )

    assert member.class_reference_url is not None
    assert "tea_and_murder" in member.class_reference_url
    assert "burglar" in member.class_reference_url


def test_party_member_from_character_skips_url_when_class_not_in_pack() -> None:
    """Integration: class_reference_url is None when the class is NOT in
    classes.yaml (class_def lookup returns None — unknown class).
    """
    from unittest.mock import MagicMock

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.views import party_member_from_character

    # No classes in the pack — class_def lookup yields None.
    genre_pack = MagicMock()
    genre_pack.classes = []
    genre_pack.inventory = None

    snapshot = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="the_waxford_affair",
        turn_manager=TurnManager(interaction=1),
        characters=[],
    )

    sd = _SessionData(
        genre_slug="tea_and_murder",
        world_slug="the_waxford_affair",
        player_name="Keith",
        player_id="p1",
        snapshot=snapshot,
        store=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )

    character = Character(
        core=CreatureCore(
            name="Blackwood",
            description="A cat burglar.",
            personality="Sly.",
            inventory=Inventory(),
            edge=EdgePool(current=8, max=10, base_max=10),
        ),
        backstory="Grew up in the rookeries.",
        char_class="UnknownClass",
        race="Human",
    )

    member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="p1",
        player_name="Keith",
    )

    assert member.class_reference_url is None
