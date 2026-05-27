"""Story 24-10 RED tests — full ConnectHandler bootstrap wires
world-grounding fields onto ``_SessionData``.

This is the "every test suite needs a wiring test" gate for the
session-bootstrap seam (AC2, AC3) plus the end-to-end tool-dispatch
assertion (AC6) and the legitimate-absence + fail-loud cases (AC7, AC8).

Why this file:
  * ``test_world_grounding_loader.py`` proves the loader functions work.
  * ``test_world_grounding_wiring.py`` proves the dataclass fields exist
    and flow sd → tc → ToolContext through the SDK production path.
  * THIS file proves the ConnectHandler — the only production seam that
    can call the loaders at session bootstrap — actually does so, and
    that the resulting fields reach a real ``get_world_grounding``
    dispatch with non-null payloads.

Without this file, the green branches of the other two are necessary but
not sufficient — Pattern-1 ("each component green, integration seam
unowned") is exactly what story 24-10 exists to close.

The grounded-pack flavour uses a cloned synthetic pack with the three
YAML files dropped in (rather than the real tea_and_murder/glenross
genre pack) so the test isn't coupled to tea_and_murder rules / classes /
dungeon attachments / etc. The bootstrap code path it exercises is
identical — what differs is only how heavy the fixture is.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext, ToolResultStatus, default_registry
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.game.session import GameSnapshot
from sidequest.protocol import GameMessage
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import WebSocketSessionHandler, _build_turn_context
from sidequest.server.session_room import RoomRegistry

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

_WORLD = "flickering_reach"


# ---------------------------------------------------------------------------
# Pack fixtures
# ---------------------------------------------------------------------------


def _clone_test_genre(tmp_path: Path, slug: str) -> Path:
    """Copy the test_genre fixture pack to ``tmp_path/<slug>`` and rewrite
    ``lethality_policy.yaml.genre_key`` so it matches the new dirname
    (the loader rejects any mismatch — same pattern as minimal_pack_factory
    in tests/conftest.py)."""
    pack_dir = tmp_path / slug
    shutil.copytree(FIXTURE_PACKS / "test_genre", pack_dir)
    lethality_yaml = pack_dir / "lethality_policy.yaml"
    policy_data = yaml.safe_load(lethality_yaml.read_text(encoding="utf-8")) or {}
    policy_data["genre_key"] = slug
    lethality_yaml.write_text(
        yaml.dump(policy_data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return pack_dir


@pytest.fixture
def grounded_pack(tmp_path: Path) -> tuple[Path, str]:
    """Clone test_genre into tmp + drop in real tea_and_murder/glenross
    grounding YAMLs (pack-level weather.yaml + world-level demographics +
    calendar). Returns (search_paths_root, genre_slug)."""
    slug = "grounded_pack"
    pack_dir = _clone_test_genre(tmp_path, slug)

    real_pack = CONTENT_GENRE_PACKS / "tea_and_murder"
    real_world = real_pack / "worlds" / "glenross"

    shutil.copy(real_pack / "weather.yaml", pack_dir / "weather.yaml")
    world_dir = pack_dir / "worlds" / _WORLD
    shutil.copy(real_world / "demographics.yaml", world_dir / "demographics.yaml")
    shutil.copy(real_world / "calendar.yaml", world_dir / "calendar.yaml")

    return tmp_path, slug


@pytest.fixture
def bare_pack(tmp_path: Path) -> tuple[Path, str]:
    """A clone of test_genre WITHOUT grounding YAML — represents the
    legitimate "pack declares no world-grounding" branch (AC7).
    caverns_and_claudes is the production analog."""
    slug = "bare_pack"
    _clone_test_genre(tmp_path, slug)
    return tmp_path, slug


@pytest.fixture
def malformed_weather_pack(tmp_path: Path) -> tuple[Path, str]:
    """A clone of test_genre with a syntactically invalid weather.yaml.
    AC8: bootstrap must fail loud."""
    slug = "malformed_weather_pack"
    pack_dir = _clone_test_genre(tmp_path, slug)
    (pack_dir / "weather.yaml").write_text(
        "climate_zones: { unterminated mapping\n",
        encoding="utf-8",
    )
    return tmp_path, slug


# ---------------------------------------------------------------------------
# Save seeding (mirrors test_event_log_wiring)
# ---------------------------------------------------------------------------


_SLUG = "world-grounding-bootstrap-fixture"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database.

    Under ADR-115 D2 the slug-connect path resolves the authoritative game
    row and snapshot from Postgres via ``db_pool.get_pool()`` (resolving
    SIDEQUEST_DATABASE_URL), reading the SQLite save_dir db only for the
    bootstrap genre/world/mode. The shared ``sidequest_test`` db otherwise
    accumulates rows across tests — a fixed-slug connect then loads a prior
    test's genre_slug / characters. Bind the pool to the migrated throwaway
    db so each test seeds and connects against one isolated database.
    """
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    # migrated_db is session-scoped (shared per xdist worker); TRUNCATE the
    # per-test state so a sibling test's fixed-slug row can't be resumed here.
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


def _seed_solo_save(save_dir: Path, genre_slug: str) -> None:
    """Register a SOLO session in Postgres carrying one Character so the
    slug-connect branch goes straight to Playing (skipping chargen).

    ADR-115 F1: the connect path loads the authoritative snapshot + bootstrap
    row from Postgres (the SQLite save layer was retired)."""
    core = CreatureCore(
        name="Thorn",
        description="A wandering investigator",
        personality="Curious",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A wanderer.",
    )
    snap = GameSnapshot(genre_slug=genre_slug, world_slug=_WORLD)
    snap.characters = [char]

    # ADR-115 D2: mirror the snapshot into the PG store the connect path
    # actually loads from so has_character=True → Playing (skips chargen).
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=_SLUG,
        mode=str(GameMode.SOLO),
        genre_slug=genre_slug,
        world_slug=_WORLD,
    )
    repo.save(snap)


def _build_handler(
    save_dir: Path,
    search_root: Path,
) -> tuple[WebSocketSessionHandler, asyncio.Queue[object]]:
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[search_root],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id="sock-test",
        out_queue=queue,
    )
    return handler, queue


def _connect_msg() -> GameMessage:
    return GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG,
                "last_seen_seq": 0,
                "player_name": "Alice",
            },
        }
    )


# ---------------------------------------------------------------------------
# AC2 + AC3 — Bootstrap loads the YAMLs and populates _SessionData
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_populates_session_data_grounding_fields(
    grounded_pack: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """AC2 + AC3 wiring test: when a session connects to a pack that
    ships ``weather.yaml`` + ``worlds/<world>/demographics.yaml`` +
    ``worlds/<world>/calendar.yaml``, the ConnectHandler MUST populate
    ``_SessionData.weather_state`` (a real ``WeatherState`` — i.e. the
    generator was constructed AND ``generate()`` was called per AC3),
    ``_SessionData.world_demographics`` (the loaded dict), and
    ``_SessionData.world_calendar`` (the loaded dict).

    A failure here means the loader was never called from production —
    the exact Pattern-1 bug story 24-10 closes."""
    search_root, genre_slug = grounded_pack
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    _seed_solo_save(save_dir, genre_slug)

    handler, _queue = _build_handler(save_dir, search_root)

    out = await handler.handle_message(_connect_msg())

    # Sanity — connection succeeded into Playing state.
    session_events = [m for m in out if getattr(m, "type", None) == MessageType.SESSION_EVENT]
    assert session_events, f"expected SESSION_EVENT connected; got {out}"
    assert getattr(session_events[0].payload, "has_character", False) is True

    sd = handler._session_data
    assert sd is not None, "_session_data not populated after connect"

    # AC3 — weather_state is a real WeatherState (generator was constructed
    # AND generate() was called once at bootstrap).
    assert sd.weather_state is not None, (
        "sd.weather_state is None after bootstrap of a pack that ships "
        "weather.yaml — the loader/generator wiring is not engaged. This is "
        "the Pattern-1 bug story 24-10 closes."
    )
    # The 24-7 OTEL hook embedded in WeatherGenerator.generate() fires per
    # WeatherState produced — that produced one. Asserting the type-shape
    # rather than concrete values (deterministic seed is impl-detail).
    from sidequest.game.weather import WeatherState

    assert isinstance(sd.weather_state, WeatherState), (
        f"sd.weather_state must be a WeatherState (real generator output), "
        f"got {type(sd.weather_state).__name__}"
    )

    # AC2 — demographics + calendar dicts.
    assert sd.world_demographics is not None, (
        "sd.world_demographics is None after bootstrap of a pack that ships "
        "worlds/<world>/demographics.yaml — loader not called"
    )
    assert isinstance(sd.world_demographics, dict)
    assert sd.world_calendar is not None, (
        "sd.world_calendar is None after bootstrap of a pack that ships "
        "worlds/<world>/calendar.yaml — loader not called"
    )
    assert isinstance(sd.world_calendar, dict)


# ---------------------------------------------------------------------------
# AC6 — End-to-end: get_world_grounding tool returns a non-null payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_world_grounding_returns_grounded_payload_through_dispatch(
    grounded_pack: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """AC6 (mandatory wiring): after bootstrap, calling the real
    ``get_world_grounding`` tool via ``default_registry`` with a
    ToolContext derived from the session's TurnContext returns a payload
    with non-null ``weather`` (round-trips ``WeatherState.model_dump()``),
    non-null ``demographics``, and non-null ``calendar`` — i.e. the
    narrator-facing tool actually sees grounded state.

    This is the integration test the story-risk note flags as the single
    most load-bearing AC."""
    search_root, genre_slug = grounded_pack
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    _seed_solo_save(save_dir, genre_slug)

    handler, _queue = _build_handler(save_dir, search_root)
    await handler.handle_message(_connect_msg())
    sd = handler._session_data
    assert sd is not None

    # Production helper builds a TurnContext from sd — same path the SDK
    # narrator turn uses (already covered by test_world_grounding_wiring's
    # level-2 test).
    tc = _build_turn_context(sd, room=sd._room)

    # ToolContext constructor — kwargs MUST mirror the production site at
    # orchestrator.py:3259. The wiring test in test_world_grounding_wiring.py
    # asserts the SDK path passes these — here we use the same kwargs to
    # exercise the real dispatch loop end-to-end.
    tool_ctx = ToolContext(
        world_id=tc.world_id or sd.world_slug,
        session_id=tc.session_id or "test",
        perspective_pc=tc.character_name,
        turn_number=tc.turn_number,
        repository=sd.repository,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        weather_state=tc.weather_state,
        world_demographics=tc.world_demographics,
        world_calendar=tc.world_calendar,
    )

    # Real dispatch — exact tool the narrator calls.
    registered = default_registry._tools["get_world_grounding"]
    args = registered.args_model.model_validate({})
    result = await registered.handler(args, tool_ctx)

    assert result.status is ToolResultStatus.OK, (
        f"get_world_grounding returned non-OK status: {result.status} message={result.message!r}"
    )
    payload = cast(dict[str, Any], result.payload)
    assert payload["weather"] is not None, (
        "tool payload.weather is None after a grounded-pack bootstrap — "
        "ToolContext.weather_state did not reach the tool"
    )
    assert isinstance(payload["weather"], dict)
    # WeatherState.model_dump() includes 'zone' and 'season'.
    assert "zone" in payload["weather"] and "season" in payload["weather"], (
        f"weather payload missing WeatherState shape; got keys={sorted(payload['weather'])}"
    )
    assert payload["demographics"] is not None, (
        "tool payload.demographics is None after a grounded-pack bootstrap"
    )
    assert payload["calendar"] is not None, (
        "tool payload.calendar is None after a grounded-pack bootstrap"
    )


# ---------------------------------------------------------------------------
# AC7 — A pack without grounding bootstraps cleanly (legitimate absence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_leaves_grounding_none_when_pack_declares_nothing(
    bare_pack: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """AC7: a pack that does NOT author weather.yaml / demographics.yaml /
    calendar.yaml must connect cleanly. The three sd fields stay None,
    the dispatch span will surface ``*_present=False`` for them, and the
    24-7 ``world_grounding.*`` spans do not fire (those gate on `is not
    None`). No exception, no warning, no crash."""
    search_root, genre_slug = bare_pack
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    _seed_solo_save(save_dir, genre_slug)

    handler, _queue = _build_handler(save_dir, search_root)

    out = await handler.handle_message(_connect_msg())

    # Connection must succeed cleanly — no ERROR message in the output.
    error_msgs = [m for m in out if getattr(m, "type", None) == MessageType.ERROR]
    assert not error_msgs, (
        f"bare-pack bootstrap surfaced ERROR messages — pack with no "
        f"weather/demographics/calendar should connect cleanly: {error_msgs}"
    )

    sd = handler._session_data
    assert sd is not None
    assert sd.weather_state is None, (
        "sd.weather_state non-None for a pack that doesn't ship weather.yaml "
        "— that's a silent fallback / synthetic default"
    )
    assert sd.world_demographics is None
    assert sd.world_calendar is None


# ---------------------------------------------------------------------------
# AC8 — Malformed pack YAML fails loud at bootstrap (No Silent Fallbacks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_fails_loud_on_malformed_pack_weather_yaml(
    malformed_weather_pack: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """AC8 (CLAUDE.md "No Silent Fallbacks"): a pack that ships a
    weather.yaml but the file is syntactically invalid MUST surface a
    visible error at connect time — either as an ERROR-typed protocol
    message returned to the client, or as a raised exception from
    ``handle_message``. What MUST NOT happen is a silent ``None`` and a
    successful Playing-state connection — that turns into a mysterious
    "weather grounding absent" symptom three turns into a session, with
    nothing in the GM panel to explain why."""
    search_root, genre_slug = malformed_weather_pack
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    _seed_solo_save(save_dir, genre_slug)

    handler, _queue = _build_handler(save_dir, search_root)

    raised: Exception | None = None
    out: list[Any] = []
    try:
        out = list(await handler.handle_message(_connect_msg()))
    except Exception as exc:  # noqa: BLE001 — fail-loud surface check
        raised = exc

    if raised is not None:
        # Raised path is acceptable — the malformed file surfaced loud.
        return

    # Non-raise path: must surface an ERROR message AND must NOT report
    # has_character=True (i.e. must NOT complete a Playing-state connect).
    error_msgs = [m for m in out if getattr(m, "type", None) == MessageType.ERROR]
    assert error_msgs, (
        "malformed weather.yaml at pack level was silently swallowed: "
        "connect handler returned no ERROR message AND no exception. "
        "That's the exact silent-fallback CLAUDE.md forbids."
    )

    # If a SESSION_EVENT connected fired, the bootstrap accepted the broken
    # pack — that's the failure mode too.
    connected_events = [
        m
        for m in out
        if getattr(m, "type", None) == MessageType.SESSION_EVENT
        and getattr(m.payload, "event", "") == "connected"
    ]
    if connected_events:
        # We have both an ERROR and a connected event — accept this if the
        # connected payload reports has_character=False (i.e. bootstrap
        # rejected the session). Anything else is silent acceptance.
        ev = connected_events[0]
        has_character = getattr(ev.payload, "has_character", None)
        assert has_character is not True, (
            "malformed weather.yaml produced a normal Playing-state "
            "connect: has_character=True alongside no fatal error. The "
            "loader silently fell back to no-weather, exactly the silent-"
            "fallback class of bug AC8 forbids."
        )
