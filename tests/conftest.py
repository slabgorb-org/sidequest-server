"""Shared pytest fixtures for sidequest-server tests."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Ephemeral real-Postgres fixtures (ADR-115). Defined at the tests/ root so
# the few non-persistence suites that drive real PG (e.g.
# server/test_save_write_lock.py — D5's structural deadlock-impossibility
# regression) can reuse them. Persistence-suite tests inherit these.
#
# Strategy: a session-scoped fixture creates a uniquely-named database (per
# pytest-xdist worker), runs `alembic upgrade head` into it, yields its
# conninfo URL, and DROPs it on teardown. If SIDEQUEST_TEST_DATABASE_URL is
# unset, the suite SKIPS with a loud reason.
# ---------------------------------------------------------------------------

_PG_ADMIN_ENV = "SIDEQUEST_TEST_DATABASE_URL"


def _pg_admin_conninfo() -> str:
    url = os.environ.get(_PG_ADMIN_ENV)
    if not url:
        pytest.skip(
            f"{_PG_ADMIN_ENV} unset — start local Postgres with `just pg-up` and export "
            f"{_PG_ADMIN_ENV}=postgresql://$USER@localhost:5432/sidequest_test, or run in CI."
        )
    return url


def _pg_swap_dbname(conninfo: str, dbname: str) -> str:
    """Return ``conninfo`` with its path (database name) replaced by ``dbname``."""
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
    import psycopg
    from alembic.config import Config

    from alembic import command

    admin = _pg_admin_conninfo()
    db_name = f"sq_test_{worker_id}_{uuid.uuid4().hex[:8]}"

    # CREATE/DROP DATABASE cannot run inside a transaction block.
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    target = _pg_swap_dbname(admin, db_name)
    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        # Alembic uses the +psycopg SQLAlchemy form of the target URL.
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


@pytest.fixture
def pg_conn(migrated_db: str) -> Iterator[Any]:
    """A connection to the migrated db, wrapped in a transaction that always
    rolls back — per-test isolation without re-running migrations."""
    import psycopg

    with psycopg.connect(migrated_db) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def behind_head_db() -> Iterator[str]:
    """A real Postgres database upgraded to base revision 0001 — deliberately
    BEHIND alembic head (Story 71-20 / finding #G4).

    Mirrors ``migrated_db`` but stops the upgrade at the base revision instead of
    ``head``, so the schema is reachable but stale — the exact silent-landmine
    condition this story guards against. Yields the plain ``postgresql://``
    conninfo (what ``db_config`` / ``db_pool`` consume) and DROPs the DB on
    teardown. Function-scoped so each behind-head test gets a fresh stale DB.
    """
    import psycopg
    from alembic.config import Config

    from alembic import command

    admin = _pg_admin_conninfo()
    db_name = f"sq_behind_{uuid.uuid4().hex[:8]}"

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    target = _pg_swap_dbname(admin, db_name)
    plain = target.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option(
            "sqlalchemy.url",
            target
            if target.startswith("postgresql+psycopg://")
            else target.replace("postgresql://", "postgresql+psycopg://", 1),
        )
        # Stop at the base revision — NOT head. This leaves the DB behind head
        # (0002 asset_ledger and any later migrations unapplied).
        command.upgrade(cfg, "0001")
        yield plain
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Refresh recorded SVG snapshots in tests/orbital/snapshots/.",
    )


@pytest.fixture(scope="session")
def content_dir() -> Path:
    """Path to the sidequest-content repo (genre packs, worlds)."""
    return Path(__file__).resolve().parent.parent.parent / "sidequest-content"


@pytest.fixture
def tmp_save_dir(tmp_path: Path) -> Path:
    """Temporary save directory per test."""
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    return save_dir


@pytest.fixture
async def initialized_tracer() -> AsyncIterator[None]:
    """Initialize OTEL tracer for the duration of a test."""
    from sidequest.telemetry import init_tracer

    init_tracer(service_name="sidequest-server-test")
    yield


# --- caverns_sunden deprecation: skip world-coupled tests -----------------
# caverns_sunden was deprecated in favor of beneath_sunden and relocated to
# genre_workshopping/ (sidequest-content PR #228). Every test below binds to
# that now-removed world (in-memory snapshots, fixtures, or on-disk world
# loads). They are SKIPPED — deliberately and visibly — pending a re-point
# to beneath_sunden or a dedicated test-fixture world. This single block is
# the reversible, documented record of that debt; nothing is buried.
# NOTE (2026-05-17, [BS-BUG-LOW]): agents/test_pov_swap.py was REMOVED
# from this set. It is a pure unit suite for swap_to_second_person — a
# world-agnostic string transform with generic names and no genre/world
# fixtures, snapshots, or on-disk world loads. Its only caverns_sunden
# tie is a docstring noting where the bug was originally found; PR #312's
# name-grep swept it in over-broadly. pov_swap is live in the
# beneath_sunden playtest right now, so its regression coverage must run
# (CLAUDE.md: no skipping tests for live subsystems). Re-included
# deliberately and visibly, in the spirit of PR #312's own reversible-
# with-reason record.
_CAVERNS_SUNDEN_DEPRECATED_TESTS = frozenset(
    {
        "audio/test_library_backend_r2_only.py",
        "cli/test_encountergen.py",
        "game/test_disposition_call_site_migration.py",
        "game/test_room_file_loader.py",
        "genre/test_beneath_sunden_world_load.py",
        "genre/test_models/test_pack_integration.py",
        "genre/test_visual_style_lora_removal_wiring.py",
        "genre/test_world_items_loader.py",
        "integration/test_cavern_static_mount.py",
        "integration/test_room_enter_cavern.py",
        "magic/test_47_9_innate_proactive.py",
        "magic/test_e2e_cnc_memorization.py",
        "magic/test_state.py",
        "protocol/test_models.py",
        # 72-15: server/dispatch/test_pregen.py was re-pointed off the deprecated
        # caverns_sunden world. Its ~16 seed_manual unit tests are world-agnostic
        # (they monkeypatch load_genre_pack), and its one e2e now binds the
        # dedicated test fixture pack (test_genre/flickering_reach), not a live
        # world. pregen is a LIVE subsystem, so its coverage must run (CLAUDE.md:
        # no skipping tests for live subsystems). Removed from this skip set
        # deliberately and visibly, per this block's reversible-with-reason contract.
        "server/test_adr105_b1_secret_invariant_wiring.py",
        "server/test_chargen_arrange_dispatch.py",
        "server/test_chargen_dispatch.py",
        "server/test_chargen_persist_and_play.py",
        "server/test_chargen_story_dispatch.py",
        # 59-16: test_confrontation_mp_broadcast.py and
        # test_confrontation_per_pc_projection.py were re-pointed to the live
        # caverns_and_claudes pack (the mp file rewritten to the single
        # filtered-delivery contract; the projection file is a world-agnostic
        # unit test of build_confrontation_payload). They no longer bind to the
        # deprecated caverns_sunden world, so they are removed from this skip
        # set — re-included deliberately and visibly per this block's contract.
        "server/test_dice_throw_session_wiring.py",
        "server/test_magic_init_caverns_and_claudes.py",
        "server/test_magic_init_mp_second_commit.py",
        "server/test_magic_init.py",
        "server/test_merged_mp_emitter_projection.py",
        "server/test_narration_pov_emission.py",
        "server/test_opening_turn_bootstrap.py",
        "server/test_persistence_otel_wiring.py",
        "server/test_region_init.py",
        "server/test_resource_deltas.py",
        "server/test_rest_hub_endpoint.py",
        "server/test_room_graph_init.py",
        "server/test_yield_dispatch.py",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    tests_root = Path(__file__).parent
    skip = pytest.mark.skip(
        reason="caverns_sunden deprecated → genre_workshopping "
        "(sidequest-content PR #228); test world binding pending migration"
    )
    for item in items:
        try:
            rel = item.path.relative_to(tests_root).as_posix()
        except ValueError:
            continue
        if rel in _CAVERNS_SUNDEN_DEPRECATED_TESTS:
            item.add_marker(skip)


# --- Shared synthetic-pack fixture (visible to all test packages) --------
# Relocated from tests/genre/conftest.py (Story 50-13): the disposition
# threshold loader/OTEL wiring suite lives under tests/game/ and needs the
# same clone-a-fixture-pack helper the genre suite uses. conftest fixtures
# are directory-scoped, so a single definition at the tests/ root is the
# correct home — one definition, visible to genre/, game/, and beyond.

# Canonical minimal pack fixture used as the base for clone-based tests.
_FIXTURE_PACK = Path(__file__).resolve().parent / "fixtures" / "packs" / "test_genre"


class MinimalPack:
    """A temporary copy of the test_genre fixture pack with mutable YAML overrides.

    Usage::

        pack = minimal_pack_factory(tmp_path)
        pack.set_rules_yaml(confrontations=[...], allowed_classes=["Fighter"])
        pack.set_classes_yaml([{"id": "fighter", ...}])
        load_genre_pack(pack.path)
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def set_rules_yaml(
        self,
        *,
        confrontations: list[dict[str, Any]],
        allowed_classes: list[str],
    ) -> None:
        """Write a minimal rules.yaml with the given confrontations and allowed_classes.

        All other required RulesConfig fields are set to safe defaults.
        """
        data: dict[str, Any] = {
            "tone": "test",
            "lethality": "low",
            "magic_level": "none",
            "stat_generation": "point_buy",
            "point_buy_budget": 27,
            "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            "allowed_classes": allowed_classes,
            "allowed_races": [],
            "confrontations": confrontations,
        }
        rules_path = self.path / "rules.yaml"
        with rules_path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def set_classes_yaml(self, classes: list[dict[str, Any]]) -> None:
        """Write classes.yaml with the given class definition dicts."""
        classes_path = self.path / "classes.yaml"
        with classes_path.open("w", encoding="utf-8") as f:
            yaml.dump(classes, f, default_flow_style=False, sort_keys=False)

    def create_spells_dir(self) -> None:
        """Create a minimal spells/ directory at the pack root.

        This simulates a pack that ships a spell catalog, which triggers
        the saving_throws validator. The YAML content is a minimal valid
        SpellCatalog entry (all required fields present).
        """
        spells_dir = self.path / "spells"
        spells_dir.mkdir(exist_ok=True)
        stub_catalog = {
            "version": "1.0",
            "genre": "test_pack",
            "tradition": "arcane",
            "level": 1,
            "spells": [
                {
                    "id": "magic_missile",
                    "name": "Magic Missile",
                    "level": 1,
                    "tradition": "arcane",
                    "range": "near",
                    "target": "single",
                    "duration": "instant",
                    "save": {"stat": None, "effect": "none"},
                    "effect_template": "Auto-hit bolt of force.",
                    "components": {"verbal": True, "somatic": True},
                    "backlash": None,
                    "narrator_register": "A bolt of force streaks unerringly.",
                    "domain": "force",
                }
            ],
        }
        catalog_path = spells_dir / "arcane_l1.yaml"
        with catalog_path.open("w", encoding="utf-8") as f:
            yaml.dump(stub_catalog, f, default_flow_style=False, sort_keys=False)


@pytest.fixture
def minimal_pack_factory():
    """Factory fixture: call with (tmp_path) to get a MinimalPack.

    The returned pack is a full clone of tests/fixtures/packs/test_genre with
    its lethality_policy.yaml genre_key updated to match the tmp directory name.
    Call set_rules_yaml() / set_classes_yaml() to inject test-specific YAML.
    """

    def _factory(tmp_path: Path) -> MinimalPack:
        dest = tmp_path / "test_pack"
        shutil.copytree(_FIXTURE_PACK, dest)
        # Update lethality_policy.yaml genre_key to match the new directory name.
        lethality_yaml = dest / "lethality_policy.yaml"
        if lethality_yaml.exists():
            with lethality_yaml.open("r", encoding="utf-8") as f:
                policy_data = yaml.safe_load(f) or {}
            policy_data["genre_key"] = dest.name
            with lethality_yaml.open("w", encoding="utf-8") as f:
                yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)
        return MinimalPack(dest)

    return _factory


# ---------------------------------------------------------------------------
# session_handler_factory (moved from tests/server/conftest.py to be visible
# to tests/e2e/ per story 73-6). Centralizes session_data + handler wiring
# for e2e and server tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def session_handler_factory(tmp_path):
    """Return a factory callable with two calling conventions.

    **Single-player (legacy):** ``factory(genre="caverns_and_claudes")``
    Returns ``(sd, handler)`` — a minimal ``_SessionData`` +
    ``WebSocketSessionHandler`` suitable for unit-testing
    ``_execute_narration_turn`` without a real WebSocket or LLM call. The
    test is responsible for overriding
    ``sd.orchestrator.run_narration_turn`` with an ``AsyncMock``.

    **Multiplayer (ADR-036):**
    ``factory(slug=..., mode=GameMode.MULTIPLAYER, seat_players=[...], active_player=(...))``
    Returns ``(handler, sd, room)`` — a fully wired multi-player setup where
    ``handler._room`` is a ``SessionRoom`` with the given players seated.

    Task 11 (story 3.4): used by test_confrontation_dispatch_wiring.py.
    Task 16 (story 3.4): snapshot now includes a Character named "Rux" so
    XP-award tests can inspect ``sd.snapshot.characters[0].core.xp``.
    Task 3 (ADR-036): extended with multiplayer calling convention.
    """
    from typing import TYPE_CHECKING
    from unittest.mock import MagicMock

    import sidequest.genre.loader as _genre_loader_mod
    from sidequest.agents.orchestrator import Orchestrator
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.repository import SaveRepository
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.loader import GenreLoader
    from sidequest.server.session_handler import (
        WebSocketSessionHandler,
        _SessionData,
        _State,
    )
    from sidequest.server.session_room import SessionRoom
    from tests._helpers.session_room import room_for

    if TYPE_CHECKING:
        from sidequest.game.persistence import GameMode

    def _make(
        genre: str = "caverns_and_claudes",
        *,
        slug: str | None = None,
        mode: GameMode | None = None,
        seat_players: list[tuple[str, str]] | None = None,
        active_player: tuple[str, str] | None = None,
        existing_room: SessionRoom | None = None,
    ):
        # Read DEFAULT_GENRE_PACK_SEARCH_PATHS from the module at call-time so
        # that the _fixture_pack_search_paths monkeypatch is visible here.
        pack = GenreLoader(_genre_loader_mod.DEFAULT_GENRE_PACK_SEARCH_PATHS).load(genre)
        snap = GameSnapshot(genre_slug=genre)
        core = CreatureCore(
            name="Rux",
            description="A stoic fighter",
            personality="stoic",
            inventory=Inventory(),
        )
        char = Character(
            core=core,
            char_class="Fighter",
            race="Human",
            backstory="A wandering fighter",
        )
        snap.characters.append(char)
        # ADR-115 F1: the factory's repository is a MagicMock(spec=SaveRepository).
        # Consumers of this factory either mock save/append_narrative (MP path
        # below) or never read persisted state back; the one test that does
        # round-trip narrative builds a real PgSaveRepository itself.
        repository = MagicMock(spec=SaveRepository)
        orch = MagicMock(spec=Orchestrator)

        # Determine player identity — defaults to legacy single-player "Rux".
        if active_player is not None:
            active_pid, active_name = active_player
        else:
            active_pid, active_name = "player-1", "Rux"

        sd = _SessionData(
            genre_slug=genre,
            world_slug="",
            player_name=active_name,
            player_id=active_pid,
            snapshot=snap,
            repository=repository,
            dungeon_repository=MagicMock(),
            telemetry_sink=MagicMock(),
            genre_pack=pack,
            orchestrator=orch,
        )
        handler = WebSocketSessionHandler(save_dir=tmp_path)
        handler._session_data = sd
        # Task E.2 wiring: every turn flowing through this handler will hit
        # ``_apply_narration_result_to_snapshot`` which requires
        # ``sd._room``. The MP path below replaces this with the seated
        # SessionRoom; the legacy single-player path falls through with
        # this binding intact.
        sd._room = room_for(snap, slug=genre)

        # ---- Multiplayer room wiring (ADR-036 Task 3) ----
        if slug is not None and mode is not None and seat_players is not None:
            # Force handler into Playing state so _handle_player_action reaches
            # the barrier logic (MP tests start post-chargen). Only done here —
            # not for the legacy single-player path — so tests that probe
            # pre-connect guard behaviour (e.g. test_dice_throw_returns_error_when_not_playing)
            # still see AwaitingConnect.
            handler._state = _State.Playing

            if existing_room is not None:
                # Share the existing room — reuse its snapshot + repository so
                # the TurnManager barrier state is shared across handlers.
                # SessionRoom.store is a SaveRepository (a MagicMock from
                # room_for) — pass it straight through as repository=.
                room = existing_room
                snap = room.snapshot
                shared_repository = room.store
                # Rebuild _SessionData against the shared snapshot/repository.
                if active_player is not None:
                    active_pid, active_name = active_player
                else:
                    active_pid, active_name = "player-1", "Rux"
                sd = _SessionData(
                    genre_slug=genre,
                    world_slug="",
                    player_name=active_name,
                    player_id=active_pid,
                    snapshot=snap,
                    repository=shared_repository,
                    dungeon_repository=MagicMock(),
                    telemetry_sink=MagicMock(),
                    genre_pack=sd.genre_pack,
                    orchestrator=sd.orchestrator,
                )
                sd.repository.save = MagicMock()
                sd.repository.append_narrative = MagicMock()
                sd._room = room
                handler._session_data = sd
                handler._room = room
                return handler, sd, room

            # In MP mode, add a Character to the snapshot for each seat so
            # that _resolve_acting_character_name can match by slot name.
            # The legacy "Rux" character added above stays for compatibility
            # but we also add one per seated player.
            existing_names = {c.core.name for c in snap.characters}
            for _pid, character_slot in seat_players:
                if character_slot not in existing_names:
                    mp_core = CreatureCore(
                        name=character_slot,
                        description=f"{character_slot} the adventurer",
                        personality="bold",
                        inventory=Inventory(),
                    )
                    mp_char = Character(
                        core=mp_core,
                        char_class="Fighter",
                        race="Human",
                        backstory="A wandering adventurer",
                    )
                    snap.characters.append(mp_char)
                    existing_names.add(character_slot)

            room = SessionRoom(slug=slug, mode=mode)
            # Bind a snapshot + repository so the room is fully initialised.
            # bind_world expects a SaveRepository (post-D2); the primary
            # construction above already wrapped this SQLite store in one,
            # so reuse the same repository instance the session_data holds.
            room.bind_world(snapshot=snap, store=sd.repository)
            # Connect and seat every player. The fixture's intent is a
            # post-chargen "in-game" room, so each peer is promoted to
            # PLAYING — this is what existing barrier tests assume and
            # what Story 45-2 made explicit. Tests that need a CHARGEN /
            # ABANDONED scenario override `_seated[pid].state` directly.
            for i, (pid, character_slot) in enumerate(seat_players):
                room.connect(pid, socket_id=f"sock-{i}")
                room.seat(pid, character_slot=character_slot)
                room.transition_to_playing(pid)
                # Wave 2B (story 45-48): party_location() reads
                # snapshot.player_seats; mirror the room's seat map so
                # the per-character location accessors work in tests.
                snap.player_seats[pid] = character_slot
            handler._room = room
            sd._room = room
            # Silence broadcast so tests don't need a real WebSocket.
            room.broadcast = MagicMock()  # type: ignore[method-assign]
            # Silence repository side-effects. Production calls
            # sd.repository.save / append_narrative (post-D1); these patches
            # also fill the two methods SqliteSaveRepository does not define.
            sd.repository.save = MagicMock()
            sd.repository.append_narrative = MagicMock()
            return handler, sd, room

        # Legacy return: (sd, handler).
        return sd, handler

    return _make
