"""Story 93-4 — unit tests for the character↔lore link resolver.

Surface the creation-seed lore fragments that belong to THIS character in
the History section. The fragments already live in the per-session
``LoreStore`` (seeded at chargen confirm by
:func:`seed_lore_from_char_creation`, story 75-15 / ADR-048); this story
adds the *link* that pulls the character's own fragments back out as a
typed, sheet-ready list.

Load-bearing design fact these tests pin
-----------------------------------------
``seed_lore_from_char_creation`` seeds **one fragment per choice in every
scene** — including the choices the player did NOT pick (see
``sidequest/game/lore_seeding.py``). So "linked to THIS character" cannot
mean "every CharacterCreation fragment in the store"; it must mean the
fragments whose ``metadata['choice_label']`` matches an answer the
character actually gave (``Character.creation_answers[].value`` for
``kind == 'choice'``). That value-match IS the per-character firewall —
two players who answered the same scene differently must never see each
other's pick in their History (AC6 / ADR-104/105).

Contract under test (defined by this RED suite — see the session
``## Design Deviations`` for the naming rationale):

    sidequest/game/lore_linking.py
        def linked_lore_for_character(
            store: LoreStore, character: Character
        ) -> list[LinkedLoreFragment]

    sidequest/protocol/models.py
        class LinkedLoreFragment(BaseModel):
            fragment_id: str
            title: str
            summary: str
            source: str
            lore_route: str | None = None
"""

from __future__ import annotations

import logging

from sidequest.game.character import Character, CreationAnswer
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.lore_linking import linked_lore_for_character
from sidequest.game.lore_store import (
    LoreCategory,
    LoreFragment,
    LoreSource,
    LoreStore,
)
from sidequest.protocol.models import LinkedLoreFragment

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_character(name: str, answers: list[CreationAnswer]) -> Character:
    """A minimal valid Character carrying ``creation_answers``.

    Mirrors ``tests/game/test_character.py::make_test_character`` so the
    P1-required fields are satisfied; only ``creation_answers`` matters to
    the linker.
    """
    return Character(
        core=CreatureCore(
            name=name,
            description="A test subject",
            personality="Stoic",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
            acquired_advancements=[],
        ),
        backstory="Raised somewhere",
        narrative_state="Standing around",
        hooks=[],
        char_class="Fighter",
        race="Human",
        pronouns="they/them",
        stats={"STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        abilities=[],
        known_facts=[],
        affinities=[],
        is_friendly=True,
        creation_answers=answers,
    )


def _seed_choice_fragment(
    store: LoreStore,
    *,
    scene_id: str,
    index: int,
    label: str,
    description: str,
) -> str:
    """Seed one creation-seed fragment exactly as
    :func:`seed_lore_from_char_creation` does, and return its id."""
    fragment = LoreFragment.new(
        id=f"lore_char_creation_{scene_id}_{index}",
        category=LoreCategory.Character,
        content=f"{label}: {description}",
        source=LoreSource.CharacterCreation,
        metadata={
            "scene_id": scene_id,
            "choice_index": str(index),
            "choice_label": label,
        },
    )
    store.add(fragment)
    return fragment.id


def _choice(scene_id: str, prompt: str, value: str) -> CreationAnswer:
    return CreationAnswer(scene_id=scene_id, prompt=prompt, kind="choice", value=value)


# ---------------------------------------------------------------------------
# AC1 — happy path: the character's own fragments come back, typed
# ---------------------------------------------------------------------------


def test_links_characters_own_creation_seed_fragments() -> None:
    store = LoreStore()
    frag_id = _seed_choice_fragment(
        store,
        scene_id="the_calling",
        index=2,
        label="Wasteland Mechanic",
        description="Keeps the convoy running on scavenged parts.",
    )

    char = _make_character(
        "Vesska",
        [_choice("the_calling", "What is your calling?", "Wasteland Mechanic")],
    )

    linked = linked_lore_for_character(store, char)

    assert len(linked) == 1, f"expected the one linked fragment, got {linked!r}"
    item = linked[0]
    assert isinstance(item, LinkedLoreFragment)
    # Typed list of {fragment_id, title/summary, source} (AC1).
    assert item.fragment_id == frag_id
    assert item.source == LoreSource.CharacterCreation
    # Title is the chosen option label; summary carries the fragment body.
    assert item.title == "Wasteland Mechanic"
    assert "Keeps the convoy running" in item.summary


def test_returns_empty_for_character_with_no_answers() -> None:
    """A legacy character with no creation_answers links to nothing — and
    must not crash or fabricate (graceful, like 93-2/93-3 absence guards)."""
    store = LoreStore()
    _seed_choice_fragment(
        store,
        scene_id="the_calling",
        index=0,
        label="Cleric",
        description="Tends the candleflame.",
    )
    char = _make_character("Legacy", [])

    assert linked_lore_for_character(store, char) == []


# ---------------------------------------------------------------------------
# AC1/AC6 — only character-creation, character-linked fragments surface
# ---------------------------------------------------------------------------


def test_world_and_event_lore_are_excluded() -> None:
    """World lore (GenrePack) and arc lore (GameEvent) live in the same
    per-session store but are NOT this character's personal History — they
    must never leak into the link result."""
    store = LoreStore()
    own = _seed_choice_fragment(
        store,
        scene_id="the_calling",
        index=1,
        label="Candle-Cartographer",
        description="Maps the dark by waxlight.",
    )
    # World lore — different source, no chargen metadata.
    store.add(
        LoreFragment.new(
            id="lore_world_mawdeep_history",
            category=LoreCategory.History,
            content="Mawdeep was carved by the first delvers.",
            source=LoreSource.GenrePack,
            metadata={"world_slug": "mawdeep"},
        )
    )
    # Arc/event lore — game-event source.
    store.add(
        LoreFragment.new(
            id="lore_arc_ch1_0",
            category=LoreCategory.History,
            content="The reactor woke on the third night.",
            source=LoreSource.GameEvent,
            metadata={"chapter_id": "ch1", "lore_index": "0"},
        )
    )

    char = _make_character(
        "Mapper",
        [_choice("the_calling", "What is your calling?", "Candle-Cartographer")],
    )

    linked = linked_lore_for_character(store, char)
    ids = {item.fragment_id for item in linked}
    assert ids == {own}, (
        "only the character's own creation-seed fragment should surface; "
        f"world/event lore leaked: {ids}"
    )
    assert all(item.source == LoreSource.CharacterCreation for item in linked)


def test_unpicked_sibling_choice_in_same_scene_does_not_surface() -> None:
    """The seeder stores a fragment for EVERY choice in a scene, including
    ones the player rejected. Only the chosen label may surface — this is
    the core firewall mechanism."""
    store = LoreStore()
    picked = _seed_choice_fragment(
        store,
        scene_id="the_calling",
        index=0,
        label="Cleric",
        description="Tends the candleflame.",
    )
    # Sibling choice in the same scene — seeded, but NOT picked by this PC.
    _seed_choice_fragment(
        store,
        scene_id="the_calling",
        index=1,
        label="Rogue",
        description="Cuts purses in the dark.",
    )

    char = _make_character("Pious", [_choice("the_calling", "What is your calling?", "Cleric")])

    linked = linked_lore_for_character(store, char)
    assert [i.fragment_id for i in linked] == [picked]
    assert all(i.title != "Rogue" for i in linked), "unpicked sibling choice leaked"


def test_cross_character_firewall_shared_store() -> None:
    """Two PCs answer the SAME scene with DIFFERENT picks against one shared
    store. Each PC's History must contain only its own pick (AC6 — another
    player's fragments do NOT leak into this character's History)."""
    store = LoreStore()
    a_id = _seed_choice_fragment(
        store, scene_id="the_calling", index=0, label="Cleric", description="Faith."
    )
    b_id = _seed_choice_fragment(
        store, scene_id="the_calling", index=1, label="Rogue", description="Guile."
    )

    alice = _make_character("Alice", [_choice("the_calling", "What is your calling?", "Cleric")])
    bob = _make_character("Bob", [_choice("the_calling", "What is your calling?", "Rogue")])

    alice_ids = {i.fragment_id for i in linked_lore_for_character(store, alice)}
    bob_ids = {i.fragment_id for i in linked_lore_for_character(store, bob)}

    assert alice_ids == {a_id}, f"Alice saw something not hers: {alice_ids}"
    assert bob_ids == {b_id}, f"Bob saw something not his: {bob_ids}"
    assert a_id not in bob_ids and b_id not in alice_ids


# ---------------------------------------------------------------------------
# AC4 — No Silent Fallback: an unresolvable link is logged + skipped, never
#       fabricated
# ---------------------------------------------------------------------------


def test_unresolvable_choice_answer_is_skipped_loudly_not_fabricated(
    caplog,
) -> None:
    """A CHOICE answer whose (scene_id, label) has no matching fragment in
    the store (e.g. a resume where seeding didn't run, or a content edit
    removed the choice) must be skipped with a WARNING — never turned into a
    fabricated lore row from the answer text alone."""
    store = LoreStore()
    # Only the_calling is seeded; the_origin answer below has NO fragment.
    real_id = _seed_choice_fragment(
        store,
        scene_id="the_calling",
        index=0,
        label="Cleric",
        description="Tends the candleflame.",
    )

    char = _make_character(
        "Ghosted",
        [
            _choice("the_calling", "What is your calling?", "Cleric"),
            _choice("the_origin", "Where are you from?", "The Sunken Vault"),
        ],
    )

    with caplog.at_level(logging.WARNING):
        linked = linked_lore_for_character(store, char)

    # The resolvable answer surfaced; the unresolvable one did NOT.
    assert [i.fragment_id for i in linked] == [real_id]
    assert all("Sunken Vault" not in i.title for i in linked), "fabricated a row"
    assert all("Sunken Vault" not in i.summary for i in linked), "fabricated body"

    # ...and it failed LOUDLY (No Silent Fallbacks).
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unresolvable lore link must emit a WARNING, not pass silently"
    assert any("the_origin" in r.getMessage() for r in warnings), (
        "the warning must name the unresolved scene so it's debuggable; "
        f"got {[r.getMessage() for r in warnings]}"
    )


def test_freeform_answers_do_not_warn() -> None:
    """Freeform answers are not creation-seed-fragment-backed (the seeder
    only iterates ``scene.choices``). A freeform answer with no fragment is
    EXPECTED-absent and must NOT produce a loud-skip warning — only genuine
    choice misses are unresolvable."""
    store = LoreStore()
    _seed_choice_fragment(
        store, scene_id="the_calling", index=0, label="Cleric", description="Faith."
    )
    char = _make_character(
        "Wordy",
        [
            _choice("the_calling", "What is your calling?", "Cleric"),
            CreationAnswer(
                scene_id="the_story",
                prompt="Tell us your story",
                kind="freeform",
                value="I crawled out of a suspension pod beneath the salt flats.",
            ),
        ],
    )

    import logging as _logging

    logger = _logging.getLogger("sidequest.game.lore_linking")
    # Capture without asserting on a specific message: there must be ZERO
    # warnings for the freeform answer.
    records: list[_logging.LogRecord] = []
    handler = _logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        linked = linked_lore_for_character(store, char)
    finally:
        logger.removeHandler(handler)

    assert len(linked) == 1, "the choice answer should still resolve"
    assert not [r for r in records if r.levelno >= _logging.WARNING], (
        "freeform answers are expected to have no fragment — must not warn"
    )


def test_no_fabricated_lore_route_when_no_page_exists() -> None:
    """Creation-seed fragments have no dedicated lore page route yet. The
    linker must leave ``lore_route`` as ``None`` rather than inventing an
    href (No Silent Fallbacks — a dead link is worse than no link)."""
    store = LoreStore()
    _seed_choice_fragment(
        store, scene_id="the_calling", index=0, label="Cleric", description="Faith."
    )
    char = _make_character("Pious", [_choice("the_calling", "What is your calling?", "Cleric")])

    linked = linked_lore_for_character(store, char)
    assert linked and linked[0].lore_route is None
