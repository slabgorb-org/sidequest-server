"""RED-phase tests for Story 158-78: the default server quality gate is red
under xdist because the Postgres test-isolation fixture does not cover every
test that uses the Postgres helpers.

Root cause (confirmed 2026-08-01, see the session file's TEA Assessment):

``tests/agents/tools/conftest.py`` defines the ``pg_store_with`` /
``pg_empty_store`` helpers **and** an autouse ``_pg_isolation`` fixture that
makes them safe — it rebinds ``SIDEQUEST_DATABASE_URL`` to a per-xdist-worker
throwaway database, TRUNCATEs it, and resets the process-global pool. That
fixture is *directory-scoped*. Two test modules outside that directory import
the helpers directly:

    tests/agents/test_102_5_wn_tool_narrator_wiring.py
    tests/agents/test_use_mutation_tool.py

They therefore get no isolation at all. ``db_pool.get_pool()`` resolves
``SIDEQUEST_DATABASE_URL`` from the ambient environment — the developer's REAL
``sidequest`` database — and both modules persist under the same hardcoded
session slug ``tool-test``. Under ``-n auto`` the xdist workers are separate
processes sharing that one row, so whichever worker calls
``init_session()``/``save()`` last wins and the loser fails, with the victim
rotating between runs:

    AssertionError: the engine's hit adjudication never reached the narrator:
        "NOT_FOUND: unknown actor: 'Vesska'"
    AssertionError: assert reloaded is not None

Under ``-n0`` there is a single process running sequentially, so the writes
cannot interleave and the suite is green — which is exactly the reported
"serial is clean, parallel is red" signature.

The correct pattern already exists in this repo: ``tests/integration/
test_mutation_wiring.py::_pg_store_with`` binds an explicit ``migrated_db`` and
uses a **unique** slug (``wiring-{uuid4}``). This story wires that same safety
into the shared helpers so it applies wherever they are used.

These tests fail today and turn green once the isolation covers every consumer
of the helpers and the helpers stop sharing one session slug.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # sidequest-server/

# The per-worker throwaway databases created by the ``migrated_db`` fixture in
# tests/conftest.py are named ``sq_test_{worker_id}_{uuid4[:8]}``. Any database
# a test binds MUST match this prefix — anything else is a real database.
_THROWAWAY_DB_PREFIX = "sq_test_"

# The two modules that import the Postgres helpers from outside
# tests/agents/tools/, and therefore run without its autouse ``_pg_isolation``.
# These are the two failures the default gate reports.
_UNISOLATED_HELPER_MODULES = (
    "tests/agents/test_102_5_wn_tool_narrator_wiring.py",
    "tests/agents/test_use_mutation_tool.py",
)


def _dbname(url: str) -> str:
    """Return the database name from a postgres conninfo URL."""
    return urlsplit(url).path.lstrip("/")


# --- AC-1: no test may ever bind the developer's real database --------------


def test_no_test_runs_with_the_developer_database_bound() -> None:
    """``SIDEQUEST_DATABASE_URL`` must never resolve to a real database while a
    test is running.

    This is the Postgres analogue of
    ``test_tmp_save_dir_fixture_isolated_from_real_home`` — that test protects
    ``~/.sidequest`` from the suite; nothing protects Postgres today.

    Two outcomes are acceptable, and Dev may choose either:

      (a) the variable is UNSET, so any test that reaches for Postgres without
          explicitly requesting an isolated database fails loud with
          ``MissingDatabaseUrlError`` (No Silent Fallbacks); or
      (b) it is bound to a ``sq_test_*`` per-worker throwaway database.

    What is NOT acceptable is the status quo: the ambient developer URL leaking
    into the suite, which is how 1,938 junk ``test-*``/``tool-test`` sessions
    were written into the real ``sidequest`` database.
    """
    url = os.environ.get("SIDEQUEST_DATABASE_URL")
    assert url is None or _dbname(url).startswith(_THROWAWAY_DB_PREFIX), (
        f"a test is running with SIDEQUEST_DATABASE_URL bound to "
        f"{_dbname(url)!r}, which is not a {_THROWAWAY_DB_PREFIX}* throwaway "
        f"database. Any test that opens the pool will read and write the "
        f"developer's real saves. Either leave the variable unset (so "
        f"unisolated Postgres access fails loud) or bind a per-worker "
        f"throwaway database."
    )


# --- AC-2: the shared helpers must not reuse one session slug ---------------


def test_pg_store_with_does_not_reuse_a_single_session_slug(
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``pg_store_with()`` calls must not silently clobber each other.

    The helper defaults to ``slug="tool-test"`` for every caller, so two
    snapshots persisted through it land on the SAME ``sessions`` row — the
    second silently overwrites the first. That is the collision that makes the
    two unisolated modules fight under xdist (one persists
    ``heavy_metal/long_foundry``, the other ``mutant_wasteland/dead_lands``,
    both as ``tool-test``).

    ``tests/integration/test_mutation_wiring.py`` already models the fix: a
    unique ``wiring-{uuid4}`` slug per store.
    """
    import psycopg

    from sidequest.game import db_pool
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from tests.agents.tools.conftest import pg_store_with

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

    def _snapshot(genre: str, world: str, who: str) -> GameSnapshot:
        core = CreatureCore(
            name=who,
            description="d",
            personality="p",
            inventory=Inventory(items=[]),
            hp=HpPool(current=10, max=10, base_max=10),
            armor_class=10,
        )
        pc = Character(core=core, backstory="b", char_class="Warrior", race="Human", stats={})
        return GameSnapshot(
            genre_slug=genre,
            world_slug=world,
            turn_manager=TurnManager(interaction=1),
            characters=[pc],
            npcs=[],
        )

    # Mirrors the two colliding modules: different worlds, different casts.
    first = pg_store_with(_snapshot("heavy_metal", "long_foundry", "Vesska"))
    pg_store_with(_snapshot("mutant_wasteland", "dead_lands", "Grist"))

    reloaded = first.load()
    assert reloaded is not None, (
        "the first store's session vanished after a second pg_store_with() call "
        "— both defaulted to the same session slug, so the second init_session() "
        "destroyed the first."
    )
    assert reloaded.snapshot.find_creature_core("Vesska") is not None, (
        f"the first store reloaded a DIFFERENT session's snapshot "
        f"({reloaded.snapshot.genre_slug}/{reloaded.snapshot.world_slug}) — "
        f"pg_store_with() shares one hardcoded slug across callers, so the "
        f"second call silently overwrote the first. Give each store a unique "
        f"slug, as tests/integration/test_mutation_wiring.py already does."
    )

    db_pool.close_pool()


# --- AC-3: wiring — the offending modules survive a real parallel run -------


@pytest.mark.timeout(120)
def test_unisolated_helper_modules_pass_under_xdist(migrated_db: str) -> None:
    """The two modules that use the PG helpers outside ``tests/agents/tools/``
    must pass when xdist schedules them onto SEPARATE worker processes.

    This is the wiring test: AC-1 and AC-2 assert the invariants, this one
    reproduces the actual reported gate failure end to end. ``--dist loadfile``
    guarantees the two files land on different workers, which makes the race
    deterministic instead of a coin flip.

    The subprocess is pointed at a throwaway database so a RED run cannot add
    more pollution to the developer's real one.
    """
    admin = os.environ.get("SIDEQUEST_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("SIDEQUEST_TEST_DATABASE_URL unset — start Postgres with `just pg-up`")

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    env = {
        **os.environ,
        "SIDEQUEST_DATABASE_URL": plain,
        "SIDEQUEST_TEST_DATABASE_URL": admin,
    }

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *_UNISOLATED_HELPER_MODULES,
            "-n",
            "2",
            "--dist",
            "loadfile",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
    )

    assert proc.returncode == 0, (
        "the PG-helper modules outside tests/agents/tools/ fail when xdist puts "
        "them on separate workers — they share one database and one session "
        "slug, so each worker's init_session()/save() clobbers the other's.\n"
        f"--- stdout ---\n{proc.stdout[-3000:]}\n"
        f"--- stderr ---\n{proc.stderr[-2000:]}"
    )
