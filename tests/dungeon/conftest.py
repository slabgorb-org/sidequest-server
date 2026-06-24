"""Shared test infra for the dungeon test package.

OTEL global-provider hermeticity (Plan 6, Tasks 4 + 6)
------------------------------------------------------
`test_commit_and_ledger_emit_spans` installs its own `_Capture`
`TracerProvider` via `trace.set_tracer_provider()` so it can assert that
`dungeon.persist.commit` / `ledger.add` / `ledger.resolve` spans really
fire. OTEL 1.x gates `set_tracer_provider()` behind a once-only guard
(`_TRACER_PROVIDER_SET_ONCE`): in a full-suite session an earlier
conftest's `init_tracer()` already won that guard, so the test's call
was a SILENT no-op — the `_Capture` exporter never installed, the test
never actually asserted anything (a dead green).

Plan 6 Task 4 added `reset_otel_provider()` so the call becomes
effective and the test really runs. But making it effective without a
matching restore meant the test then LEAKED its `_Capture` provider into
the global slot for the rest of the pytest process — contaminating
later `otel_capture`-using tests (`test_visibility_classifier`,
`test_chargen_persist_and_play`). Task 4's reset-before was only half
the fix; the restore-after is the other half. Plan 6 owns completing it
(it is Plan 6's helper that activated the previously-dormant no-op).

The fix is hermetic save-and-restore:

* `capture_otel_provider_state()` snapshots the prior global provider
  reference AND the `Once` guard's `_done` flag.
* `reset_otel_provider()` clears both so `set_tracer_provider()` is not
  a no-op (unchanged behaviour — kept for the call site).
* `restore_otel_provider_state(state)` puts the captured prior provider
  and guard flag back, so after the test the global tracer provider is
  EXACTLY what it was before — no downstream contamination.

`test_commit_and_ledger_emit_spans` wraps its provider install +
assertions in `try/finally`: capture → reset → set → assert → (finally)
restore. It both (a) starts from a clean provider and (b) leaves the
global provider exactly as found. Its three span assertions are intact:
it still really runs and really asserts.

Private-API access (`trace._TRACER_PROVIDER`,
`trace._TRACER_PROVIDER_SET_ONCE`) — update if the OTEL SDK moves the
guard. This mirrors the established private-API helper at
`tests/telemetry/test_spans.py:_reset_otel_provider`; it is duplicated
here (a sibling package-local helper) rather than cross-imported — the
underscore-prefixed test-module helper is intentionally module-private
and must not be reached across test modules.

ADR-115 D6 Postgres fixtures
------------------------------
``migrated_db`` provides an ephemeral Postgres database for tests that
need a real ``PgDungeonRepository``.  Mirrors the pattern in
``tests/persistence/conftest.py``; kept local to avoid cross-package
fixture visibility concerns.  ``SIDEQUEST_TEST_DATABASE_URL`` must be set
(``just pg-up``) or the PG tests skip loudly.

``pg_dungeon_repo`` builds a ``(pool, PgDungeonRepository)`` pair for a
fresh uuid-namespaced session; each test gets an independent slug so
xdist workers do not collide.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from opentelemetry import trace

from alembic import command

_ADMIN_ENV = "SIDEQUEST_TEST_DATABASE_URL"


def _admin_conninfo() -> str:
    url = os.environ.get(_ADMIN_ENV)
    if not url:
        pytest.skip(
            f"{_ADMIN_ENV} unset — start local Postgres with `just pg-up` and export "
            f"{_ADMIN_ENV}=postgresql://$USER@localhost:5432/sidequest_test, or run in CI."
        )
    return url


def _swap_dbname(conninfo: str, dbname: str) -> str:
    head, _, _tail = conninfo.partition("?")
    base, _slash, _olddb = head.rpartition("/")
    rebuilt = f"{base}/{dbname}"
    if _tail:
        rebuilt = f"{rebuilt}?{_tail}"
    return rebuilt


@pytest.fixture(scope="session")
def migrated_db(worker_id: str) -> Iterator[str]:
    """A freshly-migrated throwaway Postgres database; conninfo URL yielded.

    ``worker_id`` is injected by pytest-xdist ("gw0", "gw1", ... or "master"
    when serial); it namespaces the db so parallel workers do not collide.
    """
    admin = _admin_conninfo()
    db_name = f"sq_dtest_{worker_id}_{uuid.uuid4().hex[:8]}"

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    target = _swap_dbname(admin, db_name)
    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option(
            "sqlalchemy.url",
            target
            if target.startswith("postgresql+psycopg://")
            else target.replace("postgresql://", "postgresql+psycopg://", 1),
        )
        command.upgrade(cfg, "head")
        yield target
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def capture_otel_provider_state() -> dict[str, Any]:
    """Snapshot the global OTEL provider + once-guard so it can be restored.

    Returns a state dict consumed only by ``restore_otel_provider_state``.
    Captures both the stored provider reference and the ``Once._done``
    flag (the two pieces ``reset_otel_provider`` clobbers), so a test that
    installs its own provider can put the prior global state back exactly
    as it found it and never leak into the rest of the pytest session.
    """
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    return {
        "provider": getattr(trace, "_TRACER_PROVIDER", None),
        "once": once,
        "once_done": getattr(once, "_done", None) if once is not None else None,
    }


def reset_otel_provider() -> None:
    """Reset the OTEL global provider so set_tracer_provider() is not a no-op.

    OTEL 1.x gates set_tracer_provider behind a Once guard
    (_TRACER_PROVIDER_SET_ONCE). In a test session the first call wins and
    all subsequent calls are silently dropped. We reset both the stored
    provider reference and the Once._done flag before a test that calls
    set_tracer_provider directly. Always pair this with
    ``capture_otel_provider_state`` (before) and
    ``restore_otel_provider_state`` (after, in a ``finally``) so the
    test's own provider does NOT leak into the rest of the suite.
    Private-API access — update if the OTEL SDK moves the guard.
    """
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None:
        once._done = False


def restore_otel_provider_state(state: dict[str, Any]) -> None:
    """Restore the global OTEL provider + once-guard captured earlier.

    The inverse of ``reset_otel_provider`` + ``set_tracer_provider``:
    puts back the prior provider reference and the prior ``Once._done``
    flag so, after the test that installed its own provider, the global
    tracer provider is byte-for-byte what it was before. This is the
    "restore-after" half of the hermetic fix — without it, the test's
    own ``_Capture`` provider leaks and contaminates every later
    ``otel_capture``-using test in the same pytest process.
    """
    trace._TRACER_PROVIDER = state["provider"]  # type: ignore[attr-defined]
    once = state["once"]
    if once is not None and state["once_done"] is not None:
        once._done = state["once_done"]


def build_pg_dungeon_repo(monkeypatch: Any, migrated_db: str) -> tuple[Any, Any, int]:
    """Build a ``(pool, PgDungeonRepository, session_id)`` triple for one
    isolated test session.

    ``migrated_db`` is the psycopg-native conninfo URL from the
    session-scoped ``migrated_db`` fixture (may carry the
    ``postgresql+psycopg://`` prefix).  ``monkeypatch`` is the per-test
    ``pytest.MonkeyPatch`` so the env-var override is cleaned up
    automatically at the end of each test.

    The slug is uuid-namespaced — REQUIRED because ``migrated_db`` is
    session-scoped and the pool COMMITS (no rollback between tests), so
    fixed slugs bleed across xdist workers.
    """
    from sidequest.game import db_pool
    from sidequest.game.pg import sessions
    from sidequest.game.pg.dungeon import PgDungeonRepository

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"dungeon_d6_{uuid.uuid4().hex[:12]}"
    sid = sessions.ensure_session(
        pool,
        slug=slug,
        mode="solo",
        genre_slug="caverns",
        world_slug="beneath_sunden",
    )
    repo = PgDungeonRepository(pool, session_id=sid)
    return pool, repo, sid


# ---------------------------------------------------------------------------
# Task-4 (158-18): tactical_fill_fixture — real-shaped RegionFill objects for
# wiring tests of _stage_tactical / _tactical_into_mask_dicts.
# ---------------------------------------------------------------------------

import hashlib as _hashlib
from dataclasses import dataclass as _dataclass

from sidequest.dungeon.materializer import BlockInfo, RegionFill, RegionMask

_GRID = [[1, 1, 1], [1, 0, 1], [1, 1, 1]]  # 3x3, one floor cell at (1,1)


def _make_region_fill(region_id: str) -> RegionFill:
    """Build a real RegionFill with a non-None RegionMask for test fixtures."""
    mask_bytes = b"###\n#.#\n###"
    mask_sha = _hashlib.sha256(mask_bytes).hexdigest()
    block = BlockInfo(cell_width=28, grid_width=3, grid_height=3)
    mask = RegionMask(grid=_GRID, mask_bytes=mask_bytes, mask_sha=mask_sha, block=block)
    return RegionFill(
        region_id=region_id,
        algorithm="cellular",
        width=3,
        height=3,
        braid_ratio=0.0,
        grid=_GRID,
        mask=mask,
    )


@_dataclass
class _TacticalNode:
    id: str
    theme: str


@_dataclass
class _TacticalExpansion:
    new_nodes: list


class _TacticalGraph:
    def neighbors(self, region_id: str) -> list:  # mirrors RegionGraph.neighbors
        return []


@_dataclass
class _TacticalAttachReport:
    region_id: str
    setpiece_id: str


@_dataclass
class _TacticalAttachResult:
    attach_reports: list


class _TacticalCuration:
    # mirrors RegionCuration.region_creatures: dict[str, list[CuratedCreature]]
    def __init__(self) -> None:
        self.region_creatures: dict = {
            "exp001.r0": [object()],
            "exp001.r1": [],
        }


@pytest.fixture
def tactical_fill_fixture():
    """Two real RegionFill objects with masks + one set-piece on exp001.r0."""
    fill = {
        "exp001.r0": _make_region_fill("exp001.r0"),
        "exp001.r1": _make_region_fill("exp001.r1"),
    }
    return {
        "expansion": _TacticalExpansion(
            new_nodes=[
                _TacticalNode("exp001.r0", "bone_crypt"),
                _TacticalNode("exp001.r1", "bone_crypt"),
            ]
        ),
        "graph": _TacticalGraph(),
        "fill_result": fill,
        "curation": _TacticalCuration(),
        "attach_result": _TacticalAttachResult(
            attach_reports=[_TacticalAttachReport("exp001.r0", "collapse_gallery")]
        ),
    }
