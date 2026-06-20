"""Story 2026-05-10 — full end-to-end wiring for class mechanical surface.

Mandatory wiring test per CLAUDE.md "Every Test Suite Needs a Wiring Test".

Drives a real chargen against the actual caverns_and_claudes genre pack content
and asserts the full AbilityDefinition + class_moves contract holds end-to-end
through the protocol shape that the WS state-mirror sends.

WWN port (2026-06-12): the B/X Cleric/Fighter/Thief signatures (Turn Undead /
Taunt / Backstab) were replaced by the WWN Callings' signatures — Warrior
(Killing Blow + Veteran's Luck), Expert (Read the Ledger), Mage (Read the
Worked Stone) — and chargen is a 4-scene point-buy flow (no roll/arrange).

Chain exercised:
    classes.yaml
    → genre loader (load_genre_pack)
    → CharacterBuilder._seed_class_abilities (builder.build())
    → Character.abilities
    → party_member_from_character (views.py)
    → CharacterSheetDetails.abilities + class_moves

If _seed_class_abilities is broken, or the views wiring for class_moves is
missing, or classes.yaml lacks the Calling abilities block — this test fails.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.builder import (
    CharacterBuilder,
    FreeformInput,
    SceneResult,
    StoryInput,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models import MechanicalEffects
from sidequest.protocol.models import AbilitySource
from sidequest.server.session_handler import _SessionData
from sidequest.server.views import party_member_from_character

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

# Combat beats that the WWN de-nativization (story 108-3 / ADR-143) removed from
# the per-class surface. Under a Without Number binding the native combat beats
# (``committed_blow``/``strike``) are stripped off the hp_depletion combat def and
# the WN round supplies the universal combat action set (attack / cast / Total
# Defense / Fighting Withdrawal) — so a WN class carries NONE of these as a
# per-class encounter beat. What survives in ``encounter_beat_choices`` (and
# therefore in ``class_moves``) is the chase/negotiation DIAL beats only.
_COMBAT_BEAT_IDS: frozenset[str] = frozenset(
    {"committed_blow", "strike", "attack", "cast_spell", "brace", "break_contact"}
)


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _build_character(pack, *, target_class: str):
    """Walk the WWN 4-scene point-buy chargen flow for the named Calling.

    Returns the finalised Character object (pronouns they/them so the
    pronoun-agnostic prose assertions exercise a non-default pronoun).
    """
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name("Wiring")
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )

    matched = False
    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup("Wiring")
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_response(
                    StoryInput(
                        pronouns="they/them",
                        background="Raised in the caverns.",
                        description="Steadfast, candlelit, scarred.",
                    )
                )
            continue
        idx = next(
            (
                i
                for i, c in enumerate(scene.choices)
                if c.mechanical_effects and c.mechanical_effects.class_hint == target_class
            ),
            None,
        )
        if idx is None:
            # A non-class choice-scene (e.g. the_trade's six background choices,
            # which carry background/focus_id/skill_grants but no class_hint).
            # Pick the first choice to advance — the background does not affect
            # class, kit, archetype, or class_moves.
            idx = 0
        else:
            matched = True
        builder.apply_choice(idx)

    if not matched:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=target_class),
            )
        )

    return builder.build("Wiring")


def _make_session_data(pack, character) -> _SessionData:
    """Construct a minimal _SessionData around a real pack + character."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[character],
    )
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        player_name="Wiring Player",
        player_id="player:wiring",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )


def _build_sheet(pack, *, target_class: str):
    """Drive full chain: pack → chargen → session data → protocol sheet."""
    character = _build_character(pack, target_class=target_class)
    sd = _make_session_data(pack, character)
    party_member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="player:wiring",
        player_name="Wiring Player",
    )
    return party_member.sheet


# ---------------------------------------------------------------------------
# Warrior wiring test — the WWN Warrior signature pair (Killing Blow + Veteran's
# Luck) flows end-to-end as Class-source abilities. class_moves carries only the
# surviving chase/negotiation DIAL beats; the native combat beat committed_blow
# is GONE under the WWN de-nativization (ADR-143), not unwired.
# ---------------------------------------------------------------------------


def test_warrior_chargen_yields_signature_pair_in_state_mirror(cc_pack):
    """A Warrior created in caverns_and_claudes shows the WWN Warrior signature
    pair in the protocol-shaped CharacterSheetDetails with source=Class and real
    prose. class_moves resolves only the surviving chase/negotiation DIAL beats —
    committed_blow (a native combat beat) is de-nativized away under the WWN
    binding (ADR-143), so the combat menu is the universal WWN action set, not a
    per-class beat."""
    sheet = _build_sheet(cc_pack, target_class="Warrior")

    assert sheet.abilities, "Expected Warrior abilities — _seed_class_abilities may not be wired"

    names = {a.name for a in sheet.abilities if a.source == AbilitySource.Class}
    assert names == {"Killing Blow", "Veteran's Luck"}, (
        f"Warrior must carry the WWN signature pair as Class abilities; got {names}"
    )
    for ability in sheet.abilities:
        assert ability.genre_description, f"{ability.name} genre_description must not be empty"
        assert "{writer agent" not in ability.genre_description, (
            f"{ability.name} genre_description contains placeholder text"
        )

    # class_moves under the de-nativized WWN surface (epic-152 / ADR-143): a WN
    # class carries NO per-class combat beat. committed_blow was the native
    # Warrior combat beat; 108-3 stripped it and the WN round now supplies the
    # universal combat action set (attack / cast / Total Defense / Fighting
    # Withdrawal). What remains in class_moves is the chase/negotiation DIAL
    # beats — populated, provenance-traceable, and label-resolved.
    move_ids = {m.id for m in sheet.class_moves}
    assert "committed_blow" not in move_ids, (
        "committed_blow must NOT appear in class_moves — the native Warrior "
        "combat beat was de-nativized under the WWN binding (ADR-143); combat is "
        "the universal WWN action set supplied by the engine, not a per-class beat"
    )
    assert not (move_ids & _COMBAT_BEAT_IDS), (
        f"WN class_moves must carry no per-class combat beat; found "
        f"{move_ids & _COMBAT_BEAT_IDS} — combat is de-nativized (ADR-143)"
    )
    warrior_def = next(c for c in cc_pack.classes if c.display_name == "Warrior")
    assert move_ids, "Warrior class_moves must still resolve the surviving DIAL beats"
    assert move_ids <= set(warrior_def.encounter_beat_choices), (
        f"class_moves must come from the Warrior's own encounter_beat_choices; "
        f"leaked {move_ids - set(warrior_def.encounter_beat_choices)}"
    )
    assert all(m.label for m in sheet.class_moves), (
        f"every class_move must resolve to a non-empty label; got {sheet.class_moves!r}"
    )


# ---------------------------------------------------------------------------
# Mage wiring test
# ---------------------------------------------------------------------------


def test_mage_chargen_yields_read_worked_stone_signature(cc_pack):
    """The WWN Mage carries one signature Class ability (Read the Worked Stone);
    its combat magic is the WN ``cast`` action gated through the rules.yaml
    cast_spell class_filter (story 152-2), not a per-class encounter beat. So no
    combat beat (cast_spell included) appears in class_moves — only the surviving
    chase/negotiation DIAL beats, which must still be populated and resolved.
    """
    sheet = _build_sheet(cc_pack, target_class="Mage")

    class_source = {a.name for a in sheet.abilities if a.source == AbilitySource.Class}
    assert class_source == {"Read the Worked Stone"}, (
        f"Mage must carry exactly the Read the Worked Stone signature; got {class_source}"
    )

    move_ids = {m.id for m in sheet.class_moves}
    assert move_ids, "Mage class_moves must still resolve the surviving DIAL beats"
    assert not (move_ids & _COMBAT_BEAT_IDS), (
        f"Mage class_moves must carry no per-class combat beat — cast is the WN "
        f"cast action gated by cast_spell class_filter, not an encounter beat; "
        f"found {move_ids & _COMBAT_BEAT_IDS}"
    )
    mage_def = next(c for c in cc_pack.classes if c.display_name == "Mage")
    assert move_ids <= set(mage_def.encounter_beat_choices), (
        f"class_moves must come from the Mage's own encounter_beat_choices; "
        f"leaked {move_ids - set(mage_def.encounter_beat_choices)}"
    )
    assert all(m.label for m in sheet.class_moves), (
        f"every Mage class_move must resolve to a non-empty label; got {sheet.class_moves!r}"
    )


# ---------------------------------------------------------------------------
# Class-signature pronoun agnosticism (sq-playtest 2026-05-17 / [BS-BUG-LOW])
# ---------------------------------------------------------------------------
#
# Beneath Sünden 3-player MP: a they/them PC saw a "He lifts…" class signature
# — the prose hardcoded a gendered subject pronoun with no substitution layer,
# so it mismatched any PC whose pronouns differ from the authored gender. The
# shipped signature prose must be pronoun-agnostic. The chargen helper builds
# with pronouns="they/them", driving the real classes.yaml → loader → builder →
# views chain.

# Whole-word gendered 3rd-person pronouns. "they/them/their/theirs/themself"
# are intentionally NOT here — singular-they is pronoun-safe. ``\b`` boundaries
# keep "the"/"there"/"where" from matching.
_GENDERED_PRONOUN_RE = re.compile(r"\b(?:he|she|his|him|her|hers|himself|herself)\b", re.IGNORECASE)


@pytest.mark.parametrize(
    ("target_class", "ability_name"),
    [
        ("Warrior", "Killing Blow"),
        ("Warrior", "Veteran's Luck"),
        ("Expert", "Read the Ledger"),
        ("Mage", "Read the Worked Stone"),
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
        f"Rewrite pronoun-agnostic (2nd/3rd-person-plural). Prose: {prose!r}"
    )
