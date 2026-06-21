"""WIRING (Story 153-7, ADR-151) — the DEFEND-barrier resume helpers must be
reachable from the REAL connect/resume bootstrap, not dead code.

``connect.py``'s ``_State.Playing`` branch re-emits FATE_STATE / PARTY_STATUS /
LOCATION_DESCRIPTION / MAP_UPDATE on resume but emits NOTHING for a parked DEFEND
barrier. This test drives the real ``WebSocketSessionHandler`` connect path against
a PG-seeded parked Fate conflict and pins two production wirings:

  1. **Re-emit** — a reconnecting defender with an unfilled ``pending_defenses``
     entry receives a FATE_DEFEND_REQUEST in the bootstrap so they can throw
     (otherwise the barrier never fills and the round wedges).
  2. **Orphan sweep** — a pending entry whose defender is no longer a live seated
     PC is conceded on reconnect so the ledger can resume, WITHOUT touching a
     present defender's still-open entry.

Integration-level wiring test (real Fate content pack + real connect handler),
mirroring ``test_location_description_resume.py``. ``clear_orphaned_pending_defenses``
and ``_maybe_reemit_pending_defenses`` are exercised through the production path,
never a source grep (server CLAUDE.md). RED until 153-7 wires them into the
resume bootstrap.

Requires Postgres (the ``migrated_db`` fixture): the slug-connect path reads the
authoritative snapshot from the PG repository.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.encounter import FatePendingDefense
from sidequest.game.persistence import GameMode
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    FateDefendRequestMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry
from tests._helpers.fate_fixtures import parked_conflict

_GENRE = "pulp_noir"  # ruleset: fate
_WORLD = "annees_folles"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database (mirrors
    ``test_location_description_resume._pg_isolation``)."""
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture(autouse=True)
def _real_content_packs(monkeypatch: pytest.MonkeyPatch):
    """Resolve the real Fate content pack (server conftest pins the loader at frozen
    fixture packs that have no Fate combat world)."""
    import sidequest.genre.loader as _loader_mod

    monkeypatch.setattr(_loader_mod, "DEFAULT_GENRE_PACK_SEARCH_PATHS", [_CONTENT_SEARCH_PATH])


def _make_handler(save_dir: Path) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-test",
        out_queue=asyncio.Queue(),
    )
    return handler


def _seed(slug: str, snap) -> None:  # noqa: ANN001
    """Persist a parked-conflict snapshot under ``slug`` so the slug-resume path
    loads it from PG."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    repo.save(snap)


def _connect_msg(slug: str, player_id: str, player_name: str) -> SessionEventMessage:
    return SessionEventMessage(
        type="SESSION_EVENT",
        player_id=player_id,
        payload=SessionEventPayload(event="connect", game_slug=slug, player_name=player_name),
    )


@pytest.mark.asyncio
async def test_slug_resume_reemits_fate_defend_request_for_parked_defender(tmp_path: Path):
    """A reconnecting defender with an unfilled pending defense gets a
    FATE_DEFEND_REQUEST in the resume bootstrap so they can throw."""
    slug = "2026-06-21-noir-defend-resume"
    snap, _enc = parked_conflict(defender="Vance", attacker="Mook", request_id="d1", attack_total=4)
    snap.genre_slug = _GENRE
    snap.world_slug = _WORLD
    snap.player_seats["vance-player"] = "Vance"
    _seed(slug, snap)

    handler = _make_handler(tmp_path)
    outbound = await handler.handle_message(_connect_msg(slug, "vance-player", "Vance"))

    defend_msgs = [m for m in outbound if isinstance(m, FateDefendRequestMessage)]
    assert defend_msgs, (
        "Expected a FATE_DEFEND_REQUEST on slug resume for a parked DEFEND barrier "
        f"so the defender can throw. Got types: {[getattr(m, 'type', None) for m in outbound]}"
    )
    assert defend_msgs[0].payload.defender == "Vance"
    assert defend_msgs[0].payload.request_id == "d1"
    assert defend_msgs[0].payload.attack_total == 4  # the locked total, not re-derived


@pytest.mark.asyncio
async def test_slug_resume_emits_no_defend_request_when_nothing_pending(tmp_path: Path):
    """A routine reconnect into a Fate session with NO parked barrier must not
    invent a defend prompt (No Silent Fallbacks / no spurious frames)."""
    slug = "2026-06-21-noir-no-pending"
    snap, enc = parked_conflict(defender="Vance", attacker="Mook", request_id="d1")
    enc.pending_defenses.clear()  # nothing parked
    snap.genre_slug = _GENRE
    snap.world_slug = _WORLD
    snap.player_seats["vance-player"] = "Vance"
    _seed(slug, snap)

    handler = _make_handler(tmp_path)
    outbound = await handler.handle_message(_connect_msg(slug, "vance-player", "Vance"))

    assert not [m for m in outbound if getattr(m, "type", None) == MessageType.FATE_DEFEND_REQUEST]


@pytest.mark.asyncio
async def test_slug_resume_sweeps_orphaned_pending_defense_but_spares_present_one(tmp_path: Path):
    """On reconnect, an orphaned pending entry (defender no longer a seated PC) is
    conceded so the ledger can resume, while a present defender's open entry is left
    untouched — proving ``clear_orphaned_pending_defenses`` is wired into the
    production resume path with its safety invariant intact."""
    slug = "2026-06-21-noir-orphan-sweep"
    snap, enc = parked_conflict(defender="Vance", attacker="Mook", request_id="d-vance")
    # A second, ORPHANED entry: "Ghost" never appears in snapshot.characters/actors.
    enc.pending_defenses.append(
        FatePendingDefense(
            request_id="d-ghost",
            attacker="Mook",
            defender="Ghost",
            attack_skill="Fight",
            attack_total=4,
        )
    )
    snap.genre_slug = _GENRE
    snap.world_slug = _WORLD
    snap.player_seats["vance-player"] = "Vance"
    _seed(slug, snap)

    handler = _make_handler(tmp_path)
    await handler.handle_message(_connect_msg(slug, "vance-player", "Vance"))

    assert handler.session_data is not None
    resumed = handler.session_data.snapshot.encounter
    by_id = {p.request_id: p for p in resumed.pending_defenses}
    # Orphan no longer blocks the barrier (swept ⇒ conceded, or already resolved).
    assert "d-ghost" not in by_id or by_id["d-ghost"].conceded is True, (
        "the orphaned 'Ghost' entry must be conceded (or resolved) on resume so the "
        f"round can unwedge; pending={[(p.request_id, p.conceded) for p in resumed.pending_defenses]}"
    )
    # The PRESENT defender's open entry is sacrosanct — never swept.
    assert "d-vance" in by_id, "Vance's open entry must survive the sweep"
    assert by_id["d-vance"].conceded is False
    assert by_id["d-vance"].defense_total is None
