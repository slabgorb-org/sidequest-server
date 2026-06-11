"""Story 103-3 RED — Roll the Bones over the wire (build plan §D-C).

End-to-end dispatch tests through CharacterCreationHandler →
WebSocketSessionHandler → CharacterBuilder, against a synthetic pack
(minimal_pack_factory clone) whose char_creation.yaml carries the new
genre-tier flags. This is the wiring proof the story context demands:
the mode is reachable from REAL chargen, loaded by the PRODUCTION
loader, driven through the PRODUCTION dispatch path.

Pinned wire contract (mirrors arrange_* / the 103-2 stock frame):

- Picking the Roll the Bones choice answers with the standard
  ``{phase: "scene", choice: "<i+1>"}`` protocol — no new message types.
- The bones frame is a CharacterCreationMessage with
  ``input_type == "roll_the_bones"``, six ``rolled_stats`` in pack
  ability order, and ``reroll_budget_remaining == 2``.
- Visible dice (ADR-074, AC-4): the server broadcasts the EXISTING
  DiceResultMessage per stat roll — one DieGroupResult of three d6
  faces, total == the stat value, ``request_id == "chargen.bones.<STAT>"``.
  The dice are on the wire, not improvised prose.
- Client ops: ``{phase: "bones_reroll", stat}`` and
  ``{phase: "bones_confirm"}`` (reusing the existing ``stat`` field).
- Budget exhaustion over the wire is an ErrorMessage, loud.
"""

from __future__ import annotations

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    DiceResultMessage,
    ErrorMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import (
    mock_claude_client_factory as _mock_claude_client_factory,
)

BONES_CHAR_CREATION = [
    {
        "id": "the_wager",
        "title": "The Wager",
        "narration": "How do you meet fate — ledger or bones?",
        "choices": [
            {
                "label": "The Measured Path",
                "description": "Take the pack default.",
                "mechanical_effects": {},
            },
            {
                "label": "Roll the Bones",
                "description": "3d6, in order, and the dice stand.",
                "mechanical_effects": {"stat_generation": "roll_the_bones"},
            },
        ],
    },
    {
        "id": "the_bones",
        "title": "The Bones",
        "narration": "Six casts, six fates, in order.",
        "requires_stat_generation": "roll_the_bones",
    },
    {
        "id": "the_name",
        "title": "Your Name",
        "narration": "Speak it.",
        "allows_freeform": True,
    },
]


@pytest.fixture
def bones_pack_root(minimal_pack_factory, tmp_path):
    """Clone the synthetic pack and overwrite char_creation.yaml with the
    Roll the Bones scene list. Returns the search root (parent of test_pack)."""
    pack = minimal_pack_factory(tmp_path)
    cc_path = pack.path / "char_creation.yaml"
    with cc_path.open("w", encoding="utf-8") as f:
        yaml.dump(BONES_CHAR_CREATION, f, default_flow_style=False, sort_keys=False)
    return tmp_path


@pytest.fixture
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process db_pool at a fresh throwaway Postgres database.

    ``seed_slug_for_test`` requires this (see its docstring) — without it
    the connect path reads/writes whatever SIDEQUEST_DATABASE_URL points
    at (the live dev database)."""
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture
def handler(bones_pack_root, tmp_path, _pg_isolation):
    save_dir = tmp_path / "saves"
    save_dir.mkdir(exist_ok=True)
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[bones_pack_root],
        save_dir=save_dir,
    )


async def _connect(handler: WebSocketSessionHandler) -> None:
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(handler._save_dir, genre="test_pack", world="flickering_reach")
    attach_default_room_context(handler)
    payload = SessionEventPayload(event="connect", player_name="TestPlayer", game_slug=slug)
    msg = SessionEventMessage(payload=payload, player_id="")
    out = await handler.handle_message(msg)
    assert isinstance(out[0], SessionEventMessage)


async def _send(
    handler: WebSocketSessionHandler,
    payload: CharacterCreationPayload,
    player_id: str = "test-pid",
) -> list:
    msg = CharacterCreationMessage(payload=payload, player_id=player_id)
    return await handler.handle_message(msg)


def _last_chargen_payload(out: list) -> CharacterCreationPayload:
    frames = [m for m in out if isinstance(m, CharacterCreationMessage)]
    assert frames, f"no CharacterCreationMessage in {[type(m).__name__ for m in out]}"
    return frames[-1].payload


def _ability_order(handler: WebSocketSessionHandler) -> list[str]:
    sd = handler._session_data  # type: ignore[attr-defined]
    return list(sd.genre_pack.rules.ability_score_names)


# ---------------------------------------------------------------------------
# Loader wiring — the new flags parse through the PRODUCTION loader
# ---------------------------------------------------------------------------


def test_loader_accepts_roll_the_bones_flags(bones_pack_root) -> None:
    pack = load_genre_pack(bones_pack_root / "test_pack")
    scenes = {s.id: s for s in pack.char_creation}
    wager = scenes["the_wager"]
    assert wager.choices[1].mechanical_effects.stat_generation == "roll_the_bones"
    assert scenes["the_bones"].requires_stat_generation == "roll_the_bones"
    assert scenes["the_name"].requires_stat_generation is None


# ---------------------------------------------------------------------------
# AC-1: both paths over the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_choice_skips_bones_and_rolls_nothing(handler) -> None:
    await _connect(handler)
    out = await _send(handler, CharacterCreationPayload(phase="scene", choice="1"))
    payload = _last_chargen_payload(out)
    assert payload.input_type != "roll_the_bones"
    assert "name" in (payload.prompt or "").lower() or payload.allows_freeform
    assert not [m for m in out if isinstance(m, DiceResultMessage)], (
        "default path must not broadcast dice"
    )


@pytest.mark.asyncio
async def test_bones_choice_presents_bones_frame_with_budget(handler) -> None:
    await _connect(handler)
    out = await _send(handler, CharacterCreationPayload(phase="scene", choice="2"))
    payload = _last_chargen_payload(out)
    assert payload.input_type == "roll_the_bones"
    rolled = payload.rolled_stats or []
    assert [r.name for r in rolled] == _ability_order(handler)
    assert all(3 <= r.value <= 18 for r in rolled)
    assert getattr(payload, "reroll_budget_remaining", None) == 2


# ---------------------------------------------------------------------------
# AC-4: visible dice — DiceResult broadcasts, faces on the record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bones_roll_broadcasts_dice_results_per_stat(handler) -> None:
    await _connect(handler)
    out = await _send(handler, CharacterCreationPayload(phase="scene", choice="2"))
    payload = _last_chargen_payload(out)
    rolled = {r.name: r.value for r in payload.rolled_stats or []}
    dice_msgs = [m for m in out if isinstance(m, DiceResultMessage)]
    assert len(dice_msgs) == 6, "one visible DiceResult per ability score"
    seen: list[str] = []
    for msg in dice_msgs:
        dp = msg.payload
        prefix = "chargen.bones."
        assert dp.request_id.startswith(prefix), dp.request_id
        stat = dp.request_id.removeprefix(prefix)
        seen.append(stat)
        assert len(dp.rolls) == 1
        group = dp.rolls[0]
        assert int(group.spec.sides) == 6
        assert group.spec.count == 3
        assert len(group.faces) == 3
        assert all(1 <= f <= 6 for f in group.faces)
        assert dp.total == sum(group.faces) == rolled[stat]
    assert seen == _ability_order(handler)


# ---------------------------------------------------------------------------
# AC-3: reroll ops and server-side budget enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bones_reroll_op_rerolls_and_decrements_budget(handler) -> None:
    await _connect(handler)
    await _send(handler, CharacterCreationPayload(phase="scene", choice="2"))
    order = _ability_order(handler)
    out = await _send(handler, CharacterCreationPayload(phase="bones_reroll", stat=order[1]))
    payload = _last_chargen_payload(out)
    assert payload.input_type == "roll_the_bones"
    assert getattr(payload, "reroll_budget_remaining", None) == 1
    rolled = payload.rolled_stats or []
    assert [r.name for r in rolled] == order
    assert all(3 <= r.value <= 18 for r in rolled)
    dice_msgs = [m for m in out if isinstance(m, DiceResultMessage)]
    assert len(dice_msgs) == 1, "a reroll is one visible DiceResult"
    assert dice_msgs[0].payload.request_id == f"chargen.bones.{order[1]}"


@pytest.mark.asyncio
async def test_third_reroll_rejected_over_wire(handler) -> None:
    await _connect(handler)
    await _send(handler, CharacterCreationPayload(phase="scene", choice="2"))
    order = _ability_order(handler)
    await _send(handler, CharacterCreationPayload(phase="bones_reroll", stat=order[0]))
    await _send(handler, CharacterCreationPayload(phase="bones_reroll", stat=order[1]))
    out = await _send(handler, CharacterCreationPayload(phase="bones_reroll", stat=order[2]))
    errors = [m for m in out if isinstance(m, ErrorMessage)]
    assert errors, "exhausted budget must be a loud wire error"
    assert "reroll" in str(errors[0].payload.model_dump()).lower()
    assert not [m for m in out if isinstance(m, DiceResultMessage)], (
        "a rejected reroll must not roll dice"
    )


@pytest.mark.asyncio
async def test_bones_confirm_advances_to_next_scene(handler) -> None:
    await _connect(handler)
    await _send(handler, CharacterCreationPayload(phase="scene", choice="2"))
    out = await _send(handler, CharacterCreationPayload(phase="bones_confirm"))
    payload = _last_chargen_payload(out)
    assert payload.input_type != "roll_the_bones"
    # the_bones is the last gated scene; next stop is the name scene
    assert "name" in (payload.prompt or "").lower() or payload.allows_freeform


# ---------------------------------------------------------------------------
# Review rework (2026-06-11 [HIGH]): back out of bones, pick default — the
# wire must honor the player's final choice. No bones frame, no dice.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_back_then_default_choice_skips_bones_over_wire(handler) -> None:
    await _connect(handler)
    out = await _send(handler, CharacterCreationPayload(phase="scene", choice="2"))
    assert _last_chargen_payload(out).input_type == "roll_the_bones"
    out = await _send(handler, CharacterCreationPayload(action="back"))
    payload = _last_chargen_payload(out)
    assert payload.input_type != "roll_the_bones", "back must land on the wager scene"
    out = await _send(handler, CharacterCreationPayload(phase="scene", choice="1"))
    payload = _last_chargen_payload(out)
    assert payload.input_type != "roll_the_bones", (
        "stale bones mode leaked over the wire after the player chose default"
    )
    assert "name" in (payload.prompt or "").lower() or payload.allows_freeform
    assert not [m for m in out if isinstance(m, DiceResultMessage)], (
        "default path after back must not broadcast dice"
    )
