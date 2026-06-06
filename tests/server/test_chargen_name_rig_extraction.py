"""Playtest 2026-06-05 (RW-2) — server wiring for chargen name/rig extraction.

E2E through the real dispatch path (road_warrior/the_circuit): the player
answers the ``the_name`` scene with the messy repro sentence, corrects via the
hook_prompt followup, and commits. Asserts:

1. The built character's name is the EXTRACTED name (not the verbatim
   sentence) — and the followup correction actually lands (the re-prompt is
   no longer a dead input).
2. The rig-name half is honored: the vessel-tagged inventory item from the
   class loadout is renamed to the player's rig name at confirmation.
3. The extraction/rename decisions are OBSERVABLE (OTEL span events fire on
   the chargen span — the GM-panel gap called out in the playtest report).

Builder-level parsing contracts live in
tests/game/test_builder_name_extraction.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.vessel_tags import find_vessel_item
from sidequest.protocol.messages import CharacterCreationPayload, ErrorMessage
from sidequest.server.websocket_session_handler import WebSocketSessionHandler
from tests.server.test_chargen_dispatch import (
    CONTENT_ROOT,
    _connect,
    _mock_claude_client_factory,
    _send_chargen,
)

_REPRO_SENTENCE = (
    "They call me Zeppo. The rig is Duck Soup — because when she's "
    "running right, everything looks easy."
)
_REPRO_FOLLOWUP = "Road name: Zeppo. Rig name: Duck Soup."


@pytest.fixture
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process db_pool at a fresh throwaway Postgres database.

    ``seed_slug_for_test`` requires this (see its docstring) — without it the
    connect path reads/writes whatever SIDEQUEST_DATABASE_URL points at
    (the live dev database), where a stale ``test-slug`` row from prior runs
    poisons the snapshot."""
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture
def handler(tmp_path: Path, _pg_isolation: None) -> WebSocketSessionHandler:
    if not (CONTENT_ROOT / "road_warrior" / "worlds" / "the_circuit").is_dir():
        pytest.skip("road_warrior/the_circuit not available")
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )


async def _walk_to_confirmation_with_followup(
    handler: WebSocketSessionHandler,
    *,
    name_scene_text: str,
    followup_text: str,
) -> None:
    """Walk road_warrior chargen: choice scenes pick "1"; the terminal
    choice-less name scene gets ``name_scene_text``; the hook_prompt followup
    gets ``followup_text``."""
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None
    for _ in range(40):  # bounded walk — fail loud rather than spin
        if builder.is_confirmation():
            return
        if builder.is_awaiting_followup():
            out = await _send_chargen(
                handler, CharacterCreationPayload(phase="scene", choice=followup_text)
            )
        elif builder.is_in_progress():
            scene = builder.current_scene()
            if scene.choices:
                out = await _send_chargen(
                    handler, CharacterCreationPayload(phase="scene", choice="1")
                )
            elif scene.allows_freeform:
                out = await _send_chargen(
                    handler, CharacterCreationPayload(phase="scene", choice=name_scene_text)
                )
            else:
                out = await _send_chargen(handler, CharacterCreationPayload(phase="continue"))
        else:
            raise AssertionError(f"unexpected phase: {builder._phase!r}")
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(
                f"unexpected error at scene {builder.current_scene_index()}: "
                f"{out[0].payload.message}"
            )
    raise AssertionError("chargen walk did not reach Confirmation in 40 steps")


def run(coro):
    return asyncio.run(coro)


class TestNameRigExtractionWiring:
    def test_extracted_name_and_renamed_rig_land_on_snapshot(
        self, handler: WebSocketSessionHandler, otel_capture: InMemorySpanExporter
    ) -> None:
        async def body() -> None:
            await _connect(handler, genre="road_warrior", world="the_circuit")
            await _walk_to_confirmation_with_followup(
                handler,
                name_scene_text=_REPRO_SENTENCE,
                followup_text=_REPRO_FOLLOWUP,
            )

            out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
            assert out and not isinstance(out[0], ErrorMessage), (
                f"confirmation failed: {out[0].payload.message if out else 'no output'}"  # type: ignore[union-attr]
            )

            sd = handler._session_data  # type: ignore[attr-defined]
            assert len(sd.snapshot.characters) == 1
            char = sd.snapshot.characters[0]

            # 1. Extracted name, not the verbatim sentence.
            assert char.core.name == "Zeppo", (
                f"character name is {char.core.name!r} — freeform extraction "
                "did not fire (verbatim-sentence bug)"
            )

            # 2. The vessel item from the class loadout is renamed to the
            # player's rig name. Every road_warrior class kit carries
            # rig_tier_1_prospect ("Prospect Rig" template name).
            items = char.core.inventory.items
            vessel = find_vessel_item(items)
            assert vessel is not None, (
                f"no vessel-tagged item in loadout: {[i['id'] for i in items]}"
            )
            assert vessel["name"] == "Duck Soup", (
                f"vessel item still carries template name {vessel['name']!r} — "
                "the rig-name half of the_name scene was not honored"
            )

        run(body())

    def test_extraction_decisions_are_observable(
        self, handler: WebSocketSessionHandler, otel_capture: InMemorySpanExporter
    ) -> None:
        """The playtest report's OTEL gap: 'no extraction/rename span fired on
        either submit'. The extraction and the vessel rename must each emit a
        span event so the GM panel can see the decisions."""

        async def body() -> None:
            # The builder emits events on the CURRENT span (production wraps
            # dispatch in one); give the test path a recording span — the
            # established pattern (test_chargen_persist_and_play.py et al.).
            from opentelemetry import trace as otel_trace

            tracer = otel_trace.get_tracer(__name__)
            with tracer.start_as_current_span("chargen_walk"):
                await _connect(handler, genre="road_warrior", world="the_circuit")
                await _walk_to_confirmation_with_followup(
                    handler,
                    name_scene_text=_REPRO_SENTENCE,
                    followup_text=_REPRO_FOLLOWUP,
                )
                out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
                assert out and not isinstance(out[0], ErrorMessage)

            event_names = [
                e.name for s in otel_capture.get_finished_spans() for e in (s.events or [])
            ]
            assert "chargen.names_extracted" in event_names, (
                "freeform name extraction must emit chargen.names_extracted "
                f"(got events: {sorted(set(event_names))})"
            )
            assert "chargen.name_followup_correction" in event_names, (
                "the name-scene followup correction must be observable"
            )
            assert "character_creation.vessel_named" in event_names, (
                "the vessel rename must be observable"
            )

        run(body())
