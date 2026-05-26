"""Wiring tests for check_throw handler — CHECK_THROW reaches dispatch_check.

Drives the REAL ``handle_check_throw`` function with a minimal session/snapshot/
character fixture (copied from the dice_throw handler test pattern), verifies
that the handler returns a resolved ``CheckThrowOutcome`` and that the roll is
broadcast when a room is attached.

This is the integration test that proves CHECK_THROW is wired to dispatch_check
end-to-end — see CLAUDE.md "Every Test Suite Needs a Wiring Test".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.rules import RulesConfig, SwnConfig
from sidequest.handlers.check_throw import handle_check_throw
from sidequest.protocol.dice import RollOutcome
from sidequest.protocol.messages import CheckThrowMessage, CheckThrowPayload
from sidequest.server.dispatch.check import CheckThrowOutcome
from sidequest.server.session_handler import WebSocketSessionHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _swn_pack():
    """Minimal genre pack with SWN ruleset — mirrors test_check_dispatch.py."""
    rules = MagicMock(spec=RulesConfig)
    rules.ruleset = "swn"
    rules.swn = SwnConfig()
    pack = MagicMock()
    pack.rules = rules
    return pack


def _character_with_stats(name: str, **stats: int) -> Character:
    """Build a minimal Character with the supplied stats."""
    core = CreatureCore(
        name=name,
        description="a test adventurer",
        personality="resolute",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A wandering warrior.",
    )
    char.stats.update(stats)
    return char


def _snapshot_with_character(char: Character) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="test_genre")
    snap.characters.append(char)
    return snap


# ---------------------------------------------------------------------------
# Core wiring test — skill_check path
# ---------------------------------------------------------------------------


def test_handle_check_throw_skill_check_returns_outcome():
    """CHECK_THROW (skill_check) reaches dispatch_check and returns a resolved
    CheckThrowOutcome with a real RollOutcome — no improvisation possible."""
    char = _character_with_stats("Rux", DEXTERITY=14)
    snap = _snapshot_with_character(char)
    pack = _swn_pack()
    sent: list[object] = []

    def room_broadcast(msg: object) -> None:
        sent.append(msg)

    payload = CheckThrowPayload(
        kind="skill_check",
        attribute="DEXTERITY",
        skill_level=2,
        difficulty_key="tricky",
        label="Notice trap",
        faces=[4, 5],
    )

    outcome = handle_check_throw(
        payload,
        snapshot=snap,
        pack=pack,
        rolling_player_id="player-1",
        session_id="test:world:player-1",
        room_broadcast=room_broadcast,
    )

    assert isinstance(outcome, CheckThrowOutcome), (
        f"handle_check_throw must return CheckThrowOutcome, got {type(outcome)}"
    )
    assert isinstance(outcome.outcome, RollOutcome), (
        f"outcome.outcome must be a RollOutcome, got {type(outcome.outcome)}"
    )
    # 2d6: faces [4,5]=9, DEXTERITY=14 (+1 per SWN), skill_level=2 → mod=3
    # total = 12. "tricky" DC=10. margin=2 < DECISIVE_MARGIN=3 → Success.
    assert outcome.outcome is RollOutcome.Success
    assert outcome.result.total == 12


def test_handle_check_throw_broadcasts_request_and_result():
    """When room_broadcast is attached, dispatch_check broadcasts DiceRequest
    then DiceResult — two messages in order."""
    from sidequest.protocol.dice import DiceRequestPayload, DiceResultPayload

    char = _character_with_stats("Rux", DEXTERITY=14)
    snap = _snapshot_with_character(char)
    pack = _swn_pack()
    sent: list[object] = []

    payload = CheckThrowPayload(
        kind="skill_check",
        attribute="DEXTERITY",
        skill_level=2,
        difficulty_key="tricky",
        label="Notice trap",
        faces=[4, 5],
    )

    handle_check_throw(
        payload,
        snapshot=snap,
        pack=pack,
        rolling_player_id="player-1",
        session_id="test:world:player-1",
        room_broadcast=sent.append,
    )

    assert len(sent) == 2, (
        f"dispatch_check must broadcast exactly 2 messages (DiceRequest + DiceResult); "
        f"got {len(sent)}: {[type(m).__name__ for m in sent]}"
    )
    assert isinstance(sent[0], DiceRequestPayload), (
        f"first broadcast must be DiceRequestPayload, got {type(sent[0]).__name__}"
    )
    assert isinstance(sent[1], DiceResultPayload), (
        f"second broadcast must be DiceResultPayload, got {type(sent[1]).__name__}"
    )


# ---------------------------------------------------------------------------
# Save path
# ---------------------------------------------------------------------------


def test_handle_check_throw_save_path():
    """CHECK_THROW (save) resolves correctly via dispatch_check.save_params."""
    char = _character_with_stats("Rux", WISDOM=14, CHARISMA=8)
    snap = _snapshot_with_character(char)
    pack = _swn_pack()

    payload = CheckThrowPayload(
        kind="save",
        save="mental",
        faces=[13],
        label="Mental save",
    )

    outcome = handle_check_throw(
        payload,
        snapshot=snap,
        pack=pack,
        rolling_player_id="player-1",
        session_id="test:world:player-1",
        room_broadcast=None,
    )

    assert isinstance(outcome, CheckThrowOutcome)
    assert isinstance(outcome.outcome, RollOutcome)
    # WIS=14 → +1 mod; level defaults to 1 → save target = 15-(1-1)=15.
    # face [13] + 1 = 14 < 15 → Fail.
    assert outcome.outcome is RollOutcome.Fail


# ---------------------------------------------------------------------------
# Character resolution from player_seats
# ---------------------------------------------------------------------------


def test_handle_check_throw_resolves_character_from_player_seats():
    """Handler reads rolling_player_id → player_seats → character, mirroring
    the dice_throw handler's multiplayer seat resolution."""
    char = _character_with_stats("Jade", DEXTERITY=16)
    snap = _snapshot_with_character(char)
    snap.player_seats["pid-jade"] = "Jade"
    pack = _swn_pack()

    payload = CheckThrowPayload(
        kind="skill_check",
        attribute="DEXTERITY",
        skill_level=0,
        difficulty_key="tricky",
        faces=[6, 6],
        label="Spot danger",
    )

    outcome = handle_check_throw(
        payload,
        snapshot=snap,
        pack=pack,
        rolling_player_id="pid-jade",
        session_id="s",
        room_broadcast=None,
    )

    # DEX=16 → +2 mod, 2d6 faces [6,6]=12 + 2 = 14. tricky DC=10. margin=4 >= 3 → CritSuccess.
    assert outcome.outcome is RollOutcome.CritSuccess
    # character_name in the result should reflect Jade
    assert outcome.result.character_name == "Jade"


# ---------------------------------------------------------------------------
# Registry wiring test — CHECK_THROW is registered in the session handler
# ---------------------------------------------------------------------------


def test_check_throw_handler_is_registered():
    """Wiring assertion: WebSocketSessionHandler._message_handler_for('CHECK_THROW')
    returns a non-None handler, proving the registry entry exists."""
    registered = WebSocketSessionHandler._message_handler_for("CHECK_THROW")
    assert registered is not None, (
        "CHECK_THROW must be registered in WebSocketSessionHandler._MESSAGE_HANDLERS; "
        "add it to the registry in websocket_session_handler.py"
    )


# ---------------------------------------------------------------------------
# Message type parses through GameMessage discriminated union
# ---------------------------------------------------------------------------


def test_check_throw_message_type_is_routable():
    """CheckThrowMessage must parse through GameMessage without error."""
    from sidequest.protocol import GameMessage

    raw = CheckThrowMessage(
        payload=CheckThrowPayload(
            kind="skill_check",
            attribute="DEXTERITY",
            skill_level=1,
            difficulty_key="tricky",
            faces=[3, 4],
        ),
        player_id="p",
    ).model_dump_json()
    parsed = GameMessage.model_validate_json(raw)
    assert parsed.type.value == "CHECK_THROW"
    out = parsed.model_dump_json()
    assert '"type":"CHECK_THROW"' in out


# ---------------------------------------------------------------------------
# Fix 4 — CheckThrowHandler.handle surfaces dispatch_check errors as ErrorMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_throw_handler_returns_error_message_on_dispatch_failure(monkeypatch):
    """When dispatch_check raises ValueError, CheckThrowHandler.handle must return
    an ErrorMessage (not propagate the exception).

    We monkeypatch handle_check_throw to raise so no session/snapshot setup is needed.
    The test drives the async handle() on the PLAYING path and asserts the except
    branch returns the project's standard _error_msg shape (ErrorMessage).
    """
    import sidequest.handlers.check_throw as ct_mod
    from sidequest.handlers.check_throw import CheckThrowHandler
    from sidequest.protocol.messages import ErrorMessage
    from sidequest.server.session_handler import _State

    # Monkeypatch handle_check_throw to raise ValueError
    def _raise(*args, **kwargs):
        raise ValueError("save_params: unknown save category 'bogus'")

    monkeypatch.setattr(ct_mod, "handle_check_throw", _raise)

    # Build a minimal session mock in Playing state so the handler reaches the
    # dispatch call (past the state-guard and session_data guards).
    session = MagicMock()
    session._state = _State.Playing

    # Minimal session_data — the handler only reads these fields before delegating
    sd = MagicMock()
    sd.player_id = "p1"
    sd.genre_slug = "test_genre"
    sd.world_slug = "test_world"
    sd.snapshot = _snapshot_with_character(_character_with_stats("Rux", DEXTERITY=14))
    sd.genre_pack = _swn_pack()
    session._session_data = sd
    session._room = None  # no room — keeps broadcast=None path

    payload = CheckThrowPayload(
        kind="skill_check",
        attribute="DEXTERITY",
        difficulty_key="tricky",
        faces=[4, 5],
    )
    msg = CheckThrowMessage(payload=payload, player_id="p1")

    handler = CheckThrowHandler()
    result = await handler.handle(session, msg)

    assert len(result) == 1, (
        f"handle() must return exactly one message on dispatch failure; got {len(result)}: {result}"
    )
    assert isinstance(result[0], ErrorMessage), (
        f"handle() must return ErrorMessage on ValueError from dispatch_check; "
        f"got {type(result[0]).__name__}"
    )
