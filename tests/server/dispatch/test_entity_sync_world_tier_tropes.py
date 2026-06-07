"""World-tier trope definitions reach entity_sync (playtest 2026-06-07).

Root cause of the ``entity_sync.project_failed trope=… error=no_definition``
per-turn WARN spam across three genres: ``_collect_trope_definitions`` read
``genre_pack.tropes`` (genre tier) only, while the seeder's chapter tropes are
WORLD ids (``resolve_trope_inheritance`` emits only world-tier tropes into
``world.tropes``) — the ADR-140 genre/world boundary.

Pins:

1. the collector merges genre + bound-world tiers (world wins by id) — the
   same resolution the seeder used;
2. no bound world / no pack degrades exactly as before;
3. ``audit_seeded_trope_definitions`` fails LOUD once at seed time (ERROR log
   + watcher event) for a seeded trope id with no definition in either tier,
   and is silent when clean (No Silent Fallbacks);
4. wiring — the REAL chargen-commit handler invokes the audit on the
   materialized snapshot (seam-level capture through the module dict, the
   same late-bound-import mechanism the 91-4 wiring test uses).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import sidequest.server.dispatch.entity_sync as entity_sync_mod
from sidequest.game.session import TropeState
from sidequest.genre.models.tropes import TropeDefinition
from sidequest.protocol.messages import CharacterCreationPayload
from sidequest.server.dispatch.entity_sync import (
    _collect_trope_definitions,
    audit_seeded_trope_definitions,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import mock_claude_client_factory as _mock_claude_client_factory
from tests.server.test_chargen_dispatch import (
    _connect,
    _send_chargen,
    _walk_to_confirmation,
    run,
)

CONTENT_ROOT = Path(__file__).resolve().parents[4] / "sidequest-content" / "genre_packs"


def _trope(trope_id: str, name: str, description: str = "d") -> TropeDefinition:
    return TropeDefinition(id=trope_id, name=name, description=description)


def _sd(
    *,
    genre_tropes: list[TropeDefinition] = (),
    world_tropes: list[TropeDefinition] | None = None,
    world_slug: str = "testworld",
) -> Any:
    """Minimal _SessionData shape the collector reads (defensive getattrs)."""
    worlds: dict[str, Any] = {}
    if world_tropes is not None:
        worlds[world_slug] = SimpleNamespace(tropes=list(world_tropes))
    genre_pack = SimpleNamespace(tropes=list(genre_tropes), worlds=worlds)
    return SimpleNamespace(
        genre_pack=genre_pack,
        genre_slug="testgenre",
        world_slug=world_slug if world_tropes is not None else "",
    )


# ---------------------------------------------------------------------------
# 1-2. Collector merges both tiers
# ---------------------------------------------------------------------------


def test_collector_includes_world_tier_tropes() -> None:
    sd = _sd(
        genre_tropes=[_trope("genre_arc", "Genre Arc")],
        world_tropes=[_trope("the_book_tightens", "The Book Tightens")],
    )
    ids = {d.id for d in _collect_trope_definitions(sd)}
    assert ids == {"genre_arc", "the_book_tightens"}


def test_collector_world_tier_wins_on_id_collision() -> None:
    sd = _sd(
        genre_tropes=[_trope("shared_arc", "Genre Name")],
        world_tropes=[_trope("shared_arc", "World Name")],
    )
    defs = {d.id: d for d in _collect_trope_definitions(sd)}
    assert defs["shared_arc"].name == "World Name"


def test_collector_no_world_bound_is_genre_only() -> None:
    sd = _sd(genre_tropes=[_trope("genre_arc", "Genre Arc")], world_tropes=None)
    ids = {d.id for d in _collect_trope_definitions(sd)}
    assert ids == {"genre_arc"}


def test_collector_no_pack_is_empty() -> None:
    assert _collect_trope_definitions(SimpleNamespace(genre_pack=None)) == []


# ---------------------------------------------------------------------------
# 3. Seed-time audit — loud once, silent when clean
# ---------------------------------------------------------------------------


def _snapshot_with_tropes(*ids: str) -> Any:
    return SimpleNamespace(
        active_tropes=[TropeState(id=i, status="dormant", progress=0.0, beats_fired=0) for i in ids]
    )


def test_audit_reports_missing_definition_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        entity_sync_mod,
        "_watcher_publish",
        lambda kind, payload, **kw: events.append((kind, payload)),
    )
    sd = _sd(world_tropes=[_trope("known_arc", "Known Arc")])
    snap = _snapshot_with_tropes("known_arc", "phantom_arc")

    with caplog.at_level("ERROR", logger="sidequest.server.dispatch.entity_sync"):
        missing = audit_seeded_trope_definitions(sd, snap)

    assert missing == ["phantom_arc"]
    assert any("trope.seeded_without_definition" in r.getMessage() for r in caplog.records)
    assert events and events[0][1]["op"] == "missing_definition"
    assert events[0][1]["missing_ids"] == ["phantom_arc"]


def test_audit_silent_when_all_definitions_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    monkeypatch.setattr(
        entity_sync_mod,
        "_watcher_publish",
        lambda *a, **kw: events.append(a),
    )
    sd = _sd(world_tropes=[_trope("known_arc", "Known Arc")])

    assert audit_seeded_trope_definitions(sd, _snapshot_with_tropes("known_arc")) == []
    assert events == []


# ---------------------------------------------------------------------------
# 4. Wiring — the REAL chargen-commit handler runs the audit
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Per-test throwaway PG db so the commit is a true first-commit
    (materialize path) — mirrors test_chargen_quest_seed_wiring.py."""
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


def test_chargen_commit_runs_seeded_trope_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real chargen-commit handler; capture the audit at the module
    seam (the mixin imports it function-level, late-bound through the module
    dict). The audit must run against the materialized snapshot."""
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("content pack not found")
    handler = WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )

    captured: list[tuple[Any, Any]] = []
    real_audit = entity_sync_mod.audit_seeded_trope_definitions

    def _capturing_audit(sd: Any, snapshot: Any) -> list[str]:
        captured.append((sd, snapshot))
        return real_audit(sd, snapshot)

    monkeypatch.setattr(entity_sync_mod, "audit_seeded_trope_definitions", _capturing_audit)

    async def body() -> None:
        await _connect(handler)
        await _walk_to_confirmation(handler, freeform_name="Rux")
        out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
        assert out

    run(body())

    assert len(captured) == 1, (
        "the chargen first-commit path must run audit_seeded_trope_definitions "
        "exactly once on the materialized snapshot"
    )
    _sd_seen, snap_seen = captured[0]
    assert hasattr(snap_seen, "active_tropes")
