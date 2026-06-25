"""WIRING for the companion perception seam (Plan B Task 5).

Two layers, both per the server's 'No Source-Text Wiring Tests' rule (CLAUDE.md):

1. COMPOSITION — the production widening helper and the REAL CoreInvariantStage
   firewall compose correctly: a pet is included by membership, hireling/peer/
   stranger are excluded. (`*_through_real_firewall`)

2. PRODUCTION-PATH WIRING — `emit_event` actually INVOKES
   expand_visibility_for_companions on the live fan-out, so a bonded pet's
   projection decision is `include=True`. Deleting the call site in emitters.py
   flips the pet to exclude and fails this test — the regression guard the
   AC's "proven by a firewall wiring test" demands.
   (`test_emit_event_widens_owner_private_segment_to_bonded_pet`)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import GameMode
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.session import GameSnapshot
from sidequest.server.emitters import expand_visibility_for_companions
from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData
from sidequest.server.session_room import CompanionRelationship, RoomRegistry, SessionRoom

# ---------------------------------------------------------------------------
# Layer 1 — composition (helper + real firewall), no persistence needed
# ---------------------------------------------------------------------------


def _capture_pet_spans(monkeypatch) -> list[str]:
    # The helper resolves _watcher_publish via a function-local import from
    # session_handler, so that module's re-export is the one live patch target.
    spans: list[str] = []
    monkeypatch.setattr(
        "sidequest.server.session_handler._watcher_publish",
        lambda name, fields, **_kw: spans.append(name),
    )
    return spans


def _owner_private(kind: str) -> MessageEnvelope:
    return MessageEnvelope(
        kind=kind,
        payload_json=json.dumps(
            {"text": "Only Alice senses the trap.", "_visibility": {"visible_to": ["owner-pid"]}}
        ),
        origin_seq=7,
    )


def _included(envelope: MessageEnvelope, player_id: str) -> bool:
    # SessionGameStateView (the concrete view) — GameStateView is a Protocol and
    # cannot be instantiated. with_no_genre_rules() isolates the security firewall.
    decision = ComposedFilter.with_no_genre_rules().project(
        envelope=envelope, view=SessionGameStateView(), player_id=player_id
    )
    return decision.include


def _composition_room() -> SessionRoom:
    room = SessionRoom(slug="companion-wiring", mode=GameMode.SOLO)
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("rex-pid", "alice@home", CompanionRelationship.PET)
    room.register_companion_bond("gus-pid", "alice@home", CompanionRelationship.HIRELING)
    room.register_companion_bond("kit-pid", "alice@home", CompanionRelationship.PEER)
    return room


def test_pet_receives_owner_private_narration_through_real_firewall(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    widened = expand_visibility_for_companions(_owner_private("NARRATION_SEGMENT"), _composition_room())

    assert _included(widened, "owner-pid") is True  # the human still sees it
    assert _included(widened, "rex-pid") is True  # the PET shares the owner's view
    assert _included(widened, "gus-pid") is False  # the HIRELING is excluded
    assert _included(widened, "kit-pid") is False  # the PEER is excluded
    assert _included(widened, "stranger-pid") is False  # an unbonded seat is excluded
    assert "companion.routed_as_pet" in spans  # the firewall decision is observable


def test_pet_receives_owner_private_secret_note_through_real_firewall(monkeypatch):
    spans = _capture_pet_spans(monkeypatch)
    widened = expand_visibility_for_companions(_owner_private("SECRET_NOTE"), _composition_room())

    assert _included(widened, "rex-pid") is True  # PET shares the owner's secret note
    assert _included(widened, "gus-pid") is False  # hireling excluded
    assert _included(widened, "stranger-pid") is False
    assert "companion.routed_as_pet" in spans


# ---------------------------------------------------------------------------
# Layer 2 — production-path wiring: drive the real emit_event fan-out
# (Postgres-backed event log + projection), modeled on
# test_merged_mp_emitter_projection.py.
# ---------------------------------------------------------------------------

_GENRE = "caverns_and_claudes"
_WORLD = "sunden"
_SLUG = "companion-perception-wiring"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _pc(name: str) -> Character:
    core = CreatureCore(
        name=name, description="A test subject.", personality="Test.", inventory=Inventory()
    )
    return Character(
        core=core, backstory="A wanderer.", char_class="Fighter", race="Human", pronouns="they/them"
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


def _seed_repo():
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


def _setup_tracing() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if hasattr(current, "add_span_processor"):
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    return exporter


def _make_handler(tmp_path: Path) -> WebSocketSessionHandler:
    """Owner (p_owner, seated Carl) + a bonded PET companion (p_pet, seated
    Donut). Both connected; the pet is bonded to the owner's resolved identity."""
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [_pc("Carl"), _pc("Donut")]
    handler._session_data = _SessionData.__new__(_SessionData)
    handler._session_data.snapshot = snap
    handler._session_data.player_id = "p_owner"
    handler._session_data.genre_slug = _GENRE
    handler._session_data.world_slug = _WORLD

    repo = _seed_repo()
    handler._event_log = EventLog(repo)
    # NARRATION_SEGMENT is gated structurally in CoreInvariantStage, so an
    # empty genre ruleset is sufficient — the firewall is the surface under test.
    handler._projection_filter = ComposedFilter(
        rules=load_rules_from_yaml_str("rules: []"), pack_slug=None
    )
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    room.connect("p_owner", socket_id="sock-owner")
    room.connect("p_pet", socket_id="sock-pet")
    room.seat("p_owner", character_slot="Carl")
    room.seat("p_pet", character_slot="Donut")
    room.set_player_identity("p_owner", "alice.host")
    room.register_companion_bond("p_pet", "alice.host", CompanionRelationship.PET)
    handler._room = room
    return handler


def _attach_queues(room) -> dict[str, asyncio.Queue]:
    qs = {pid: asyncio.Queue() for pid in ("p_owner", "p_pet")}
    room.attach_outbound("sock-owner", qs["p_owner"])
    room.attach_outbound("sock-pet", qs["p_pet"])
    return qs


def test_emit_event_widens_owner_private_segment_to_bonded_pet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRODUCTION-PATH WIRING: emit_event must invoke
    expand_visibility_for_companions on the live fan-out so the bonded pet is
    INCLUDED by the firewall. Without the call site, the pet's owner-private
    NARRATION_SEGMENT decision is exclude — this asserts include, so deleting
    the wiring call fails the test."""
    from sidequest.server import session_handler as handler_module
    from sidequest.server import views as views_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION_SEGMENT", _FakeMsg)
    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    handler = _make_handler(tmp_path)
    _attach_queues(handler._room)

    exporter = _setup_tracing()
    exporter.clear()

    payload = {"text": "Only Alice senses the trap.", "_visibility": {"visible_to": ["p_owner"]}}
    handler._emit_event("NARRATION_SEGMENT", dict(payload), author_player_id="p_owner")

    decides = {
        (s.attributes or {}).get("player_id"): (s.attributes or {}).get("decision.include")
        for s in exporter.get_finished_spans()
        if s.name == "projection.filter.decide"
    }
    assert decides.get("p_pet") is True, (
        "production wiring: emit_event must widen the owner-private "
        "NARRATION_SEGMENT to the bonded pet via expand_visibility_for_companions "
        "(emitters.py); deleting that call site flips the pet to exclude. "
        f"decide spans: {decides}"
    )
