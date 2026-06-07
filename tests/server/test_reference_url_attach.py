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
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
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
    # Epic 94: resolve_inventory traverses pack.worlds world-first; stub empty so
    # the MagicMock pack falls through to the (None) genre-tier inventory.
    genre_pack.worlds = {}
    # Story 68-1: party_member_from_character now reads the genre survivability
    # label; pin it to None on the synthetic pack so PartyMember validates.
    genre_pack.rules.survivability_pool_label = None
    # Story 82-8: party_member_from_character now resolves wealth tiers; pin an
    # empty ladder on the synthetic pack so it short-circuits to no wealth
    # label (a MagicMock ladder would make the resolver's fallback misfire).
    genre_pack.progression.wealth_tiers = []

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
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
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
            hp=HpPool(current=8, max=10, base_max=10),
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
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.views import party_member_from_character

    # No classes in the pack — class_def lookup yields None.
    genre_pack = MagicMock()
    genre_pack.classes = []
    genre_pack.inventory = None
    # Epic 94: resolve_inventory traverses pack.worlds world-first; stub empty so
    # the MagicMock pack falls through to the (None) genre-tier inventory.
    genre_pack.worlds = {}
    # Story 68-1: party_member_from_character now reads the genre survivability
    # label; pin it to None on the synthetic pack so PartyMember validates.
    genre_pack.rules.survivability_pool_label = None
    # Story 82-8: party_member_from_character now resolves wealth tiers; pin an
    # empty ladder on the synthetic pack so it short-circuits to no wealth
    # label (a MagicMock ladder would make the resolver's fallback misfire).
    genre_pack.progression.wealth_tiers = []

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
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
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
            hp=HpPool(current=8, max=10, base_max=10),
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


# ---------------------------------------------------------------------------
# Task 8 — JournalEntry.reference_url field
# ---------------------------------------------------------------------------


def test_journal_entry_accepts_reference_url() -> None:
    from sidequest.protocol.models import FactCategory, JournalEntry

    entry = JournalEntry(
        fact_id="abc",
        content="The Vicarage smells of rosewater.",
        category=FactCategory.Place,
        source="Observation",
        confidence="confirmed",
        learned_turn=3,
        reference_url="/reference/lore/tea_and_murder/glenross#location-the-vicarage",
    )
    assert entry.reference_url == ("/reference/lore/tea_and_murder/glenross#location-the-vicarage")


def test_journal_entry_reference_url_defaults_to_none() -> None:
    from sidequest.protocol.models import FactCategory, JournalEntry

    entry = JournalEntry(
        fact_id="abc",
        content="Some quest.",
        category=FactCategory.Quest,
        source="Observation",
        confidence="confirmed",
        learned_turn=3,
    )
    assert entry.reference_url is None


def test_journal_entry_reference_url_serialises() -> None:
    """reference_url appears in model_dump(mode='json') output."""
    from sidequest.protocol.models import FactCategory, JournalEntry

    entry = JournalEntry(
        fact_id="abc",
        content="The Vicarage smells of rosewater.",
        category=FactCategory.Place,
        source="Observation",
        confidence="confirmed",
        learned_turn=3,
        reference_url="/reference/lore/tea_and_murder/glenross#location-the-vicarage",
    )
    payload = entry.model_dump(mode="json")
    assert payload["reference_url"] == (
        "/reference/lore/tea_and_murder/glenross#location-the-vicarage"
    )


# ---------------------------------------------------------------------------
# Task 8 — wiring test: JournalRequestHandler attaches reference_url
# ---------------------------------------------------------------------------


def test_journal_request_handler_attaches_reference_url_for_lore_match() -> None:
    """Integration: JournalRequestHandler._attach_reference_url populates
    reference_url when a Lore-category fact's content matches a legend name.

    Fixture-driven: synthetic _SessionData with a minimal GenrePack carrying
    one legend. No live genre_packs loaded.
    """
    import asyncio
    from unittest.mock import MagicMock, patch

    from sidequest.game.character import Character, KnownFact
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.legends import Legend
    from sidequest.handlers.journal_request import JournalRequestHandler
    from sidequest.protocol.models import FactCategory
    from sidequest.server.session_handler import _SessionData

    legend = Legend(name="The Curse of Glenross")
    world_mock = MagicMock()
    world_mock.legends = [legend]
    world_mock.history = None

    genre_pack = MagicMock()
    genre_pack.worlds = {"glenross": world_mock}

    snapshot = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=1),
        characters=[],
    )

    lore_fact = KnownFact(
        fact_id="f1",
        content="The Curse of Glenross",
        category=FactCategory.Lore,
        source="Observation",
        confidence="Certain",
        learned_turn=1,
    )
    character = Character(
        core=CreatureCore(
            name="Blackwood",
            description="A detective.",
            personality="Sharp.",
            inventory=Inventory(),
            hp=HpPool(current=8, max=10, base_max=10),
        ),
        backstory="From the city.",
        char_class="Detective",
        race="Human",
        known_facts=[lore_fact],
    )
    snapshot.characters.append(character)
    snapshot.player_seats["p1"] = "Blackwood"

    sd = _SessionData(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        player_name="Keith",
        player_id="p1",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )

    room = MagicMock()
    room.snapshot = snapshot
    room.slug = "test-room"

    session = MagicMock()
    session._room = room  # noqa: SLF001
    session._state = MagicMock()  # noqa: SLF001
    session._state.name = "playing"  # noqa: SLF001
    session._session_data = sd  # noqa: SLF001

    msg = MagicMock()
    msg.player_id = "p1"

    handler = JournalRequestHandler()

    # Suppress the OTEL span side-effects during test — the behaviour we assert
    # is the JournalEntry.reference_url value, not span emission.
    with patch("sidequest.handlers.journal_request.tracer"):
        result = asyncio.run(handler.handle(session, msg))

    assert len(result) == 1
    response = result[0]
    entries = response.payload.entries
    assert len(entries) == 1
    assert entries[0].reference_url is not None
    assert "tea_and_murder" in entries[0].reference_url
    assert "glenross" in entries[0].reference_url
    assert "the-curse-of-glenross" in entries[0].reference_url


def test_journal_request_handler_no_url_for_person_fact() -> None:
    """Person-category facts get reference_url=None — no span emitted.

    Person entries are excluded from URL attachment (npcs.yaml is not rendered).
    """
    import asyncio
    from unittest.mock import MagicMock, patch

    from sidequest.game.character import Character, KnownFact
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.legends import Legend
    from sidequest.handlers.journal_request import JournalRequestHandler
    from sidequest.protocol.models import FactCategory
    from sidequest.server.session_handler import _SessionData

    legend = Legend(name="Some Legend")
    world_mock = MagicMock()
    world_mock.legends = [legend]
    world_mock.history = None

    genre_pack = MagicMock()
    genre_pack.worlds = {"glenross": world_mock}

    snapshot = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=1),
        characters=[],
    )

    person_fact = KnownFact(
        fact_id="f2",
        content="Lady Ashford is the murderer.",
        category=FactCategory.Person,
        source="Observation",
        confidence="Suspected",
        learned_turn=2,
    )
    character = Character(
        core=CreatureCore(
            name="Blackwood",
            description="A detective.",
            personality="Sharp.",
            inventory=Inventory(),
            hp=HpPool(current=8, max=10, base_max=10),
        ),
        backstory="From the city.",
        char_class="Detective",
        race="Human",
        known_facts=[person_fact],
    )
    snapshot.characters.append(character)
    snapshot.player_seats["p1"] = "Blackwood"

    sd = _SessionData(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        player_name="Keith",
        player_id="p1",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )

    room = MagicMock()
    room.snapshot = snapshot
    room.slug = "test-room"

    session = MagicMock()
    session._room = room  # noqa: SLF001
    session._state = MagicMock()  # noqa: SLF001
    session._state.name = "playing"  # noqa: SLF001
    session._session_data = sd  # noqa: SLF001

    msg = MagicMock()
    msg.player_id = "p1"

    handler = JournalRequestHandler()

    with patch("sidequest.handlers.journal_request.tracer"):
        result = asyncio.run(handler.handle(session, msg))

    assert len(result) == 1
    entries = result[0].payload.entries
    assert len(entries) == 1
    assert entries[0].reference_url is None


# ---------------------------------------------------------------------------
# Task 9 — LocationEntity.reference_url field
# ---------------------------------------------------------------------------


def test_location_entity_accepts_reference_url() -> None:
    from sidequest.protocol.models import LocationEntity

    entity = LocationEntity(
        id="vicarage",
        label="The Vicarage",
        tier="real_object",
        reference_url="/reference/lore/tea_and_murder/glenross#location-the-vicarage",
    )
    assert entity.reference_url == ("/reference/lore/tea_and_murder/glenross#location-the-vicarage")


def test_location_entity_reference_url_defaults_to_none() -> None:
    from sidequest.protocol.models import LocationEntity

    entity = LocationEntity(
        id="vicarage",
        label="The Vicarage",
        tier="real_object",
    )
    assert entity.reference_url is None


# ---------------------------------------------------------------------------
# Task 9 — wiring test: compose_room_prose attaches reference_url
# ---------------------------------------------------------------------------


def test_compose_room_prose_attaches_reference_url_when_pack_and_world_given() -> None:
    """Integration: compose_room_prose populates reference_url on each entity
    when pack_id + world_slug are supplied.

    This is the behavioural wiring test required by CLAUDE.md
    ("Every Test Suite Needs a Wiring Test"). Uses a minimal LookDef fixture;
    no live genre_packs are loaded.
    """
    import random

    from sidequest.game.cookbook.compose import compose_room_prose
    from sidequest.game.cookbook.models import LookDef

    look_def = LookDef(
        id="dripping_cave",
        generator_binding="cellular",
        register="grim",
        dressing=[
            "Stalactites hang overhead like stone fingers.",
            "A pool of dark water reflects torchlight.",
            "The walls are streaked with mineral deposits.",
        ],
    )

    result = compose_room_prose(
        rng=random.Random(42),
        look_def=look_def,
        special_rooms=[],
        room_id="room-001",
        pack_id="tea_and_murder",
        world_slug="glenross",
    )

    assert result.entities, "expected at least one entity from dressing"
    for entity in result.entities:
        assert entity.reference_url is not None, (
            f"entity {entity.id!r} (label={entity.label!r}) has reference_url=None "
            "but pack_id + world_slug were supplied"
        )
        assert "tea_and_murder" in entity.reference_url
        assert "glenross" in entity.reference_url
        assert "location" in entity.reference_url


def test_compose_room_prose_reference_url_none_when_no_pack() -> None:
    """compose_room_prose leaves reference_url=None when pack_id is absent
    (existing callers without world context — no crash, no silent promotion).
    """
    import random

    from sidequest.game.cookbook.compose import compose_room_prose
    from sidequest.game.cookbook.models import LookDef

    look_def = LookDef(
        id="dripping_cave",
        generator_binding="cellular",
        register="grim",
        dressing=[
            "Stalactites hang overhead like stone fingers.",
            "A pool of dark water reflects torchlight.",
            "The walls are streaked with mineral deposits.",
        ],
    )

    result = compose_room_prose(
        rng=random.Random(42),
        look_def=look_def,
        special_rooms=[],
        room_id="room-001",
        # pack_id and world_slug intentionally omitted
    )

    for entity in result.entities:
        assert entity.reference_url is None
