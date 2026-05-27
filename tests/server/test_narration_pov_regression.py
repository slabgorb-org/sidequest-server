"""Regression sentinels for Story 49-8 — protect existing behavior.

The new POV-swap path must be opt-in: payloads without an
``_visibility`` sidecar (legacy events, event-log replay from a save
written before this story, narration emitted by code paths that don't
yet stamp the sidecar) MUST fan out unchanged to every recipient.

Pairs with ``test_narration_pov_emission.py`` (positive path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.session import GameSnapshot
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)
from sidequest.server.session_room import RoomRegistry

_GENRE = "caverns_and_claudes"
_WORLD = "sunden"
_SLUG = "pov-regression"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

_RULES_YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
"""


def _pc(name: str, pronouns: str = "he/him") -> Character:
    core = CreatureCore(
        name=name,
        description="Test PC.",
        personality="test",
        inventory=Inventory(),
    )
    return Character(
        core=core,
        backstory="A wanderer.",
        char_class="Fighter",
        race="Human",
        pronouns=pronouns,
    )


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: events/projection persist to Postgres)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _seed_game_row(tmp_path: Path):
    """Register the session in Postgres and return the PgSaveRepository."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=_SLUG,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    return repo


def _make_handler_two_pcs(tmp_path: Path) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [_pc("Carl"), _pc("Donut")]
    handler._session_data = _SessionData.__new__(_SessionData)
    handler._session_data.snapshot = snap
    handler._session_data.player_id = "p_carl"
    handler._session_data.genre_slug = _GENRE
    handler._session_data.world_slug = _WORLD

    store = _seed_game_row(tmp_path)
    repo = store
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter(
        rules=load_rules_from_yaml_str(_RULES_YAML),
        pack_slug=_GENRE,
    )
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.connect("p_donut", socket_id="sock-donut")
    handler._room = room
    return handler


# ---------------------------------------------------------------------------
# Legacy payload — no _visibility sidecar at all
# ---------------------------------------------------------------------------


def test_legacy_payload_without_visibility_sidecar_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NARRATION payload that does NOT carry _visibility is the
    pre-49-8 wire format. The fan-out must broadcast it verbatim to
    every recipient. No swap, no exception.

    This protects event-log replay from older saves and any code path
    that emits narration without going through the classifier (e.g.
    error/degraded turns)."""
    handler = _make_handler_two_pcs(tmp_path)
    q_carl: asyncio.Queue = asyncio.Queue()
    q_donut: asyncio.Queue = asyncio.Queue()
    handler._room.attach_outbound("sock-carl", q_carl)
    handler._room.attach_outbound("sock-donut", q_donut)

    from sidequest.server import session_handler as handler_module
    from sidequest.server import views as views_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)
    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    canonical = "Carl plants a boot on the moth's thorax."
    payload = {
        "text": canonical,
        "footnotes": [],
        # _visibility deliberately absent — pre-49-8 wire format.
    }

    handler._emit_event("NARRATION", payload)

    # Donut sees the original prose (current behavior — story does NOT
    # filter peer cards). No swap.
    assert q_donut.qsize() == 1
    donut_text = q_donut.get_nowait().payload["text"]
    assert donut_text == canonical, f"legacy payload to peer must be unchanged; got {donut_text!r}"


# ---------------------------------------------------------------------------
# Sidecar present but anchor_pc is null — atmospheric path
# ---------------------------------------------------------------------------


def test_sidecar_with_null_anchor_skips_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the classifier ran but found no PC anchor (atmospheric
    narration), the sidecar carries ``anchor_pc: None`` and
    ``pov_strategy: 'atmospheric'``. The swap path must short-circuit;
    every recipient sees the canonical prose unchanged."""
    handler = _make_handler_two_pcs(tmp_path)
    q_carl: asyncio.Queue = asyncio.Queue()
    q_donut: asyncio.Queue = asyncio.Queue()
    handler._room.attach_outbound("sock-carl", q_carl)
    handler._room.attach_outbound("sock-donut", q_donut)

    from sidequest.server import session_handler as handler_module
    from sidequest.server import views as views_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)
    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    canonical = "Rain hammers the slate roof. The torches gutter."
    payload = {
        "text": canonical,
        "footnotes": [],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {},
            "anchor_pc": None,
            "pov_strategy": "atmospheric",
        },
    }

    handler._emit_event("NARRATION", payload)

    assert q_donut.qsize() == 1
    assert q_donut.get_nowait().payload["text"] == canonical


# ---------------------------------------------------------------------------
# Sidecar present, anchor_pc maps to NO connected player_id
# ---------------------------------------------------------------------------


def test_anchor_pc_with_no_matching_recipient_no_op_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the classifier stamps anchor_pc='Mira' but no connected
    player_id maps to character 'Mira' (player disconnected mid-turn,
    NPC sneaking through), the fan-out must broadcast the canonical
    prose to all real recipients. The swap must NEVER fire on a
    recipient whose PC name does not match the anchor."""
    handler = _make_handler_two_pcs(tmp_path)
    q_carl: asyncio.Queue = asyncio.Queue()
    q_donut: asyncio.Queue = asyncio.Queue()
    handler._room.attach_outbound("sock-carl", q_carl)
    handler._room.attach_outbound("sock-donut", q_donut)

    from sidequest.server import session_handler as handler_module
    from sidequest.server import views as views_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)
    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    canonical = "Mira slides past the gate, ledger under her coat."
    payload = {
        "text": canonical,
        "footnotes": [],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {},
            "anchor_pc": "Mira",  # Not present in this session's snapshot
            "pov_strategy": "pc_anchored",
        },
    }

    handler._emit_event("NARRATION", payload)

    # Both recipients see the canonical prose; nobody gets a swap.
    assert q_donut.qsize() == 1
    donut_text = q_donut.get_nowait().payload["text"]
    assert "Mira slides past" in donut_text
    assert "You slide past" not in donut_text
