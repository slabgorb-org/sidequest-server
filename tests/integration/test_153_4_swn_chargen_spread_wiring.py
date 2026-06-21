"""Story 153-4 — WIRING proof: building a real space_opera SWN character through
the REAL ``CharacterBuilder.build()`` produces the shaped WN "14-to-7" attribute
spread, not flat point-buy stats.

This is the production-reachability test required by CLAUDE.md ("Every Test Suite
Needs a Wiring Test"). It is a behavior test (load the real pack, walk the real
char_creation scenes, build the character, inspect the result) — NOT a
source-text grep. It catches the fix whether it lands in the WN ruleset seam
(server) or, equivalently, makes the WN family own the spread.

FINDING: a fresh aureate_span Officer currently comes out
``{Influence:13, Physique:13, Reflex:13, Intellect:12, Cunning:12, Resolve:12}``
— every WN modifier is +0 (8-13 band). Mechanically flat: no prime edge, no dump
stat. The shaped WWN/SWN SRD array ``[14, 12, 11, 10, 9, 7]`` gives the prime a
+1 and a dump stat a -1.

Skips cleanly when sidequest-content is not present on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The canonical WWN/SWN SRD standard array — the "14-to-7 spread" the story names.
WN_SHAPED_SPREAD = [14, 12, 11, 10, 9, 7]


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_space_opera():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _build_first_choice_character(pack, world_slug: str, name: str):
    """Walk the real char_creation scenes picking choice 0 everywhere, then build.

    Mirrors the deterministic walk in test_wwn_elemental_harmony_chargen.py — no
    Claude calls. The aureate_span flow is
    ``origins → pronouns → crucible → drive → role → confirmation`` with no
    LLM-blocking autogen scene, so choice-0 / auto-advance / followup reaches
    confirmation. The ``crucible`` scene's choice 0 sets the Officer class_hint."""
    from sidequest.game.builder import CharacterBuilder
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=world_slug)
    assert scenes, f"space_opera/{world_slug} must declare char_creation scenes"

    builder = (
        CharacterBuilder(
            scenes=scenes,
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name(name)
        .with_classes(pack.classes)
    )
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)

    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue
        builder.apply_choice(0)

    return builder.build(name)


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_real_swn_chargen_produces_shaped_spread() -> None:
    """A real aureate_span SWN character carries the WN 14-to-7 spread, not flat
    point-buy stats."""
    pack = _load_space_opera()
    assert pack.rules.ruleset == "swn", f"space_opera must bind swn; got {pack.rules.ruleset!r}"

    char = _build_first_choice_character(pack, "aureate_span", "Kael Voss")

    assert sorted(char.stats.values(), reverse=True) == WN_SHAPED_SPREAD, (
        f"real SWN chargen must emit the WN 14-to-7 spread {WN_SHAPED_SPREAD}; "
        f"got {char.stats} (flat point-buy is the bug)"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_real_swn_character_is_not_mechanically_flat() -> None:
    """The built character has a real mechanical shape — multiple WN modifier
    bands across its stats (the heart of the finding)."""
    from sidequest.game.ruleset.swn import swn_attribute_modifier

    pack = _load_space_opera()
    char = _build_first_choice_character(pack, "aureate_span", "Kael Voss")

    modifiers = {swn_attribute_modifier(v) for v in char.stats.values()}
    assert len(modifiers) >= 2, (
        f"real SWN character is mechanically flat — every stat shares one modifier "
        f"{modifiers}. Stats: {char.stats}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_real_swn_prime_requisite_gets_top_value() -> None:
    """The chosen Calling's prime requisite is the single highest score (14, +1)."""
    pack = _load_space_opera()
    char = _build_first_choice_character(pack, "aureate_span", "Kael Voss")

    class_def = next((c for c in pack.classes if c.display_name == char.char_class), None)
    assert class_def is not None, f"built class {char.char_class!r} not in pack roster"
    prime = class_def.prime_requisite

    assert char.stats.get(prime) == 14, (
        f"{char.char_class}'s prime ({prime}) must land the spread's top value 14; "
        f"got {char.stats.get(prime)}. Stats: {char.stats}"
    )
    assert list(char.stats.values()).count(14) == 1, (
        f"the prime must be the SOLE top score; got {char.stats}"
    )
