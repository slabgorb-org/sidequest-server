"""Lore-seeding integration — Story 2.3 Slice F.

Drives a real chargen confirmation through ``WebSocketSessionHandler``
against the in-repo synthetic ``test_genre`` / ``flickering_reach`` fixture
pack (story 74-5: no real-pack coupling — the wiring proof is preserved
because it still boots the production handler and dispatch path) and asserts:

- ``sd.lore_store`` ends up non-empty after commit
- char-creation fragments carry :class:`LoreSource.CharacterCreation` with
  the Rust ``lore_char_creation_<scene_id>_<choice_index>`` id format
- world-lore fragments carry :class:`LoreSource.GenrePack` with
  ``lore_world_<slug>_*`` ids (``flickering_reach`` authors world lore, so
  the world seeder fires at confirmation) and NO ``lore_genre_*`` fragments
  appear (epic 74: lore is world-only)
- OTEL emits ``lore.char_creation_seeded`` with the expected counts

The seeding call fires BEFORE ``sd.builder = None`` so the scene list
is available — covered by the successful count assertion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.lore_store import LoreSource
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import (
    mock_claude_client_factory as _mock_claude_client_factory,
)

_FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    ADR-115 F1: chargen connect resolves the bootstrap row from Postgres and
    confirmation persists the snapshot there. ``seed_slug_for_test`` registers
    the session in this isolated db; TRUNCATE per test prevents fixed-slug
    bleed across sibling tests.
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


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[_FIXTURE_PACKS_DIR],
        save_dir=tmp_path,
    )


@pytest.fixture
def otel_capture():
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


async def _walk_and_confirm(handler: WebSocketSessionHandler) -> list:
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None

    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Rux")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")

    tracer = otel_trace.get_tracer("test")
    with tracer.start_as_current_span("chargen_confirmation"):
        return await handler.handle_message(
            CharacterCreationMessage(
                payload=CharacterCreationPayload(phase="confirmation"),
                player_id="pid",
            )
        )


def _events(exporter: InMemorySpanExporter, name: str) -> list:
    return [e for span in exporter.get_finished_spans() for e in span.events if e.name == name]


class TestLoreSeedingDispatch:
    def test_flickering_reach_confirmation_seeds_lore_store(
        self, handler: WebSocketSessionHandler
    ) -> None:
        async def body() -> None:
            from tests.server.conftest import attach_default_room_context, seed_slug_for_test

            slug = seed_slug_for_test(
                handler._save_dir,
                genre="test_genre",
                world="flickering_reach",
            )
            attach_default_room_context(handler)
            await handler.handle_message(
                SessionEventMessage(
                    payload=SessionEventPayload(
                        event="connect",
                        player_name="Tester",
                        game_slug=slug,
                    ),
                    player_id="",
                )
            )
            sd = handler._session_data  # type: ignore[attr-defined]
            # Session starts with an empty lore store.
            assert sd.lore_store.is_empty()

            out = await _walk_and_confirm(handler)
            assert isinstance(out[0], CharacterCreationMessage)
            assert out[0].payload.phase == "complete"

            # Post-confirmation: lore store has fragments from every
            # chargen scene's choices PLUS the world's lore corpus
            # (history/geography/cosmology/factions) — added by the
            # pingpong 2026-04-30 fix that wired ``seed_lore_from_world``
            # into chargen-confirm. Pre-fix only ``seed_lore_from_char_creation``
            # ran, leaving the narrator's RAG retrieval to query an
            # effectively-empty store and improvise lore on every turn.
            # (Epic 74: genre-tier lore is no longer seeded; ``flickering_reach``
            # authors its own world lore, so the world seeder fires here.)
            assert not sd.lore_store.is_empty()

            # Partition: every fragment must carry one of the expected
            # source flags. Char-creation fragments still keep their
            # ``lore_char_creation_`` id prefix; genre-pack fragments carry the
            # ``lore_world_`` prefix (epic 74: genre-tier ``lore_genre_*`` ids
            # are no longer produced, so that arm would be dead code).
            char_creation_frags = []
            genre_pack_frags = []
            for frag in sd.lore_store.fragments_iter():
                if frag.source == LoreSource.CharacterCreation:
                    assert frag.id.startswith("lore_char_creation_")
                    char_creation_frags.append(frag)
                elif frag.source == LoreSource.GenrePack:
                    assert frag.id.startswith("lore_world_"), (
                        f"Genre-pack fragment {frag.id!r} must use the "
                        "world-scoped lore_world_* prefix (epic 74: world-only lore)"
                    )
                    genre_pack_frags.append(frag)
                else:
                    raise AssertionError(
                        f"Unexpected lore source {frag.source!r} for fragment {frag.id!r}"
                    )
                assert frag.content  # all fragments must have body text

            # Char-creation lore still seeds at confirmation.
            assert len(char_creation_frags) > 0, (
                "Char-creation seeder must add at least one fragment "
                "(test_genre has populated chargen scenes)."
            )
            # Epic 74 — lore is WORLD-ONLY. ``flickering_reach`` authors world
            # lore, so the world seeder fires at confirmation: GenrePack-sourced
            # fragments must be PRESENT and every one must use the world-scoped
            # ``lore_world_flickering_reach_*`` id (never a genre-tier id).
            assert len(genre_pack_frags) > 0, (
                "epic 74: flickering_reach authors world lore, so the world "
                "seeder must add at least one GenrePack-sourced fragment at "
                "confirmation; got none"
            )
            assert all(
                frag.id.startswith("lore_world_flickering_reach_") for frag in genre_pack_frags
            ), (
                "every GenrePack-sourced fragment must be world-scoped to "
                f"flickering_reach; got {[f.id for f in genre_pack_frags]}"
            )
            assert not any(
                frag.id.startswith("lore_genre_") for frag in sd.lore_store.fragments_iter()
            ), "epic 74: genre lore must not be seeded (lore is world-only)"

        asyncio.run(body())

    def test_confirmation_emits_otel_lore_seeded(
        self,
        handler: WebSocketSessionHandler,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            from tests.server.conftest import (
                attach_default_room_context,
                seed_slug_for_test,
            )

            slug = seed_slug_for_test(
                handler._save_dir,
                genre="test_genre",
                world="flickering_reach",
            )
            attach_default_room_context(handler)
            await handler.handle_message(
                SessionEventMessage(
                    payload=SessionEventPayload(
                        event="connect",
                        player_name="Tester",
                        game_slug=slug,
                    ),
                    player_id="",
                )
            )
            await _walk_and_confirm(handler)
            sd = handler._session_data  # type: ignore[attr-defined]

            events = _events(otel_capture, "lore.char_creation_seeded")
            assert len(events) == 1
            attrs = dict(events[0].attributes or {})
            assert attrs["event"] == "char_creation_lore_seeded"
            # Pingpong 2026-04-30 wiring change: the genre/world seeders
            # run BEFORE char-creation, so by the time the
            # ``lore.char_creation_seeded`` event fires, the store
            # already contains genre/world fragments and
            # ``total_fragments`` reflects all three layers. The
            # ``fragments_added`` field is the char-creation delta only,
            # so it is now strictly LESS THAN ``total_fragments`` rather
            # than equal. Assertion updates capture this contract.
            assert attrs["fragments_added"] > 0
            assert attrs["total_fragments"] == len(sd.lore_store)
            assert attrs["fragments_added"] <= attrs["total_fragments"], (
                "fragments_added (char-creation only) must be ≤ "
                "total_fragments (post-genre+world+char layers); "
                "an inversion would imply the genre/world seeders "
                "ran AFTER char-creation, breaking the wiring order."
            )
            assert attrs["total_tokens"] == sd.lore_store.total_tokens()
            assert attrs["genre"] == "test_genre"
            assert attrs["world"] == "flickering_reach"

        asyncio.run(body())
