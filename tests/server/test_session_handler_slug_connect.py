"""Tests for WebSocket connect accepting game_slug (Task 4, MP-01).

Verifies that SESSION_EVENT{connect} with a game_slug field:
- loads the game from the slug-based SQLite store and emits SESSION_CONNECTED
- emits ERROR when the slug doesn't correspond to a known game
- genre_pack is populated (not None) so PLAYER_ACTION doesn't crash
- a slug-connect for a session with a saved snapshot resumes rather than restarting
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import (
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

# Use a genre pack that exists in the content repo.
_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    Slug-connect (ADR-115 D2) loads the authoritative snapshot from PG via
    db_pool.get_pool(); seed and connect must share one isolated database.
    """
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


def _seed_pg_for_slug(
    slug: str,
    snap: GameSnapshot,
    *,
    mode: GameMode = GameMode.MULTIPLAYER,
    genre_slug: str = _GENRE,
    world_slug: str = _WORLD,
) -> None:
    """Mirror a seeded snapshot into PG — the store the slug-resume path loads."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(mode),
        genre_slug=genre_slug,
        world_slug=world_slug,
    )
    repo.save(snap)


def _make_handler(save_dir: Path, search_paths: list[Path]) -> WebSocketSessionHandler:
    """Construct a handler with room-context wiring.

    Mirrors what ws_endpoint does: build the handler, then immediately call
    attach_room_context with a fresh RoomRegistry, a unique socket_id, and an
    asyncio.Queue for outbound messages. The slug-connect branch requires
    all three — there is no silent test-only bypass.
    """
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=search_paths,
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-test",
        out_queue=asyncio.Queue(),
    )
    return handler


_SLUG = "2026-04-22-grimvault-test"

# Resolve the content search path relative to this file so tests work from
# any working directory.
# __file__ = oq-2/sidequest-server/tests/server/<file>.py
# parents[3] = oq-2 (orchestrator root)
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture
def seeded_game(tmp_path: Path) -> Path:
    """Register an empty MP session in Postgres (no snapshot → chargen).

    ADR-115 F1: connect resolves the bootstrap row from PG. ``_build_pg_
    repos_for_slug`` ensures the session row without persisting a snapshot,
    so connect sees has_character=False and enters Creating.
    """
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=_SLUG,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_connect_by_slug_loads_existing_game(seeded_game: Path):
    handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(
            event="connect",
            game_slug=_SLUG,
        ),
    )
    outbound = await handler.handle_message(msg)
    assert any(getattr(m, "type", None) == "SESSION_EVENT" for m in outbound), (
        f"Expected SESSION_EVENT(connected) in outbound, got: {[getattr(m, 'type', None) for m in outbound]}"
    )
    # Verify the connected event has event="connected"
    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs, f"Expected SESSION_EVENT{{connected}} in outbound, got: {outbound}"
    assert handler.session_data is not None
    assert handler.session_data.game_slug == _SLUG
    assert handler.session_data.mode == GameMode.MULTIPLAYER
    # Bug 1 regression: genre_pack must be a real GenrePack, never None.
    assert handler.session_data.genre_pack is not None, (
        "genre_pack must not be None after slug-connect — PLAYER_ACTION would crash"
    )


@pytest.mark.asyncio
async def test_slug_connect_emits_theme_css(seeded_game: Path):
    """ADR-079: slug-connect emits SESSION_EVENT{theme_css} with the genre's
    client_theme.css content so the UI's useGenreTheme hook can apply it.

    Without this event the UI silently falls back to the dark-mode shadcn
    defaults — a mishmash of stock vars that doesn't match any genre. The
    server-side wiring (loader reads the file, connect handler emits it)
    is the missing piece this test guards against regression.
    """
    handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(
            event="connect",
            game_slug=_SLUG,
        ),
    )
    outbound = await handler.handle_message(msg)

    theme_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "theme_css"
    ]
    assert theme_msgs, (
        "Expected SESSION_EVENT{theme_css} in outbound — UI cannot apply "
        f"genre theme without it. Got events: "
        f"{[getattr(getattr(m, 'payload', None), 'event', None) for m in outbound]}"
    )
    payload = theme_msgs[0].payload
    assert payload.genre == _GENRE
    assert payload.world == _WORLD
    assert payload.css, "theme_css payload missing CSS content"
    # Sanity check: caverns_and_claudes ships :root[data-genre] selector
    # per ADR-079 (post-unification). If this assertion fails the genre
    # pack drifted out of compliance.
    assert ":root[data-genre]" in payload.css, (
        "Genre CSS must use :root[data-genre] selector (ADR-079) — "
        "specificity bump is what beats stock .dark defaults."
    )
    # Theme must arrive BEFORE the chargen scene / ready event so the UI
    # paints the right colors on first render, not after a flash.
    types_and_events = [
        (getattr(m, "type", None), getattr(getattr(m, "payload", None), "event", None))
        for m in outbound
    ]
    theme_idx = next(
        i for i, (t, e) in enumerate(types_and_events) if t == "SESSION_EVENT" and e == "theme_css"
    )
    connected_idx = next(
        i for i, (t, e) in enumerate(types_and_events) if t == "SESSION_EVENT" and e == "connected"
    )
    assert connected_idx < theme_idx, (
        f"theme_css must follow connected (got connected@{connected_idx}, theme@{theme_idx})"
    )


@pytest.mark.asyncio
async def test_slug_connect_rewrites_font_urls_to_cdn(
    seeded_game: Path, monkeypatch: pytest.MonkeyPatch
):
    """Font (and asset) url()s in the genre CSS are routed through the
    asset_urls seam before the theme_css event is emitted, so the browser
    pulls fonts from R2 (CDN) — never from the raw /genre/ mount and never
    from fonts.googleapis.com.

    This is the wiring guard for the rewrite_theme_css_asset_urls integration
    in connect.py: loader reads the pack CSS, connect rewrites it, UI receives
    cdn URLs. Default (no SIDEQUEST_ASSET_BASE_URL) = CDN mode.
    """
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(event="connect", game_slug=_SLUG),
    )
    outbound = await handler.handle_message(msg)

    theme_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "theme_css"
    ]
    assert theme_msgs, "no theme_css event emitted"
    css = theme_msgs[0].payload.css

    # No Google Fonts anywhere — the genre pack must self-host.
    assert "fonts.googleapis.com" not in css, (
        "genre CSS still references Google Fonts after rewrite"
    )
    # caverns_and_claudes ships at least one self-hosted @font-face; after the
    # rewrite its src must point at the CDN, not the raw /genre/ mount.
    assert "https://cdn.slabgorb.com/genre_packs/assets/fonts/" in css, (
        "expected font url() rewritten to the CDN asset seam"
    )
    assert "url('/genre/assets/fonts/" not in css and "url(/genre/assets/fonts/" not in css, (
        "raw /genre/ font mount survived the rewrite"
    )


@pytest.mark.asyncio
async def test_slug_connect_warns_loudly_when_theme_css_absent(
    seeded_game: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    """Story 158-9: a connect that resolves NO client_theme.css must fail
    LOUDLY (No-Silent-Fallbacks), not silently emit nothing and leave the UI to
    collapse ``--accent`` to the near-invisible dark default.

    Every live genre pack ships a genre-level client_theme.css, so a "none"
    theme source on connect signals a real misconfig (the 2026-06-21
    beneath_sunden playtest finding). We force that state by nulling the loaded
    pack's (and its worlds') ``client_theme_css``, then assert:
      - NO theme_css SESSION_EVENT is emitted (we're on the absent branch), and
      - a WARNING ``session.theme_css_absent`` log line surfaces the gap
        server-side immediately, instead of waiting on the UI's 8s loud-fail
        banner.
    """
    from sidequest.genre.loader import GenreLoader

    real_load = GenreLoader.load

    def _load_without_theme(self, slug, *args, **kwargs):  # noqa: ANN001, ANN202
        pack = real_load(self, slug, *args, **kwargs)
        pack.client_theme_css = None
        for world in pack.worlds.values():
            world.client_theme_css = None
        return pack

    monkeypatch.setattr(GenreLoader, "load", _load_without_theme)

    handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(event="connect", game_slug=_SLUG),
    )
    with caplog.at_level(logging.WARNING, logger="sidequest.handlers.connect"):
        outbound = await handler.handle_message(msg)

    # Absent branch: no theme_css event at all.
    theme_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "theme_css"
    ]
    assert not theme_msgs, (
        "theme_css must NOT be emitted when no client_theme.css resolves — "
        f"got {len(theme_msgs)} theme event(s)"
    )

    # Loud guard: a WARNING log line names the gap so it surfaces server-side.
    absent_records = [r for r in caplog.records if "session.theme_css_absent" in r.getMessage()]
    assert absent_records, (
        "expected a loud session.theme_css_absent WARNING when the genre theme "
        "is missing — No-Silent-Fallbacks. Got: "
        f"{[r.getMessage()[:60] for r in caplog.records]}"
    )
    assert absent_records[0].levelno == logging.WARNING, (
        "theme_css_absent must be logged at WARNING (loud), not below"
    )


@pytest.mark.asyncio
async def test_connect_by_unknown_slug_errors(seeded_game: Path):
    handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(
            event="connect",
            game_slug="2020-01-01-nowhere",
        ),
    )
    outbound = await handler.handle_message(msg)
    assert any(getattr(m, "type", None) == "ERROR" for m in outbound), (
        f"Expected ERROR in outbound, got: {[getattr(m, 'type', None) for m in outbound]}"
    )


@pytest.mark.asyncio
async def test_slug_connect_resumes_saved_snapshot(tmp_path: Path):
    """Bug 2 regression: slug-connect with a saved session restores it.

    Seeds a game row *and* a saved GameSnapshot with one character so
    has_character comes back True and state is Playing (not Creating).
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-04-22-resume-test"

    # Build a minimal character and save a snapshot that contains it.
    core = CreatureCore(
        name="Rux",
        description="A stoic fighter",
        personality="stoic",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="A wandering fighter")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [char]
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        # Original player resuming: UI sends display_name as both player_id
        # and player_name. The MP-legacy-backfill branch will match
        # display_name "Rux" against the existing character "Rux" and
        # back-fill player_seats so subsequent joiners see the populated
        # seat map. See test_mp_legacy_save_resumes_original_player_by_name
        # for the explicit branch assertion.
        player_id="Rux",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
        ),
    )
    outbound = await handler.handle_message(msg)

    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs, f"Expected SESSION_EVENT(connected), got: {outbound}"

    connected_payload = connected_msgs[0].payload
    # has_character must reflect the saved snapshot, not hardcoded False.
    assert connected_payload.has_character is True, (
        "has_character should be True when the saved snapshot has a character"
    )
    sd = handler.session_data
    assert sd is not None
    assert sd.snapshot.characters, "Snapshot must carry the saved character after resume"
    assert sd.snapshot.characters[0].core.name == "Rux"


@pytest.mark.asyncio
async def test_slug_connect_emits_mp_span(seeded_game: Path):
    """Wiring test: mp.slug_connect span fires with the expected attrs.

    Per CLAUDE.md OTEL mandate — GM panel must be able to tell that
    slug-connect actually ran (vs. Claude improvising a plausible-looking
    narration). This test is the lie-detector for that claim.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
        msg = SessionEventMessage(
            type="SESSION_EVENT",
            player_id="alice",
            payload=SessionEventPayload(event="connect", game_slug=_SLUG),
        )
        await handler.handle_message(msg)

        mp_spans = [s for s in exporter.get_finished_spans() if s.name == "mp.slug_connect"]
        assert len(mp_spans) == 1, (
            f"Expected exactly one mp.slug_connect span, got {[s.name for s in exporter.get_finished_spans()]}"
        )
        span = mp_spans[0]
        assert span.attributes["slug"] == _SLUG
        assert span.attributes["player_id"] == "alice"
        # row.mode is GameMode.MULTIPLAYER → "multiplayer"
        assert span.attributes["mode"] == "multiplayer"
        # Fresh connect — no one was paused before.
        assert span.attributes["was_paused_before"] is False
        assert span.attributes["resolved_pause"] is False
        assert span.attributes["connected_count"] == 1
    finally:
        processor.shutdown()


@pytest.mark.asyncio
async def test_slug_connect_routes_new_player_to_chargen_when_seat_taken(tmp_path: Path):
    """Per-player chargen gate (playtest 2026-04-25 bug 1).

    A snapshot with ``player_seats={"P1": "Laverne"}`` already seats Player 1.
    When Player 2 (a *new* player_id) connects to the same slug, the gate
    must report ``has_character is False`` so the UI routes to chargen
    instead of auto-claiming Laverne.

    This is the wiring test OQ-2 flagged as missing — without it, a future
    refactor could regress the gate to ``bool(snapshot.characters)`` and
    no test would catch it.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-04-25-multiseat-test"

    core = CreatureCore(
        name="Laverne",
        description="A blunt delver",
        personality="brusque",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="P1's PC")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [char]
    snap.player_seats = {"P1": "Laverne"}
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="P2",
        payload=SessionEventPayload(event="connect", game_slug=slug),
    )
    outbound = await handler.handle_message(msg)

    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs, f"Expected SESSION_EVENT(connected), got: {outbound}"
    payload = connected_msgs[0].payload
    assert payload.has_character is False, (
        "P2 (new player_id) must NOT inherit P1's seat — gate must report "
        "has_character=False so the UI routes to chargen"
    )


@pytest.mark.asyncio
async def test_slug_connect_resumes_seated_player_by_id(tmp_path: Path):
    """Same-player_id resume keeps the gate at has_character=True.

    Companion to the new-player-routes-to-chargen test above. P1 reconnecting
    to a slug where ``player_seats`` already binds them must resume their PC.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-04-25-resume-seated-test"

    core = CreatureCore(
        name="Laverne",
        description="A blunt delver",
        personality="brusque",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="P1's PC")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [char]
    snap.player_seats = {"P1": "Laverne"}
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="P1",
        payload=SessionEventPayload(event="connect", game_slug=slug),
    )
    outbound = await handler.handle_message(msg)

    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs, f"Expected SESSION_EVENT(connected), got: {outbound}"
    payload = connected_msgs[0].payload
    assert payload.has_character is True, (
        "P1 reconnecting to their own seat must resume — gate reports has_character=True"
    )


@pytest.mark.asyncio
async def test_slug_connect_chargen_gate_logs_branch_decision(tmp_path: Path, caplog):
    """OTEL mandate (CLAUDE.md): the chargen-gate decision must be loggable.

    Without this log line, GM panel can't tell whether the per-player gate
    actually ran or whether the snapshot just happened to be empty. That's
    a CLAUDE.md OTEL Observability Principle violation.
    """
    import logging

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-04-25-gate-log-test"

    core = CreatureCore(
        name="Laverne",
        description="d",
        personality="p",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="b")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [char]
    snap.player_seats = {"P1": "Laverne"}
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="P2",
        payload=SessionEventPayload(event="connect", game_slug=slug),
    )
    with caplog.at_level(logging.INFO, logger="sidequest.handlers.connect"):
        await handler.handle_message(msg)

    gate_records = [r for r in caplog.records if "session.chargen_gate" in r.getMessage()]
    assert gate_records, (
        "Chargen-gate must emit an info-level log line so GM panel can verify which branch fired"
    )
    msg_text = gate_records[0].getMessage()
    assert "branch=player_seats" in msg_text, (
        f"With populated player_seats, branch=player_seats expected; got: {msg_text}"
    )
    assert "has_character=False" in msg_text, (
        f"P2 connecting to a slug seated by P1 must log has_character=False; got: {msg_text}"
    )


@pytest.mark.asyncio
async def test_mp_legacy_save_routes_new_joiner_to_chargen(tmp_path: Path, caplog):
    """Playtest 2026-04-25 P0: MP slug with characters but empty player_seats.

    Pre-fix: when Laverne's chargen completed on a server BEFORE the
    chargen-confirmation seat-binding landed, her save has
    ``characters=[Laverne]`` but ``player_seats={}``. The legacy fallback
    branch read ``has_character = bool(snapshot.characters)`` and routed
    ANY connecting player straight to slug_resume — Player 2 connected
    with name "Squiggy" and landed on Laverne's character sheet labeled
    "(YOU)".

    Post-fix: in MP mode with empty player_seats, the gate matches
    ``display_name`` against existing character names. Squiggy doesn't
    match → has_character=False → routed to chargen, plus a watcher
    event ``mp_new_joiner_chargen_required`` for the GM panel.
    """
    import logging

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-04-25-mp-legacy-no-seats"

    core = CreatureCore(
        name="Laverne",
        description="d",
        personality="p",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="b")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [char]
    # Crucial: empty player_seats simulates pre-binding chargen save.
    snap.player_seats = {}
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    # Squiggy connects with their own display name (UI sends displayName as
    # both player_id and player_name on slug-connect).
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="Squiggy",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Squiggy",
        ),
    )
    with caplog.at_level(logging.INFO, logger="sidequest.handlers.connect"):
        outbound = await handler.handle_message(msg)

    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs, f"Expected SESSION_EVENT(connected), got: {outbound}"
    payload = connected_msgs[0].payload
    assert payload.has_character is False, (
        "Squiggy (new joiner, MP, empty seats) must NOT inherit Laverne's "
        "character — gate must report has_character=False so the UI routes "
        "to chargen"
    )
    # ``ready`` events mean slug_resume fired — the bug. There must be NO
    # SESSION_EVENT(ready) on this connect.
    ready_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "ready"
    ]
    assert not ready_msgs, (
        f"Squiggy must NOT receive SESSION_EVENT(ready) (slug_resume) — "
        f"chargen fork required. Got: {ready_msgs}"
    )
    # Gate-decision log must show the new MP-aware branch fired.
    gate_records = [r for r in caplog.records if "session.chargen_gate" in r.getMessage()]
    assert gate_records, "Chargen-gate must log its decision"
    msg_text = gate_records[0].getMessage()
    assert "branch=mp_new_joiner_chargen_required" in msg_text, (
        f"Expected MP-new-joiner branch; got: {msg_text}"
    )


@pytest.mark.asyncio
async def test_mp_legacy_save_resumes_original_player_by_name(tmp_path: Path, caplog):
    """Companion to the new-joiner test: original player CAN resume.

    Laverne reconnects to her own pre-binding save. ``display_name=Laverne``
    matches the existing character → has_character=True → resume, AND the
    seat is back-filled so subsequent joiners hit the authoritative
    ``player_seats`` branch.
    """
    import logging

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-04-25-mp-legacy-backfill"

    core = CreatureCore(
        name="Laverne",
        description="d",
        personality="p",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="b")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [char]
    snap.player_seats = {}
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="Laverne",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Laverne",
        ),
    )
    with caplog.at_level(logging.INFO, logger="sidequest.handlers.connect"):
        outbound = await handler.handle_message(msg)

    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs
    assert connected_msgs[0].payload.has_character is True
    # Branch log must show backfill.
    gate_records = [r for r in caplog.records if "session.chargen_gate" in r.getMessage()]
    assert gate_records
    assert "branch=mp_legacy_backfill" in gate_records[0].getMessage()


@pytest.mark.parametrize(
    "genre_slug,world_slug,location",
    [
        # Original Mawdeep regression (caverns_and_claudes/grimvault has
        # opening hooks, used as proxy for the same shape Mawdeep had).
        ("caverns_and_claudes", "grimvault", "Sinkhole Inn Room"),
        # Playtest 2026-04-26 [S2-BUG] coyote_star regression: George
        # got a fresh ``arena_trial`` cold-open even though John was
        # already in the world. Same suppression must reach this pack
        # (and any future pack with opening hooks).
        ("space_opera", "coyote_star", "Trail Junction"),
    ],
)
@pytest.mark.asyncio
async def test_mp_joiner_suppresses_opening_seed(
    tmp_path: Path,
    caplog,
    genre_slug: str,
    world_slug: str,
    location: str,
):
    """Wiring test: MP joiner does NOT inherit the cold-open hook.

    Playtest 2026-04-26 "Multiplayer parallel-solo desynchronizes scene
    context entirely". When Player 2 joins a slug that already has a
    seated character, completing chargen used to trigger
    ``_run_opening_turn_narration`` with the genre pack's
    in-medias-res ``opening_seed`` + ``opening_directive``. The narrator
    obeyed the directive and invented a NEW scene divorced from where
    Player 1 already was — Ralph stuck at the Sinkhole Inn while Potsie
    descended into "THE THROAT".

    Post-fix: when ``has_character=False`` AND the snapshot already has
    >=1 character, the connect handler suppresses ``opening_seed`` and
    ``opening_directive`` on the new joiner's session, and emits the
    ``mp_joiner_opening_suppressed`` watcher event so the GM panel can
    confirm the suppression. The post-chargen narration falls back to
    the generic "I look around…" action which the shared narrator
    (ADR-067) handles as a continuation of the existing scene.

    Parametrized over packs so a future genre with opening hooks
    (space_opera/coyote_star being the prompt for parametrization)
    can't silently regress.
    """
    import logging

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = f"2026-04-26-mp-joiner-opening-suppressed-{world_slug}"

    # Seat the host so the joiner sees a populated snapshot. player_seats
    # populated → the gate's ``player_seats`` branch fires (joiner absent
    # → has_character=False), which is the canonical MP-joiner path
    # post-MP-02.
    core = CreatureCore(
        name="Host",
        description="d",
        personality="p",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Fighter", race="Human", backstory="b")
    snap = GameSnapshot(genre_slug=genre_slug, world_slug=world_slug, location=location)
    snap.characters = [char]
    snap.player_seats = {"host-id": "Host"}
    _seed_pg_for_slug(
        slug,
        snap,
        mode=GameMode.MULTIPLAYER,
        genre_slug=genre_slug,
        world_slug=world_slug,
    )

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="joiner-id",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Joiner",
        ),
    )
    with caplog.at_level(logging.INFO, logger="sidequest.handlers.connect"):
        await handler.handle_message(msg)

    # The session-data we just constructed must have None for both opening
    # fields — that's the actual state the post-chargen turn will read.
    sd = handler._session_data
    assert sd is not None, "Handler must have built _session_data on connect"
    assert sd.opening_seed is None, (
        "MP joiner must NOT inherit the genre pack's opening_seed; got: "
        f"{sd.opening_seed!r} (pack={genre_slug}/{world_slug})"
    )
    assert sd.opening_directive is None, (
        "MP joiner must NOT inherit the genre pack's opening_directive; got: "
        f"{sd.opening_directive!r} (pack={genre_slug}/{world_slug})"
    )
    # Suppression decision must be logged so a future drift can be diagnosed
    # from logs alone (not just from missing scenes).
    suppress_records = [
        r for r in caplog.records if "session.mp_joiner_opening_suppressed" in r.getMessage()
    ]
    assert suppress_records, (
        "Suppression must log session.mp_joiner_opening_suppressed for "
        f"GM-panel observability (pack={genre_slug}/{world_slug})"
    )


@pytest.mark.asyncio
async def test_slug_connect_backfills_presence_for_existing_peers(seeded_game: Path, caplog):
    """Playtest 2026-04-26 S2-BUG: PLAYER_PRESENCE back-fill on slug_connect.

    The MultiplayerSessionStatus widget on the chargen screen only showed
    the local player plus players who joined AFTER them. The server
    broadcasts PLAYER_PRESENCE on each new connect (so later joiners
    show up live for earlier joiners) but never sent the *new* connection
    a snapshot of who was already there. Net result for a 4-player game:
    P1 sees all 4, P2 sees 3, P3 sees 2, P4 sees only self.

    Fix verification: connect three players in order to a shared room.
    The third connection must receive PRESENCE{connected} frames for the
    first two players via its outbound queue. Existing peers must NOT
    receive duplicate frames for already-known players (only the live
    join broadcast for the *new* player). Plus the GM-panel watcher
    log line must be emitted.
    """
    import logging

    # Single shared RoomRegistry so all three handlers attach to the same
    # SessionRoom — that's what production does (one registry per app).
    registry = RoomRegistry()

    def _make_member_handler(socket_id: str) -> tuple[WebSocketSessionHandler, asyncio.Queue]:
        handler = WebSocketSessionHandler(
            save_dir=seeded_game,
            genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
        )
        out = asyncio.Queue()
        handler.attach_room_context(
            registry=registry,
            socket_id=socket_id,
            out_queue=out,
        )
        return handler, out

    async def _connect(handler: WebSocketSessionHandler, player_id: str, name: str) -> None:
        msg = SessionEventMessage(
            type="SESSION_EVENT",
            player_id=player_id,
            payload=SessionEventPayload(
                event="connect",
                game_slug=_SLUG,
                player_name=name,
            ),
        )
        await handler.handle_message(msg)

    h1, q1 = _make_member_handler("sock-1")
    h2, q2 = _make_member_handler("sock-2")
    h3, q3 = _make_member_handler("sock-3")

    # P1 connects: nobody else in room → no back-fill expected.
    await _connect(h1, "P1", "Alice")
    # Drain P1's queue — the only PRESENCE we'd ever expect in q1 from
    # this point is for P2 / P3 joining live (the standard broadcast).
    initial_q1_msgs = []
    while not q1.empty():
        initial_q1_msgs.append(q1.get_nowait())
    p1_backfill_presences = [
        m for m in initial_q1_msgs if getattr(m, "type", None) == "PLAYER_PRESENCE"
    ]
    assert p1_backfill_presences == [], (
        "First connection must not receive any back-fill — there were no "
        f"existing peers; got: {p1_backfill_presences}"
    )

    # P2 connects with caplog capturing the back-fill log line.
    with caplog.at_level(logging.INFO, logger="sidequest.handlers.connect"):
        await _connect(h2, "P2", "Bob")

    # P2's queue must now contain a PRESENCE{P1, connected} back-fill.
    q2_msgs = []
    while not q2.empty():
        q2_msgs.append(q2.get_nowait())
    q2_presence = [m for m in q2_msgs if getattr(m, "type", None) == "PLAYER_PRESENCE"]
    q2_presence_ids = [
        m.payload.player_id for m in q2_presence if getattr(m.payload, "state", None) == "connected"
    ]
    assert q2_presence_ids == ["P1"], (
        f"P2 must receive exactly one PRESENCE back-fill for P1; got: {q2_presence_ids}"
    )

    # The standard outbound broadcast must have hit P1's queue with
    # PRESENCE{P2, connected} — that's the existing live-join behaviour
    # we explicitly do NOT want to regress.
    q1_after_p2 = []
    while not q1.empty():
        q1_after_p2.append(q1.get_nowait())
    q1_p2_join = [
        m
        for m in q1_after_p2
        if getattr(m, "type", None) == "PLAYER_PRESENCE"
        and getattr(m.payload, "player_id", None) == "P2"
    ]
    assert q1_p2_join, (
        "P1 must still receive the live-join PRESENCE for P2 (existing "
        f"behaviour); got: {q1_after_p2}"
    )

    # P3 connects: must back-fill BOTH P1 and P2.
    await _connect(h3, "P3", "Carol")
    q3_msgs = []
    while not q3.empty():
        q3_msgs.append(q3.get_nowait())
    q3_presence_ids = [
        m.payload.player_id
        for m in q3_msgs
        if getattr(m, "type", None) == "PLAYER_PRESENCE"
        and getattr(m.payload, "state", None) == "connected"
    ]
    assert sorted(q3_presence_ids) == ["P1", "P2"], (
        f"P3 must receive PRESENCE back-fill for both P1 and P2; got: {q3_presence_ids}"
    )
    # P3 must NOT see itself in its own back-fill — the UI tracks the
    # local player separately via connectedPlayerName.
    assert "P3" not in q3_presence_ids, (
        f"Back-fill must exclude the connecting player; got: {q3_presence_ids}"
    )

    # GM-panel observability: the back-fill log line must fire.
    backfill_records = [r for r in caplog.records if "session.presence_backfill" in r.getMessage()]
    assert backfill_records, (
        "Back-fill must emit session.presence_backfill log line for "
        "GM-panel observability (CLAUDE.md OTEL mandate)"
    )
    msg_text = backfill_records[0].getMessage()
    assert "backfilled_count=1" in msg_text, (
        f"P2's back-fill log line should report backfilled_count=1; got: {msg_text}"
    )


@pytest.mark.asyncio
async def test_slug_connect_without_room_context_raises(seeded_game: Path):
    """Wiring test: slug-connect must fail loudly when attach_room_context was skipped.

    Regression test for the removed `hasattr(self, "_room_registry")` silent
    fallback. Any code path that reaches slug-connect without the WebSocket
    lifecycle having called attach_room_context() is a wiring bug — the
    handler must refuse to proceed, not silently skip room registration.
    """
    handler = WebSocketSessionHandler(
        save_dir=seeded_game,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    # Deliberately do NOT call attach_room_context.
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(event="connect", game_slug=_SLUG),
    )
    with pytest.raises(RuntimeError, match="attach_room_context"):
        await handler.handle_message(msg)


@pytest.mark.asyncio
async def test_slug_connect_backfills_seat_confirmed_for_existing_seats(tmp_path: Path, caplog):
    """Playtest 2026-05-02 [BUG-LOW]: roster shows peers as "creating character"
    forever in MP. The MultiplayerSessionStatus widget mirrors per-player seat
    state via the SEAT_CONFIRMED broadcast on every PLAYER_SEAT, but a player
    who connects AFTER existing seats are claimed never receives those frames
    — broadcasts only fire at seat-claim time. Fix: replay one SEAT_CONFIRMED
    per existing ``snapshot.player_seats`` entry on slug-connect.

    This wiring test seeds two existing seats on the saved snapshot, connects
    a third player, and asserts the bootstrap reply carries one
    SEAT_CONFIRMED frame per existing seat with matching player_id +
    character_slot. Plus the GM-panel watcher / log line.
    """
    import logging

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    slug = "2026-05-02-seat-backfill-test"

    laverne = Character(
        core=CreatureCore(name="Laverne", description="d", personality="p", inventory=Inventory()),
        char_class="Fighter",
        race="Human",
        backstory="P1 PC",
    )
    shirley = Character(
        core=CreatureCore(name="Shirley", description="d", personality="p", inventory=Inventory()),
        char_class="Rogue",
        race="Human",
        backstory="P2 PC",
    )
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Entrance")
    snap.characters = [laverne, shirley]
    snap.player_seats = {"P1": "Laverne", "P2": "Shirley"}
    _seed_pg_for_slug(slug, snap, mode=GameMode.MULTIPLAYER)

    handler = _make_handler(tmp_path, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="P3",
        payload=SessionEventPayload(event="connect", game_slug=slug, player_name="Carol"),
    )
    with caplog.at_level(logging.INFO):
        outbound = await handler.handle_message(msg)

    seat_msgs = [m for m in outbound if getattr(m, "type", None) == "SEAT_CONFIRMED"]
    out_types = [getattr(m, "type", "?") for m in outbound]
    assert len(seat_msgs) == 2, (
        f"Expected exactly 2 SEAT_CONFIRMED back-fill frames (one per existing seat); "
        f"got {len(seat_msgs)} — outbound types: {out_types}"
    )
    seat_pairs = sorted(
        (str(m.payload.player_id), str(m.payload.character_slot)) for m in seat_msgs
    )
    assert seat_pairs == [("P1", "Laverne"), ("P2", "Shirley")], (
        f"Back-fill frames must mirror snapshot.player_seats; got: {seat_pairs}"
    )

    backfill_records = [
        r for r in caplog.records if "session.seat_backfill_emitted" in r.getMessage()
    ]
    assert backfill_records, (
        "Seat back-fill must emit session.seat_backfill_emitted log line (CLAUDE.md OTEL mandate)"
    )
    text = backfill_records[0].getMessage()
    assert "count=2" in text, f"Log must report count=2; got: {text}"


@pytest.mark.asyncio
async def test_slug_connect_seat_backfill_empty_when_no_seats(seeded_game: Path):
    """Solo-style fresh seed: no seats yet → no SEAT_CONFIRMED back-fill.
    Regression guard against accidentally emitting empty/garbage frames
    on first-ever connect to an empty slug.
    """
    handler = _make_handler(seeded_game, [_CONTENT_SEARCH_PATH])
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(event="connect", game_slug=_SLUG, player_name="Alice"),
    )
    outbound = await handler.handle_message(msg)

    seat_msgs = [m for m in outbound if getattr(m, "type", None) == "SEAT_CONFIRMED"]
    assert seat_msgs == [], (
        f"Empty-seats fresh slug must not emit any SEAT_CONFIRMED back-fill; got: {seat_msgs}"
    )
