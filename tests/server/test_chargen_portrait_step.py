"""Epic 66 — pick_portrait step interposed before the chargen confirmation.

Wiring tests for the portrait-picker chargen step:

1. ``_next_message`` at the confirmation boundary emits a one-time
   ``input_type="pick_portrait"`` scene frame (gated by
   ``sd.portrait_step_shown``) instead of the confirmation summary.
2. ``phase="portrait_confirm"`` (dispatched through
   ``handlers/character_creation.py`` → ``_chargen_portrait_confirm``)
   stores ``selected_portrait_ref`` on the session and renders the
   confirmation summary.
3. The real confirmation commit (``_chargen_confirmation``) copies
   ``sd.selected_portrait_ref`` onto the built Character reachable from
   the snapshot — the MANDATORY wiring test (production path, not a
   reimplementation).
4. The portrait confirm fires the ``chargen.portrait_select`` OTEL span
   (the GM panel is the lie detector).

Harness mirrors ``test_45_6_chargen_archetype_gate.py``: WS-handler-driven
against the loaded caverns_and_claudes pack with per-test PG isolation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database.

    Same rationale as test_45_6_chargen_archetype_gate.py: the slug-connect
    path resolves the authoritative snapshot from Postgres; TRUNCATE the
    per-test state so a fixed-slug row from a sibling test can't bleed in.
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
def handler_factory(tmp_path: Path):
    """Build a WebSocketSessionHandler bound to caverns_and_claudes content."""
    content_root = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    if not (content_root / "caverns_and_claudes").is_dir():
        pytest.skip("caverns_and_claudes content not found")

    def make() -> WebSocketSessionHandler:
        return WebSocketSessionHandler(
            claude_client_factory=_mock_claude_client_factory(),
            genre_pack_search_paths=[content_root],
            save_dir=tmp_path,
        )

    return make


# OTEL capture: tests use the shared ``otel_capture`` fixture from
# tests/server/conftest.py (global-provider exporter with the 45-36
# stale-processor reset) — no local copy.


# ---------------------------------------------------------------------------
# WS-driven helpers
# ---------------------------------------------------------------------------


async def _connect(handler: WebSocketSessionHandler) -> None:
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(handler._save_dir, genre="caverns_and_claudes", world="grimvault")
    attach_default_room_context(handler)
    payload = SessionEventPayload(
        event="connect",
        player_name="Pumblestone",
        game_slug=slug,
    )
    out = await handler.handle_message(SessionEventMessage(payload=payload, player_id=""))
    assert isinstance(out[0], SessionEventMessage)


async def _send(
    handler: WebSocketSessionHandler,
    payload: CharacterCreationPayload,
) -> list:
    return await handler.handle_message(CharacterCreationMessage(payload=payload, player_id="pid"))


async def _walk_to_confirmation(handler: WebSocketSessionHandler) -> list:
    """Walk chargen scenes until the builder reaches Confirmation.

    Returns the OUTPUT FRAMES of the last step — the frame emitted at the
    transition into the confirmation boundary (where the portrait step
    interposes).
    """
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None, "connect must construct a chargen builder"

    out: list = []
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Pumblestone")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await _send(handler, payload)
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")
    return out


def _last_chargen_payload(out: list) -> CharacterCreationPayload:
    msgs = [m for m in out if isinstance(m, CharacterCreationMessage)]
    assert msgs, f"no CharacterCreationMessage in {[type(m).__name__ for m in out]}"
    return msgs[-1].payload


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. The portrait step interposes at the confirmation boundary
# ---------------------------------------------------------------------------


class TestPortraitStepInterposes:
    def test_confirmation_boundary_emits_pick_portrait_scene_once(self, handler_factory) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd.portrait_step_shown is False

            out = await _walk_to_confirmation(handler)
            payload = _last_chargen_payload(out)
            assert payload.phase == "scene"
            assert payload.input_type == "pick_portrait"
            assert payload.portraits_available is not None
            assert sd.portrait_step_shown is True

        run(body())


# ---------------------------------------------------------------------------
# 2. portrait_confirm stores the ref and renders the confirmation summary
# ---------------------------------------------------------------------------


class TestPortraitConfirm:
    def test_portrait_confirm_stores_ref_and_returns_summary(self, handler_factory) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)
            sd = handler._session_data  # type: ignore[attr-defined]

            out = await _send(
                handler,
                CharacterCreationPayload(
                    phase="portrait_confirm", selected_portrait_ref="picker_a"
                ),
            )
            assert sd.selected_portrait_ref == "picker_a"
            payload = _last_chargen_payload(out)
            assert payload.phase == "confirmation", (
                "portrait_confirm must advance to the confirmation summary, "
                f"got phase={payload.phase!r} input_type={payload.input_type!r}"
            )
            assert payload.input_type != "pick_portrait"

        run(body())

    def test_portrait_confirm_with_no_ref_records_skip(self, handler_factory) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)
            sd = handler._session_data  # type: ignore[attr-defined]

            out = await _send(handler, CharacterCreationPayload(phase="portrait_confirm"))
            assert sd.selected_portrait_ref is None
            assert _last_chargen_payload(out).phase == "confirmation"

        run(body())


# ---------------------------------------------------------------------------
# 3. MANDATORY wiring: the real confirmation commit applies portrait_ref
# ---------------------------------------------------------------------------


class TestPortraitRefAppliedAtCommit:
    def test_confirmation_commit_copies_ref_onto_built_character(self, handler_factory) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)
            sd = handler._session_data  # type: ignore[attr-defined]

            await _send(
                handler,
                CharacterCreationPayload(
                    phase="portrait_confirm", selected_portrait_ref="picker_a"
                ),
            )
            out = await _send(handler, CharacterCreationPayload(phase="confirmation"))
            assert not (out and isinstance(out[0], ErrorMessage)), (
                f"confirmation failed: {out[0].payload.message if out else out}"
            )
            chars = sd.snapshot.characters
            assert chars, "confirmation commit must append the built character"
            assert chars[0].portrait_ref == "picker_a"

        run(body())


# ---------------------------------------------------------------------------
# 4. OTEL — chargen.portrait_select fires on portrait_confirm
# ---------------------------------------------------------------------------


class TestPortraitSelectSpan:
    def test_portrait_confirm_fires_portrait_select_span(
        self, handler_factory, otel_capture: InMemorySpanExporter
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            await _send(
                handler,
                CharacterCreationPayload(
                    phase="portrait_confirm", selected_portrait_ref="picker_a"
                ),
            )
            spans = _spans_named(otel_capture, "chargen.portrait_select")
            assert spans, (
                "portrait_confirm must fire chargen.portrait_select — the GM "
                "panel is the lie detector"
            )
            attrs = spans[-1].attributes or {}
            assert attrs.get("selected_portrait_ref") == "picker_a"
            assert attrs.get("skipped") is False
            assert attrs.get("genre") == "caverns_and_claudes"
            assert attrs.get("world") == "grimvault"
            assert attrs.get("player_id") == "pid"

        run(body())
