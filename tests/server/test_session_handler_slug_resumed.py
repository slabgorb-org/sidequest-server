"""Regression tests for slug-connect bootstrap messages (playtest 2026-04-23).

The slug-resumed connect branch was missing three bootstrap messages that
the legacy genre+world connect path emits:

1. CharacterBuilder initialization (no-character case).
2. First CHARACTER_CREATION scene message so the client's <CharacterCreation/>
   component has something to render — without it the UI lands on an empty
   div with no way to advance (symptom: blank div + idle server).
3. SESSION_EVENT{event:"ready"} on resume-into-playing so the client flips
   sessionPhase from "connect" → "game".

These tests verify the slug path now matches the legacy path on all three.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.game.session import GameSnapshot
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _State,
)
from sidequest.server.session_room import RoomRegistry

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database.

    The slug-connect path (ADR-115 D2) reads the authoritative snapshot +
    narration from the PG repository via ``db_pool.get_pool()`` (resolves
    SIDEQUEST_DATABASE_URL), not the SQLite save_dir store. Bind that pool to
    the migrated throwaway db so the seed helpers and the connect handler share
    one isolated database (mirrors tests/dungeon/conftest.build_pg_dungeon_repo).
    """
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _seed_pg_for_slug(
    slug: str,
    snap: GameSnapshot,
    *,
    mode: GameMode = GameMode.SOLO,
    narrations: tuple[str, ...] = (),
) -> None:
    """Seed a snapshot (+ optional prior NARRATION events) into PG (ADR-115 F1).

    ``_build_pg_repos_for_slug`` also registers the session row the connect
    handshake reads — Postgres is the sole save backend.
    """
    from sidequest.game import db_pool
    from sidequest.game.event_log import EventLog
    from sidequest.protocol.messages import NarrationPayload
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(mode),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    repo.save(snap)
    if narrations:
        event_log = EventLog(repo)
        for prose in narrations:
            event_log.append(
                kind="NARRATION",
                payload_json=NarrationPayload(text=prose, seq=0).model_dump_json(exclude={"seq"}),
            )


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


def _seed_fresh_game(tmp_path: Path, slug: str) -> None:
    """Register a fresh SOLO session in Postgres (no snapshot → chargen)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )


def _seed_resumable_game(tmp_path: Path, slug: str) -> None:
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
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [char]
    snap.character_locations["Rux"] = "Entrance"
    _seed_pg_for_slug(slug, snap)


@pytest.mark.asyncio
async def test_slug_fresh_no_character_emits_chargen_scene(tmp_path: Path) -> None:
    """Fresh slug + no character → outbound contains the opening chargen scene.

    Without this the browser lands on <div data-testid="character-creation" />
    with no scene data and the player cannot advance.
    """
    slug = "2026-04-23-fresh-chargen"
    _seed_fresh_game(tmp_path, slug)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(event="connect", game_slug=slug),
    )
    outbound = await handler.handle_message(msg)

    chargen_msgs = [
        m for m in outbound if getattr(m, "type", None) == MessageType.CHARACTER_CREATION
    ]
    assert chargen_msgs, (
        "Expected a CHARACTER_CREATION message in outbound so the client "
        "<CharacterCreation/> has a scene to render. "
        f"Got types: {[getattr(m, 'type', None) for m in outbound]}"
    )
    # First emitted scene is scene_index=0 (builder starts there after
    # with_lobby_name). We verify the scene payload carries an index.
    first_scene = chargen_msgs[0]
    scene_index = getattr(first_scene.payload, "scene_index", None)
    assert scene_index == 0, f"First chargen scene should be scene_index=0, got {scene_index}"

    # State must be Creating — builder must be bound.
    assert handler._state is _State.Creating
    assert handler.session_data is not None
    assert handler.session_data.builder is not None, (
        "builder must be constructed on the slug path when has_character=False"
    )


@pytest.mark.asyncio
async def test_slug_resume_with_character_emits_ready_event(tmp_path: Path) -> None:
    """Slug resume + existing character → SESSION_EVENT{ready, has_character:True}.

    Without this the client stays on ConnectScreen forever — sessionPhase
    never flips from "connect" to "game".
    """
    slug = "2026-04-23-resume-ready"
    _seed_resumable_game(tmp_path, slug)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(event="connect", game_slug=slug),
    )
    outbound = await handler.handle_message(msg)

    ready_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "ready"
    ]
    assert ready_msgs, (
        "Expected SESSION_EVENT{event:'ready'} on slug resume with an "
        f"existing character. Got: {[(getattr(m, 'type', None), getattr(getattr(m, 'payload', None), 'event', None)) for m in outbound]}"
    )
    ready_payload = ready_msgs[0].payload
    assert ready_payload.has_character is True
    assert ready_payload.genre == _GENRE
    assert ready_payload.world == _WORLD

    # State must be Playing.
    assert handler._state is _State.Playing


@pytest.mark.asyncio
async def test_slug_fresh_emits_chargen_bootstrap_span_event(tmp_path: Path) -> None:
    """OTEL: slug_connect.chargen_bootstrap span event fires so the GM panel
    can verify chargen actually kicked off (lie detector per CLAUDE.md).
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
        slug = "2026-04-23-fresh-chargen-otel"
        _seed_fresh_game(tmp_path, slug)
        handler = _make_handler(tmp_path)
        msg = SessionEventMessage(
            type="SESSION_EVENT",
            player_id="alice",
            payload=SessionEventPayload(event="connect", game_slug=slug),
        )
        await handler.handle_message(msg)

        mp_spans = [s for s in exporter.get_finished_spans() if s.name == "mp.slug_connect"]
        assert mp_spans, "Expected mp.slug_connect span"
        bootstrap_spans = [
            s for s in exporter.get_finished_spans() if s.name == "slug_connect.chargen_bootstrap"
        ]
        assert bootstrap_spans, (
            "Expected slug_connect.chargen_bootstrap span so the GM "
            "panel can see chargen fired. "
            f"Got span names: {[s.name for s in exporter.get_finished_spans()]}"
        )
        bs = bootstrap_spans[0]
        assert bs.attributes["player_id"] == "alice"
        assert bs.attributes["scene_index"] == 0
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Playtest 2026-04-23 Bug 1 — display-name wiring for slug-connect
# ---------------------------------------------------------------------------
#
# On the slug-connect path the UI passes the player's display name in
# ``payload.player_name`` (localStorage['sq:display-name']). The session
# must use that for the lobby name / SessionData.player_name / SESSION_EVENT
# emission — NOT the opaque player_id UUID. Without this fix, genre packs
# without a name-entry scene (mutant_wasteland, etc.) end up with the UUID
# on the character-sheet header because CharacterBuilder.with_lobby_name
# falls through as the default character name.


@pytest.mark.asyncio
async def test_slug_connect_uses_player_name_from_payload(tmp_path: Path) -> None:
    """``payload.player_name`` (display name) wires into ``sd.player_name``
    and the builder's lobby-name default — not the opaque player_id."""
    slug = "2026-04-23-display-name-wire"
    _seed_fresh_game(tmp_path, slug)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="116f74b2-ba0f-4899-9277-2933cbe6e097",  # UUID from playtest
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Slabgorb",
        ),
    )
    outbound = await handler.handle_message(msg)

    # SessionData carries the display name for character-sheet fallbacks
    # and for party_name/emitted SESSION_EVENT frames.
    sd = handler.session_data
    assert sd is not None
    assert sd.player_name == "Slabgorb", (
        f"sd.player_name must be the display name, got {sd.player_name!r}"
    )
    assert sd.player_id == "116f74b2-ba0f-4899-9277-2933cbe6e097"

    # Builder's lobby-name fallback is the display name. When a genre pack
    # has no name-entry scene, builder.build(name=None-path) uses this as
    # the character name.
    assert sd.builder is not None
    # CharacterBuilder exposes its lobby name via the internal attr set
    # in with_lobby_name(). Verify it's NOT the UUID.
    lobby_name = getattr(sd.builder, "_lobby_name", None)
    assert lobby_name == "Slabgorb", (
        f"builder lobby name must be the display name, got {lobby_name!r}"
    )

    # SESSION_EVENT{connected} carries the display name, not the UUID.
    connected_msgs = [
        m
        for m in outbound
        if getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
    ]
    assert connected_msgs, "expected SESSION_EVENT{connected} on slug-connect"
    assert connected_msgs[0].payload.player_name == "Slabgorb"


@pytest.mark.asyncio
async def test_slug_connect_chargen_complete_character_name_is_display_name(
    tmp_path: Path,
) -> None:
    """End-to-end regression for playtest 2026-04-23 Bug 1: a slug-connected
    session in mutant_wasteland (no name-entry scene) must land with
    ``character.core.name`` = display name, NOT the player UUID.

    Walks the mutant_wasteland chargen flow to confirmation and inspects
    the built character's name from the resulting CHARACTER_CREATION
    {phase=complete} + PARTY_STATUS frames.
    """
    from sidequest.protocol.messages import (
        CharacterCreationMessage,
        CharacterCreationPayload,
    )

    # mutant_wasteland has no name-entry scene — this is the worst-case
    # genre for the lobby-name fallback.
    genre = "mutant_wasteland"
    world = "flickering_reach"
    if not (_CONTENT_SEARCH_PATH / genre).is_dir():
        pytest.skip(f"{genre} content not found")

    slug = "2026-04-23-chargen-name-e2e"
    from sidequest.game import db_pool as _db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug as _bld

    _bld(_db_pool.get_pool(), slug=slug, mode=str(GameMode.SOLO), genre_slug=genre, world_slug=world)

    # Use a mock Claude client so the post-confirmation opening narration
    # doesn't try to shell out.
    from tests.server.conftest import (
        mock_claude_client_factory as _mock_claude_client_factory,
    )

    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
        claude_client_factory=_mock_claude_client_factory(),
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-test",
        out_queue=asyncio.Queue(),
    )

    player_uuid = "116f74b2-ba0f-4899-9277-2933cbe6e097"
    display_name = "Slabgorb"

    connect_msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id=player_uuid,
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name=display_name,
        ),
    )
    await handler.handle_message(connect_msg)

    # Walk to confirmation — mutant_wasteland scenes are all choice-based.
    sd = handler.session_data
    assert sd is not None and sd.builder is not None
    builder = sd.builder
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            # Shouldn't hit this on mutant_wasteland but be safe.
            payload = CharacterCreationPayload(
                phase="scene",
                choice=display_name,
            )
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id=player_uuid)
        )
        # Bail out loudly on unexpected errors.
        assert not any(getattr(m, "type", None) == "ERROR" for m in out), (
            f"error walking chargen: {out}"
        )

    # Confirm — builds the character.
    out = await handler.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id=player_uuid,
        )
    )
    complete_msgs = [
        m
        for m in out
        if getattr(m, "type", None) == "CHARACTER_CREATION"
        and getattr(getattr(m, "payload", None), "phase", None) == "complete"
    ]
    assert complete_msgs, f"expected CHARACTER_CREATION{{complete}}, got {out}"
    built_character = complete_msgs[0].payload.character
    assert built_character is not None
    # Character name must be the display name, NOT the UUID.
    char_name = built_character.get("core", {}).get("name")
    assert char_name == display_name, (
        f"character.core.name should be {display_name!r} (display name), "
        f"got {char_name!r} — slug-path is probably passing player_id "
        f"into with_lobby_name() again."
    )
    assert char_name != player_uuid, (
        "character name must not be the player UUID — UI header would "
        "render the UUID as the character name."
    )


# ---------------------------------------------------------------------------
# Playtest 2026-04-23 Bug 2 — PARTY_STATUS carries populated stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slug_chargen_complete_party_status_has_stats(
    tmp_path: Path,
) -> None:
    """PARTY_STATUS emitted at chargen completion carries a non-empty
    ``sheet.stats`` dict with int values. UI Character → Stats tab binds
    to this — a missing/empty stats dict → blank values next to labels.
    """
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import (
        CharacterCreationMessage,
        CharacterCreationPayload,
    )

    genre = "mutant_wasteland"
    world = "flickering_reach"
    if not (_CONTENT_SEARCH_PATH / genre).is_dir():
        pytest.skip(f"{genre} content not found")

    slug = "2026-04-23-party-status-stats"
    from sidequest.game import db_pool as _db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug as _bld

    _bld(_db_pool.get_pool(), slug=slug, mode=str(GameMode.SOLO), genre_slug=genre, world_slug=world)

    from tests.server.conftest import (
        mock_claude_client_factory as _mock_claude_client_factory,
    )

    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
        claude_client_factory=_mock_claude_client_factory(),
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-test",
        out_queue=asyncio.Queue(),
    )

    player_uuid = "pid-" + "0" * 8
    await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id=player_uuid,
            payload=SessionEventPayload(
                event="connect",
                game_slug=slug,
                player_name="Slabgorb",
            ),
        )
    )

    sd = handler.session_data
    assert sd is not None and sd.builder is not None
    builder = sd.builder
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Slabgorb")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id=player_uuid)
        )
        assert not any(getattr(m, "type", None) == "ERROR" for m in out), (
            f"error walking chargen: {out}"
        )

    out = await handler.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id=player_uuid,
        )
    )

    party_status_msgs = [m for m in out if getattr(m, "type", None) == MessageType.PARTY_STATUS]
    assert party_status_msgs, (
        f"expected PARTY_STATUS after chargen completion. Got: "
        f"{[getattr(m, 'type', None) for m in out]}"
    )
    ps = party_status_msgs[0]
    assert len(ps.payload.members) >= 1
    member = ps.payload.members[0]
    assert member.sheet is not None, (
        "PartyMember.sheet must be populated post-chargen — this is the "
        "source the UI binds its Character tab to."
    )
    stats = member.sheet.stats
    assert stats, (
        "sheet.stats must be a non-empty dict. Empty here → UI shows stat "
        "labels with blank values next to them."
    )
    # Every value is an int (dict[str, int] per protocol). A value of None
    # would render as empty in the UI.
    for key, value in stats.items():
        assert isinstance(value, int), (
            f"sheet.stats[{key!r}] = {value!r} (type {type(value).__name__}) "
            "— must be int for UI to render."
        )
        assert value > 0, f"sheet.stats[{key!r}] = {value}; stats should be positive post-chargen"
    # Race is present so the UI can display it on the sheet header.
    assert member.sheet.race, "sheet.race must be populated — used for sheet subtitle."
    # Inventory currency carries the pack-declared noun — mutant_wasteland
    # is "Salvage" per genre_packs/mutant_wasteland/inventory.yaml. UI
    # reads this to render "42 Salvage" instead of the legacy hardcoded
    # "42 gold" fantasy leak. Pingpong 2026-04-24 "500 gold in Space Opera".
    assert member.inventory is not None, "PartyMember.inventory must be populated post-chargen"
    assert member.inventory.currency_name == "Salvage", (
        f"expected mutant_wasteland currency_name 'Salvage'; got "
        f"{member.inventory.currency_name!r}. The server reads the noun "
        f"from inventory.yaml::currency.name on the active pack."
    )


# ---------------------------------------------------------------------------
# Rename-on-resume for pre-fix UUID saves
# (pingpong 2026-04-24 — "Resumed character shows UUID as name")
# ---------------------------------------------------------------------------


def _seed_resumable_game_with_uuid_name(tmp_path: Path, slug: str, player_id: str) -> None:
    """Seed a save whose character.core.name is the opaque player_id UUID.

    Mirrors pre-fix chargen state: CharacterBuilder committed the character
    before the with_lobby_name() rename landed, so core.name == player_id.
    """
    core = CreatureCore(
        name=player_id,  # the bug: UUID leaked into the display name
        description="A pre-fix save",
        personality="stoic",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A pre-fix fighter",
    )
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [char]
    snap.character_locations["Rux"] = "Entrance"
    _seed_pg_for_slug(slug, snap)


@pytest.mark.asyncio
async def test_slug_resume_renames_uuid_character_to_display_name(
    tmp_path: Path,
) -> None:
    """Pre-fix save: character.core.name is the player_id UUID. On resume,
    the handler must rename it to the display_name the client sent on
    connect AND persist the change so the next turn's PARTY_STATUS
    reflects the real name.
    """
    player_id = "116f74b2-ba0f-4899-9277-2933cbe6e097"
    slug = "2026-04-24-uuid-rename"
    _seed_resumable_game_with_uuid_name(tmp_path, slug, player_id)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id=player_id,
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Slabgorb",
        ),
    )
    await handler.handle_message(msg)

    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    assert sd.snapshot.characters, "expected resumed character in snapshot"
    assert sd.snapshot.characters[0].core.name == "Slabgorb", (
        "UUID-shaped core.name must be swapped for the client-provided "
        f"display_name on resume; got {sd.snapshot.characters[0].core.name!r}"
    )

    # Persisted — reload from PG (the authoritative store post-D2) and confirm
    # the rename stuck, so a subsequent reconnect doesn't re-detect the UUID.
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    reopened_repo, _d, _s = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    loaded = reopened_repo.load()
    assert loaded is not None
    assert loaded.snapshot.characters[0].core.name == "Slabgorb"


@pytest.mark.asyncio
async def test_slug_resume_leaves_real_name_untouched(tmp_path: Path) -> None:
    """Resume with a non-UUID character name (e.g. Slabgorb's actual name
    after the fix lands) must NOT rename anything, even if display_name
    differs — don't overwrite a legitimate name with every reconnect.
    """
    slug = "2026-04-24-real-name-untouched"
    _seed_resumable_game(tmp_path, slug)  # seeds core.name="Rux"
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="116f74b2-ba0f-4899-9277-2933cbe6e097",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Slabgorb",  # different from saved "Rux"
        ),
    )
    await handler.handle_message(msg)

    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd.snapshot.characters[0].core.name == "Rux", (
        "Real character name must not be overwritten on resume; only "
        "UUID-shaped names get the rename treatment."
    )


def test_rename_helper_idempotent_on_already_renamed_character(tmp_path: Path) -> None:
    """Calling the rename helper again after a successful rename is a
    no-op — guards against the helper introducing drift when a resume
    cycle fires twice (e.g. StrictMode double-mount or a reconnect).
    """
    from sidequest.server.session_handler import (
        _rename_resumed_character_if_uuid,
    )

    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="x")
    snap.characters = [
        Character(
            core=CreatureCore(
                name="Slabgorb",
                description="post-rename",
                personality="stoic",
                inventory=Inventory(),
            ),
            char_class="Fighter",
            race="Human",
            backstory="ok",
        )
    ]
    changed = _rename_resumed_character_if_uuid(
        snapshot=snap,
        display_name="Slabgorb",
        player_id="116f74b2-ba0f-4899-9277-2933cbe6e097",
    )
    assert changed is False
    assert snap.characters[0].core.name == "Slabgorb"


def _seed_resumable_game_with_narrations(tmp_path: Path, slug: str, narrations: list[str]) -> None:
    """Seed a resumable game with prior NARRATION events in the event_log.

    Used by the "empty narrative on resume" regression test to prove
    replay_msgs actually carries historical narration back to the
    reconnecting client.
    """
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
    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug=_WORLD,
        location="Entrance",
    )
    snap.characters = [char]
    _seed_pg_for_slug(slug, snap, narrations=tuple(narrations))


@pytest.mark.asyncio
async def test_slug_resume_replays_prior_narration(tmp_path: Path) -> None:
    """On resume with prior NARRATION events in the event_log, the
    outbound batch MUST include those narrations (replayed via the
    projection_cache / lazy_fill path) so the reconnecting client can
    rehydrate the narrative column.

    Pre-fix the slug-connect bootstrap returned no narration rows
    because lazy_fill wasn't invoked OR the cache read skipped them.
    Without this the UI lands on "The narrator gathers their
    thoughts..." and the player has no context for the last scene
    they were in. Pingpong 2026-04-24.
    """
    from sidequest.protocol.enums import MessageType

    slug = "2026-04-24-narration-replay"
    prior = [
        "The vault's threshold yawns open before you.",
        "Cold air rises from the stone below.",
        "You step across the threshold and descend.",
    ]
    _seed_resumable_game_with_narrations(tmp_path, slug, prior)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
            last_seen_seq=0,  # fresh reconnect — replay everything
        ),
    )
    outbound = await handler.handle_message(msg)

    narration_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.NARRATION]
    assert len(narration_msgs) == len(prior), (
        f"Expected {len(prior)} NARRATION frames on resume, got "
        f"{len(narration_msgs)}. Outbound types: "
        f"{[getattr(m, 'type', None) for m in outbound]}"
    )
    # NarrationPayload.text is a NonBlankString root model — compare
    # str(…) so the assertion is about the underlying content, not the
    # pydantic wrapper representation.
    replayed_texts = [str(m.payload.text) for m in narration_msgs]
    assert replayed_texts == prior, (
        "Replayed narration text diverges from saved event_log "
        f"payload. Saved={prior!r}; replayed={replayed_texts!r}"
    )


@pytest.mark.asyncio
async def test_slug_resume_emits_chapter_marker_for_saved_location(
    tmp_path: Path,
) -> None:
    """On resume, the server must emit CHAPTER_MARKER with the saved
    location so the UI's ``useRunningHeader`` hook populates the
    running-header chapter title. Pre-fix the header was empty on
    resume because CHAPTER_MARKER was an orphan protocol type the
    server never emitted.
    """
    from sidequest.protocol.enums import MessageType

    slug = "2026-04-24-chapter-marker-resume"
    _seed_resumable_game(tmp_path, slug)  # seeds location="Entrance"
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
        ),
    )
    outbound = await handler.handle_message(msg)

    chapter_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.CHAPTER_MARKER]
    assert chapter_msgs, (
        "Expected CHAPTER_MARKER on slug resume with a saved location. "
        f"Got message types: {[getattr(m, 'type', None) for m in outbound]}"
    )
    assert chapter_msgs[0].payload.location == "Entrance"


@pytest.mark.asyncio
async def test_slug_resume_without_saved_location_skips_chapter_marker(
    tmp_path: Path,
) -> None:
    """When the saved snapshot has no location set (brand-new session
    resumed mid-chargen), the server must NOT emit a CHAPTER_MARKER
    with an empty location — the UI's hook would clobber any prior
    title with a blank string. Follow CLAUDE.md 'no silent fallbacks'.
    """
    from sidequest.protocol.enums import MessageType

    slug = "2026-04-24-chapter-marker-skip"
    # Seed a resumable game but with empty location.
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
    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug=_WORLD,
        location="",  # no location
    )
    snap.characters = [char]
    _seed_pg_for_slug(slug, snap)

    handler = _make_handler(tmp_path)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
        ),
    )
    outbound = await handler.handle_message(msg)

    chapter_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.CHAPTER_MARKER]
    assert not chapter_msgs, (
        "CHAPTER_MARKER must be skipped when snapshot.location is empty "
        "— no silent fallback to a blank title."
    )


@pytest.mark.asyncio
async def test_slug_resume_backfills_last_narration_when_replay_is_empty(
    tmp_path: Path,
) -> None:
    """Fresh-browser resume: client's persisted ``last_seen_seq`` already
    covers the tail (nothing new happened while the tab was closed), so the
    normal replay loop emits zero narrations — leaving the narrative pane
    blank. The tail backfill must re-emit the recent NARRATION block
    regardless of ``last_seen_seq`` so the player lands with the last
    chapter on screen. Pingpong 2026-04-24 "Slug-resume shows empty
    Narrative pane on fresh browser session" + 2026-04-30 "Resume
    narration replay emits only 1 of N" (cap raised so multi-turn
    scrollback survives a refresh).
    """
    from sidequest.protocol.enums import MessageType

    slug = "2026-04-24-tail-backfill"
    prior = [
        "The corridor yawns open before you.",
        "You step across the threshold.",
        "A brass memory core blinks in the dust.",
    ]
    _seed_resumable_game_with_narrations(tmp_path, slug, prior)
    handler = _make_handler(tmp_path)

    # Simulate a fresh browser tab resuming a live session: the client's
    # persisted ``last_seen_seq`` is pinned at the most recent event the
    # previous tab observed. The seq is 1-indexed and NarrationPayload's
    # three rows landed at seq=1,2,3 so last_seen_seq=3 covers the tail.
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
            last_seen_seq=3,
        ),
    )
    outbound = await handler.handle_message(msg)

    narration_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.NARRATION]
    # Backfill cap is DEFAULT_TAIL_BACKFILL_LIMIT (5). With 3 prior
    # narrations seeded, all three should come back in order.
    assert [str(m.payload.text) for m in narration_msgs] == prior, (
        "Tail backfill must replay the recent narration window in order. "
        f"Got {[str(m.payload.text) for m in narration_msgs]!r}, "
        f"expected {prior!r}. Outbound types: "
        f"{[getattr(m, 'type', None) for m in outbound]}"
    )


@pytest.mark.asyncio
async def test_slug_resume_backfill_caps_at_default_limit(
    tmp_path: Path,
) -> None:
    """When more narrations exist than the backfill limit, the tail
    backfill returns only the most recent ``limit`` of them so we don't
    dump the entire game history on every refresh. Pingpong 2026-04-30
    "Resume narration replay emits only 1 of N" — the bug was 1, the cap
    is now ``DEFAULT_TAIL_BACKFILL_LIMIT``.
    """
    from sidequest.protocol.enums import MessageType
    from sidequest.server.views import DEFAULT_TAIL_BACKFILL_LIMIT

    slug = "2026-04-30-tail-backfill-cap"
    prior = [f"Narration line {i}" for i in range(DEFAULT_TAIL_BACKFILL_LIMIT + 3)]
    _seed_resumable_game_with_narrations(tmp_path, slug, prior)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
            last_seen_seq=len(prior),  # past the tail — forces backfill
        ),
    )
    outbound = await handler.handle_message(msg)

    narration_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.NARRATION]
    assert len(narration_msgs) == DEFAULT_TAIL_BACKFILL_LIMIT, (
        f"Expected {DEFAULT_TAIL_BACKFILL_LIMIT} narrations from the "
        f"capped tail backfill, got {len(narration_msgs)}."
    )
    expected_tail = prior[-DEFAULT_TAIL_BACKFILL_LIMIT:]
    assert [str(m.payload.text) for m in narration_msgs] == expected_tail, (
        "Tail backfill must return the most recent ``limit`` narrations "
        f"in order. Got {[str(m.payload.text) for m in narration_msgs]!r}, "
        f"expected {expected_tail!r}."
    )


@pytest.mark.asyncio
async def test_slug_resume_backfill_skips_when_normal_replay_has_narration(
    tmp_path: Path,
) -> None:
    """When the normal replay already carries narration forward, the
    tail backfill must not fire — otherwise the most recent narration
    would be duplicated in the replay batch.
    """
    from sidequest.protocol.enums import MessageType

    slug = "2026-04-24-tail-backfill-skip"
    prior = ["One", "Two", "Three"]
    _seed_resumable_game_with_narrations(tmp_path, slug, prior)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="rux-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Rux",
            last_seen_seq=0,  # replay everything
        ),
    )
    outbound = await handler.handle_message(msg)

    narration_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.NARRATION]
    texts = [str(m.payload.text) for m in narration_msgs]
    assert texts == prior, (
        f"Full replay path must not be duplicated by tail backfill. "
        f"Got {texts!r}, expected {prior!r}."
    )


def test_rename_helper_declines_when_display_name_is_also_uuid(
    tmp_path: Path,
) -> None:
    """If the client-provided display_name is itself UUID-shaped, the
    helper declines to rename — replacing one opaque id with another
    accomplishes nothing.
    """
    from sidequest.server.session_handler import (
        _rename_resumed_character_if_uuid,
    )

    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="x")
    snap.characters = [
        Character(
            core=CreatureCore(
                name="116f74b2-ba0f-4899-9277-2933cbe6e097",
                description="pre-fix",
                personality="stoic",
                inventory=Inventory(),
            ),
            char_class="Fighter",
            race="Human",
            backstory="ok",
        )
    ]
    changed = _rename_resumed_character_if_uuid(
        snapshot=snap,
        display_name="deadbeef-dead-beef-dead-beefdeadbeef",
        player_id="116f74b2-ba0f-4899-9277-2933cbe6e097",
    )
    assert changed is False
    # Leave the UUID — don't replace with another UUID.
    assert snap.characters[0].core.name == "116f74b2-ba0f-4899-9277-2933cbe6e097"
