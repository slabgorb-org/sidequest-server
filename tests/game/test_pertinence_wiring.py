"""Story 84-1 (WI-1) — production wiring of the unified pertinence scorer (RED).

server CLAUDE.md "Every Test Suite Needs a Wiring Test" + "No Source-Text Wiring
Tests": drive a REAL player action through ``WebSocketSessionHandler`` and assert
on BEHAVIOR + the ``retrieval.universal`` span — never on a source grep. This
proves the new §A1 scorer is reachable on the live turn-build path:

    WebSocketSessionHandler.handle_message(PlayerAction)
      → _retrieve_entities_for_turn
        → server/dispatch/universal_retrieval.retrieve_for_turn
          → game/retrieval_orchestration.retrieve_turn_context   ← scorer plugs in here

The drama-gate is the load-bearing observable wiring assertion: when the player
NAMES a scene-present NPC, the unified scorer must skip the daemon ``embed`` —
the §D4 code always embedded, so a skip on the live path is unambiguous proof the
new mechanism (not the old floor/fill) is what ran. The fake daemon records every
``embed`` call; we assert it was never called for the named action.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

_RETRIEVAL_SPAN_NAME = "retrieval.universal"


class _RecordingDaemon:
    """Records ``embed`` calls so the wiring test can prove the production
    drama-gate skipped the embed on a named/present action."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        return {"embedding": [1.0, 0.0, 0.0], "model": "fake", "latency_ms": 1}

    def calls_for(self, needle: str) -> list[str]:
        """Embed calls whose query text contains ``needle`` — lets the wiring
        assertion target the named action's retrieval turn specifically, ignoring
        unrelated retrieval turns (e.g. the MP arrival-grounding narration)."""
        return [c for c in self.calls if needle in c]


class TestUnifiedScorerProductionWiring:
    # Heavy end-to-end wiring test: full chargen walk + narration turn through
    # the real pack (~25-30s). Override the global ``--timeout=30`` so the
    # thread-method timeout doesn't fire mid-event-loop and crash the xdist
    # worker. Real-logic correctness is unaffected.
    @pytest.mark.timeout(120)
    def test_named_present_action_skips_embed_on_live_turn(
        self,
        otel_capture: Any,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sidequest.protocol.messages import (
            CharacterCreationMessage,
            CharacterCreationPayload,
            ErrorMessage,
            PlayerActionMessage,
            PlayerActionPayload,
            SessionEventMessage,
            SessionEventPayload,
        )
        from sidequest.server.session_handler import WebSocketSessionHandler
        from tests.server.conftest import (
            attach_default_room_context,
            make_mock_claude_client,
            seed_slug_for_test,
        )

        content_root = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
        if not (content_root / "caverns_and_claudes").is_dir():
            pytest.skip("content pack not found")

        # The orchestration constructs its own DaemonClient — patch it at the
        # production reach site so the recording stand-in is what the live path uses.
        fake = _RecordingDaemon()
        monkeypatch.setattr(
            "sidequest.game.retrieval_orchestration.DaemonClient",
            lambda: fake,
        )

        mock_claude = make_mock_claude_client()
        handler = WebSocketSessionHandler(
            claude_client_factory=lambda: mock_claude,
            genre_pack_search_paths=[content_root],
            save_dir=tmp_path,
        )

        async def body() -> None:
            slug = seed_slug_for_test(
                handler._save_dir,  # type: ignore[attr-defined]
                genre="caverns_and_claudes",
                world="grimvault",
            )
            attach_default_room_context(handler)
            await handler.handle_message(
                SessionEventMessage(
                    payload=SessionEventPayload(
                        event="connect", player_name="WiringTester", game_slug=slug
                    ),
                    player_id="",
                )
            )
            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd is not None and sd.builder is not None
            builder = sd.builder
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
            await handler.handle_message(
                CharacterCreationMessage(
                    payload=CharacterCreationPayload(phase="confirmation"),
                    player_id="pid",
                )
            )
            if sd.embed_task is not None:
                await sd.embed_task
                sd.embed_task = None

            # Seed a scene-present, recently-seen NPC named "Borin" into the live
            # snapshot so naming it produces strong structured signals.
            from sidequest.game.creature_core import CreatureCore
            from sidequest.game.session import Npc

            current_turn = sd.snapshot.turn_manager.interaction
            sd.snapshot.npcs.append(
                Npc(
                    core=CreatureCore(
                        name="Borin",
                        description="Borin the dock-warden.",
                        personality="gruff",
                    ),
                    last_seen_turn=current_turn,
                )
            )

            # Act: NAME the present NPC. Strong mention + here → drama-gate skips embed.
            result = await handler.handle_message(
                PlayerActionMessage(
                    payload=PlayerActionPayload(action="I attack Borin", round=0),
                    player_id="pid",
                )
            )
            assert result, "player action must produce outbound messages"

            # (1) The unified retrieval ran on the live turn (span fired).
            fired = [s for s in otel_capture.get_finished_spans() if s.name == _RETRIEVAL_SPAN_NAME]
            assert fired, (
                "the unified scorer must be wired into the live turn-build path "
                f"and emit the {_RETRIEVAL_SPAN_NAME!r} span"
            )

            # (2) The production drama-gate SKIPPED the embed for the named/present
            #     action — unambiguous proof the §A1 scorer (not §D4 always-embed)
            #     is what ran on the live path. Target the "Borin" retrieval turn
            #     specifically so an unrelated arrival-grounding embed cannot mask
            #     the assertion.
            assert fake.calls_for("Borin") == [], (
                "naming a scene-present NPC must skip the daemon embed on the live "
                f"turn (drama-gate, §A1) — the daemon embedded the action anyway: "
                f"{fake.calls_for('Borin')!r}"
            )

            # (3) The span carries the new embed-skipped signal so the GM panel
            #     (WI-6) can show WHY the cosine pass was bypassed.
            span = fired[-1]
            attrs = dict(span.attributes or {})
            assert attrs.get("retrieval.embed_skipped") is True, (
                "the retrieval.universal span must record retrieval.embed_skipped=True "
                "when the drama-gate bypassed the cosine pass"
            )

            if sd.embed_task is not None:
                await sd.embed_task

        asyncio.run(body())
