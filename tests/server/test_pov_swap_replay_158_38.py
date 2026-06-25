"""Story 158-38 Facet 2 (RED) — POV localization on the resume/replay path.

Follow-up to DONE 158-8 / 158-14. ``_apply_pov_swap`` runs only at LIVE emit
(``emitters.emit_event`` per-recipient fan-out). The projection cache is written
from the pre-swap projection decision (``emitters.py`` ``_cache_decision`` runs
before the swap), so the cache holds the canonical 3rd-person prose. On reconnect
(ADR-133 full-replay mirror) the replay reconstruction rebuilds messages straight
from that cache via ``_build_message_for_kind`` and never re-applies the swap —
so the resuming player reads their OWN past action in 3rd person
("Carl plants a boot...") where the live frame had read "You plant a boot..."
(pingpong 2026-06-24).

These tests drive the real ``views.backfill_last_narration_block`` reconstruction
— the tail-backfill is the production path a solo/fresh-browser resume actually
traverses when ``last_seen_seq`` already covers the tail (connect.py:1614). They
are RED until that reconstruction applies ``_apply_pov_swap`` per recipient and
stamps the lie-detector span with an ``origin='replay'`` marker (AC-2).

Harness mirrors tests/server/test_narration_pov_emission.py (3-PC MP room,
Postgres-backed event log + projection cache). Carl is emitted as the author of
his own action card (``author_player_id`` => ``project_emitter`` so Carl gets a
cache row), then we replay Carl's reconnect through the real backfill function.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import GameMode
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.session import GameSnapshot
from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData
from sidequest.server.session_room import RoomRegistry
from sidequest.server.views import backfill_last_narration_block

_GENRE = "caverns_and_claudes"
_WORLD = "sunden"
_SLUG = "pov-replay-wiring-158-38"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

_RULES_YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
"""

# Carl's own action card, authored 3rd-person (as the narrator writes + as the
# cache stores it). On Carl's LIVE frame this had read "You plant a boot...".
_CARL_CARD_TEXT = "Carl plants a boot on the moth's thorax."
_CARL_VIZ = {
    "visible_to": "all",
    "fidelity": {},
    "anchor_pc": "Carl",
    "pov_strategy": "pc_anchored",
}


# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_narration_pov_emission.py)
# ---------------------------------------------------------------------------


def _pc(name: str, pronouns: str = "he/him") -> Character:
    core = CreatureCore(
        name=name,
        description="A test subject.",
        personality="Test.",
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


@pytest.fixture
def otel_capture() -> Iterator:
    """Drain OTEL spans into an in-memory exporter (mirrors
    tests/agents/test_pov_swap_otel.py)."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _seed_game_row():
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


def _make_handler_three_pcs(tmp_path: Path) -> WebSocketSessionHandler:
    """Carl/Donut/Katia seated in a MULTIPLAYER room with a Postgres-backed
    event log + projection cache."""
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [
        _pc("Carl", pronouns="he/him"),
        _pc("Donut", pronouns="he/him"),
        _pc("Katia", pronouns="she/her"),
    ]
    handler._session_data = _SessionData.__new__(_SessionData)
    handler._session_data.snapshot = snap
    handler._session_data.player_id = "p_carl"
    handler._session_data.genre_slug = _GENRE
    handler._session_data.world_slug = _WORLD

    repo = _seed_game_row()
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
    room.connect("p_katia", socket_id="sock-katia")
    room.seat("p_carl", character_slot="Carl")
    room.seat("p_donut", character_slot="Donut")
    room.seat("p_katia", character_slot="Katia")
    handler._room = room
    return handler


def _attach_queues(room) -> dict[str, asyncio.Queue]:
    queues: dict[str, asyncio.Queue] = {}
    for pid, sock in (
        ("p_carl", "sock-carl"),
        ("p_donut", "sock-donut"),
        ("p_katia", "sock-katia"),
    ):
        q: asyncio.Queue = asyncio.Queue()
        room.attach_outbound(sock, q)
        queues[pid] = q
    return queues


def _emit_carl_card(handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live-emit Carl's own action card (author => Carl is projected + cached)."""
    from sidequest.server import views as views_module

    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})
    payload = {"text": _CARL_CARD_TEXT, "footnotes": [], "_visibility": dict(_CARL_VIZ)}
    handler._emit_event("NARRATION", payload, author_player_id="p_carl")


def _narration_texts(messages: list[object]) -> list[str]:
    """Pull the prose out of replay NARRATION messages.

    ``payload.text`` is a ``NonBlankString`` RootModel, not a bare ``str`` —
    coerce via ``.root`` (or ``str(...)``) so callers compare against plain
    strings.
    """
    texts: list[str] = []
    for m in messages:
        payload = getattr(m, "payload", None)
        text = getattr(payload, "text", None)
        if text is None:
            continue
        texts.append(str(getattr(text, "root", text)))
    return texts


def _spans_named(exporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# 1. The acting player's own PC is re-localized to 2nd person on replay
# ---------------------------------------------------------------------------


def test_resume_replay_localizes_acting_player_pc_to_second_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carl reconnects: the backfill reconstruction of his own past action card
    must read 2nd-person ('You plant a boot...'), matching the live frame — NOT
    the stored 3rd-person 'Carl plants a boot...'."""
    handler = _make_handler_three_pcs(tmp_path)
    _attach_queues(handler._room)
    _emit_carl_card(handler, monkeypatch)

    replay = backfill_last_narration_block(handler, player_id="p_carl")
    texts = _narration_texts(replay)

    assert texts, f"backfill must return Carl's NARRATION on resume; got: {replay!r}"
    joined = " || ".join(texts)
    assert "You plant a boot" in joined, (
        f"Carl's replayed card must be re-localized to 2nd-person on resume; got: {joined!r}"
    )
    assert "Carl plants a boot" not in joined, (
        f"Carl must not read his own action in 3rd-person after reconnect; got: {joined!r}"
    )


# ---------------------------------------------------------------------------
# 2. A non-anchor recipient's replay stays 3rd-person (no over-swap)
# ---------------------------------------------------------------------------


def test_resume_replay_keeps_non_anchor_pc_third_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Donut reconnects: Carl's card does not mention Donut's PC, so the
    per-recipient swap is a no-op and Donut still reads 'Carl plants a boot...'.
    Guards the fix against over-swapping someone else's card."""
    handler = _make_handler_three_pcs(tmp_path)
    _attach_queues(handler._room)
    _emit_carl_card(handler, monkeypatch)

    replay = backfill_last_narration_block(handler, player_id="p_donut")
    texts = _narration_texts(replay)

    assert texts, f"backfill must return the card to Donut on resume; got: {replay!r}"
    joined = " || ".join(texts)
    assert "Carl plants a boot" in joined, (
        f"non-anchor recipient must still read 3rd-person on replay; got: {joined!r}"
    )
    assert "You plant" not in joined, (
        f"non-anchor recipient must NOT receive the swapped prose on replay; got: {joined!r}"
    )


# ---------------------------------------------------------------------------
# 3. The replay swap is observable — emits the lie-detector span with origin
# ---------------------------------------------------------------------------


def test_resume_replay_emits_second_person_swap_span_with_replay_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """AC-2 lie-detector: the replay localization must emit
    ``narration.second_person_swap`` carrying ``origin='replay'`` so the GM
    panel can distinguish a replayed swap from a live one (and prove the replay
    path engaged rather than silently shipping stored 3rd-person prose)."""
    handler = _make_handler_three_pcs(tmp_path)
    _attach_queues(handler._room)
    _emit_carl_card(handler, monkeypatch)

    # Drop the live-emit spans; we only want the spans the replay path emits.
    otel_capture.clear()

    backfill_last_narration_block(handler, player_id="p_carl")

    swap_spans = _spans_named(otel_capture, "narration.second_person_swap")
    replay_spans = [s for s in swap_spans if (s.attributes or {}).get("origin") == "replay"]
    assert replay_spans, (
        "replay backfill must emit a narration.second_person_swap span with "
        f"origin='replay'; got span names: {[s.name for s in otel_capture.get_finished_spans()]} "
        f"with origins: {[(s.attributes or {}).get('origin') for s in swap_spans]}"
    )
    assert any((s.attributes or {}).get("swap_target_name") == "Carl" for s in replay_spans), (
        f"the replay swap span must target Carl; got: {[dict(s.attributes or {}) for s in replay_spans]}"
    )
