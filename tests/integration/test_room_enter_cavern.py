"""Integration tests — ADR-096 Task 20b: TACTICAL_GRID wiring.

Verifies that ``load_room_payload`` is reachable from gameplay via the
``_maybe_emit_tactical_grid`` helper. Per CLAUDE.md "Verify Wiring, Not Just
Existence" — the loader from Task 13 must be connected to the turn dispatch
path, not just importable.

Two test strategies:

1. Direct dispatch test — calls ``_maybe_emit_tactical_grid`` with a real
   caverns_sunden snapshot and asserts a TacticalGridPayload is emitted.
   Uses the ``mouth`` room (cavern) and ``masquerade`` room (settlement).

2. Message protocol test — asserts the TACTICAL_GRID MessageType and
   TacticalGridMessage are in the wire catalog and round-trip correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CAVERNS_PACK = REPO_ROOT / "sidequest-content" / "genre_packs" / "caverns_and_claudes"
CAVERNS_SUNDEN_WORLD = CAVERNS_PACK / "worlds" / "caverns_sunden"


def _packs_available() -> bool:
    return CAVERNS_PACK.exists() and CAVERNS_SUNDEN_WORLD.exists()


# ---------------------------------------------------------------------------
# Protocol catalog tests — TACTICAL_GRID in MessageType + GameMessage union
# ---------------------------------------------------------------------------


def test_tactical_grid_message_type_in_catalog() -> None:
    """TACTICAL_GRID must be registered in the MessageType enum."""
    from sidequest.protocol.enums import MessageType

    assert hasattr(MessageType, "TACTICAL_GRID"), (
        "MessageType.TACTICAL_GRID missing — wiring task 20b did not add it"
    )
    assert MessageType.TACTICAL_GRID == "TACTICAL_GRID"


def test_tactical_grid_message_round_trips() -> None:
    """TacticalGridMessage must serialize/deserialize correctly."""
    from sidequest.protocol.messages import GameMessage, TacticalGridMessage
    from sidequest.protocol.models import TacticalGridPayload

    payload = TacticalGridPayload(
        room_id="mouth",
        room_name="The Mouth",
        room_type="cavern",
        mask="##\n..",
        cavern_image_url="http://example.com/mouth.cavern.png",
        cell_size=28,
        cellular=None,
        derived=None,
        tokens=[],
        initiative=None,
    )
    msg = TacticalGridMessage(payload=payload, player_id="test")
    gm = GameMessage(root=msg)

    json_str = gm.to_json()
    assert '"type":"TACTICAL_GRID"' in json_str
    assert '"room_id":"mouth"' in json_str

    parsed = GameMessage.parse_json(json_str)
    assert parsed.type == "TACTICAL_GRID"
    assert parsed.payload.room_id == "mouth"
    assert parsed.payload.room_type == "cavern"


def test_tactical_grid_settlement_payload_includes_description() -> None:
    """Settlement TacticalGridPayload carries description + exits."""
    from sidequest.protocol.models import TacticalGridPayload

    payload = TacticalGridPayload(
        room_id="masquerade",
        room_name="The Masquerade",
        room_type="settlement",
        mask=None,
        cavern_image_url=None,
        cell_size=None,
        cellular=None,
        derived=None,
        tokens=[],
        initiative=None,
        settlement_description="A vaulted hall hung with carved wooden masks.",
        settlement_exits=[{"to": "sunden_square", "label": "north to Sünden Square"}],
    )
    assert payload.settlement_description == "A vaulted hall hung with carved wooden masks."
    assert payload.settlement_exits is not None
    assert payload.settlement_exits[0]["to"] == "sunden_square"


# ---------------------------------------------------------------------------
# Wiring test — _maybe_emit_tactical_grid reaches load_room_payload
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_maybe_emit_tactical_grid_cavern_room() -> None:
    """_maybe_emit_tactical_grid emits a TacticalGridPayload for the mouth room
    (cavern type) in caverns_sunden when the snapshot is bound to that world."""
    if not _packs_available():
        pytest.skip("caverns_and_claudes content pack not present")

    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.loader import load_genre_pack
    from sidequest.protocol.messages import TacticalGridMessage
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.websocket_session_handler import _maybe_emit_tactical_grid

    pack = load_genre_pack(CAVERNS_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
    )
    snap.character_locations["Rux"] = "mouth"
    snap.discovered_rooms = ["mouth"]

    # Build a minimal _SessionData (only the fields _maybe_emit_tactical_grid reads).
    from sidequest.agents.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)  # don't __init__ — no claude needed

    # Mock SqliteStore that does nothing
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        player_name="Rux",
        player_id="player-1",
        snapshot=snap,
        store=store,
        genre_pack=pack,
        orchestrator=orchestrator,
    )

    emitted: list[object] = []

    def capture_emit(msg: object, kind: str) -> None:
        emitted.append(msg)

    _maybe_emit_tactical_grid(
        None,  # handler — not used by the helper
        sd=sd,
        snapshot=snap,
        actor="Rux",
        emit_fn=capture_emit,
    )

    assert len(emitted) == 1, (
        f"Expected 1 TACTICAL_GRID message emitted; got {len(emitted)}. "
        "load_room_payload is not wired into the room-enter dispatch path."
    )
    msg = emitted[0]
    assert isinstance(msg, TacticalGridMessage), f"Expected TacticalGridMessage; got {type(msg)}"
    payload = msg.payload
    assert payload.room_id == "mouth"
    assert payload.room_type == "cavern"
    assert payload.cavern_image_url is not None
    assert payload.cavern_image_url.endswith("mouth.cavern.png"), payload.cavern_image_url
    assert payload.mask is not None and len(payload.mask) > 0


@pytest.mark.integration
def test_maybe_emit_tactical_grid_settlement_room() -> None:
    """_maybe_emit_tactical_grid emits a settlement TacticalGridPayload for masquerade."""
    if not _packs_available():
        pytest.skip("caverns_and_claudes content pack not present")

    from sidequest.agents.orchestrator import Orchestrator
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.loader import load_genre_pack
    from sidequest.protocol.messages import TacticalGridMessage
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.websocket_session_handler import _maybe_emit_tactical_grid

    pack = load_genre_pack(CAVERNS_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
    )
    snap.character_locations["Rux"] = "masquerade"
    snap.discovered_rooms = ["masquerade"]

    orchestrator = Orchestrator.__new__(Orchestrator)
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        player_name="Rux",
        player_id="player-1",
        snapshot=snap,
        store=store,
        genre_pack=pack,
        orchestrator=orchestrator,
    )

    emitted: list[object] = []

    def capture_emit(msg: object, kind: str) -> None:
        emitted.append(msg)

    _maybe_emit_tactical_grid(
        None,
        sd=sd,
        snapshot=snap,
        actor="Rux",
        emit_fn=capture_emit,
    )

    assert len(emitted) == 1, f"Expected 1 message; got {len(emitted)}"
    msg = emitted[0]
    assert isinstance(msg, TacticalGridMessage), f"Expected TacticalGridMessage; got {type(msg)}"
    payload = msg.payload
    assert payload.room_id == "masquerade"
    assert payload.room_type == "settlement"
    assert payload.cavern_image_url is None
    assert payload.mask is None


@pytest.mark.integration
def test_maybe_emit_tactical_grid_missing_room_is_silent() -> None:
    """_maybe_emit_tactical_grid must NOT crash when room YAML is absent.
    It should emit nothing (non-fatal silent skip per the docstring)."""
    if not _packs_available():
        pytest.skip("caverns_and_claudes content pack not present")

    from sidequest.agents.orchestrator import Orchestrator
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.loader import load_genre_pack
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.websocket_session_handler import _maybe_emit_tactical_grid

    pack = load_genre_pack(CAVERNS_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
    )
    snap.character_locations["Rux"] = "nonexistent_room_xyz"

    orchestrator = Orchestrator.__new__(Orchestrator)
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        player_name="Rux",
        player_id="player-1",
        snapshot=snap,
        store=store,
        genre_pack=pack,
        orchestrator=orchestrator,
    )

    emitted: list[object] = []

    def capture_emit(msg: object, kind: str) -> None:
        emitted.append(msg)

    # Must not raise; must emit nothing.
    _maybe_emit_tactical_grid(
        None,
        sd=sd,
        snapshot=snap,
        actor="Rux",
        emit_fn=capture_emit,
    )

    assert len(emitted) == 0, (
        f"Expected 0 messages for missing room; got {len(emitted)}. "
        "Missing room YAML should be a silent non-fatal skip."
    )
