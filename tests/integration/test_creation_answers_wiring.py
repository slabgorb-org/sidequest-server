"""Story 93-2 — wiring test: creation_answers reaches the serialized snapshot.

Mandatory wiring test per CLAUDE.md "Every Test Suite Needs a Wiring Test"
and the story's explicit AC: "a snapshot built from a real chargen flow
exposes creation_answers (asserts it reaches the serialized payload, not
just the in-memory model)".

Chain exercised (mirrors tests/integration/test_class_signature_wiring.py):
    char_creation.yaml (real caverns_and_claudes content)
    → CharacterBuilder scene walk (choice + StoryInput answers)
    → builder.build() → Character.creation_answers
    → party_member_from_character (views.py)
    → CharacterSheetDetails.creation_answers
    → model_dump() — the protocol shape the WS state-mirror sends

If build() doesn't populate the provenance, or the views.py wiring doesn't
copy it onto the sheet, or the protocol model lacks the field — this fails.
"""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.builder import CharacterBuilder, StoryInput
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _SessionData
from sidequest.server.views import party_member_from_character

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

_STORY_BACKGROUND = "Raised in the caverns by candle-cartographers."
_STORY_DESCRIPTION = "Steadfast, candlelit, scarred."


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _walk_chargen(pack, *, target_class: str = "Warrior", rng_seed: int = 42):
    """Walk the WWN point-buy C&C chargen flow; returns (character, walk_log).

    WWN port: the flow is the_calling → the_trade → the_story → the_kit →
    the_mouth (no the_roll / the_arrangement — stats come from the point-buy
    budget). the_calling and the_trade (choices) and the_story (freeform) are
    answered scenes; the_kit / the_mouth auto-advance.

    ``walk_log`` records the answered scenes as the walk makes them:
    [(scene_id, scene_title, kind, expected_value), ...] — ground truth
    for the provenance assertions, captured at answer time.
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

    walk_log: list[tuple[str, str, str, str]] = []

    # Scene 0: the_calling — pick the target Calling by class_hint.
    scene = builder.current_scene()
    assert scene.id == "the_calling", f"expected the_calling first, got {scene.id!r}"
    idx = next(
        (i for i, c in enumerate(scene.choices) if c.mechanical_effects.class_hint == target_class),
        None,
    )
    assert idx is not None, f"{target_class!r} not among {scene.choices}"
    walk_log.append((scene.id, scene.title, "choice", scene.choices[idx].label))
    builder.apply_choice(idx)
    # Scene 1: the_trade — WWN background pick (added in the chargen
    # reconciliation). Choose the first option.
    trade_scene = builder.current_scene()
    assert trade_scene.id == "the_trade", (
        f"expected the_trade second, got {trade_scene.id!r}"
    )
    walk_log.append(
        (trade_scene.id, trade_scene.title, "choice", trade_scene.choices[0].label)
    )
    builder.apply_choice(0)
    # Scene 2: the_story — pronouns + freeform background/description.
    story_scene = builder.current_scene()
    builder.apply_response(
        StoryInput(
            pronouns="they/them",
            background=_STORY_BACKGROUND,
            description=_STORY_DESCRIPTION,
        )
    )
    walk_log.append((story_scene.id, story_scene.title, "freeform", _STORY_BACKGROUND))
    # Scenes 3-4: the_kit / the_mouth — auto-advance (not answers).
    builder.apply_auto_advance()
    builder.apply_auto_advance()

    return builder.build("Wiring"), walk_log


def _make_session_data(pack, character) -> _SessionData:
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
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )


def test_real_chargen_flow_exposes_creation_answers_in_sheet_payload(cc_pack) -> None:
    """Full chain: real pack → chargen walk → views.py → serialized sheet."""
    character, walk_log = _walk_chargen(cc_pack)

    # The in-memory model carries the provenance...
    assert character.creation_answers, (
        "builder.build() must populate Character.creation_answers from the "
        "scene walk — the provenance layer is the whole point of 93-2"
    )

    sd = _make_session_data(cc_pack, character)
    party_member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="player:wiring",
        player_name="Wiring Player",
    )
    sheet = party_member.sheet

    # ...and views.py copies it onto the protocol sheet.
    answers = sheet.creation_answers
    assert answers, (
        "CharacterSheetDetails.creation_answers is empty — the views.py "
        "snapshot wiring is missing (in-memory model alone is not exposure)"
    )

    # Answered scenes appear in walk order with the values the player gave.
    answered = [(a.scene_id, a.kind) for a in answers]
    expected_order = [(sid, kind) for sid, _title, kind, _val in walk_log]
    assert answered == expected_order, (
        f"creation_answers must list exactly the ANSWERED scenes in walk "
        f"order; expected {expected_order}, got {answered}"
    )
    for entry, (sid, title, kind, expected_value) in zip(answers, walk_log, strict=True):
        assert entry.prompt == title, (
            f"{sid}: prompt must be the scene's title {title!r}, got {entry.prompt!r}"
        )
        if kind == "choice":
            assert entry.value == expected_value, (
                f"{sid}: choice value must be the chosen option label "
                f"{expected_value!r}, got {entry.value!r}"
            )
        else:
            assert expected_value in entry.value, (
                f"{sid}: freeform value must carry the player's verbatim "
                f"words; {expected_value!r} not in {entry.value!r}"
            )

    # Un-answered scenes (auto-advance) must NOT leak in.
    answered_ids = {a.scene_id for a in answers}
    for absent in ("the_kit", "the_mouth"):
        assert absent not in answered_ids, (
            f"{absent} was never answered by the player — it must not appear in creation_answers"
        )

    # The serialized payload — what the WS state-mirror actually sends —
    # carries the data, not just the live pydantic object.
    dumped = party_member.model_dump()
    dumped_answers = dumped["sheet"]["creation_answers"]
    assert dumped_answers, "creation_answers missing from the serialized sheet payload"
    assert dumped_answers[0]["scene_id"] == walk_log[0][0]
    assert dumped_answers[0]["value"] == walk_log[0][3]
    assert set(dumped_answers[0]) >= {
        "scene_id",
        "prompt",
        "kind",
        "value",
        "archetype_inferred",
    }, f"serialized entry is missing contract keys: {sorted(dumped_answers[0])}"
