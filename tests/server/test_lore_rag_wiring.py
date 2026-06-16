"""Wiring test for the full lore RAG pipeline — Story 37-33 round-trip #4.

Proves the end-to-end production path is actually connected, not just
that the individual pieces work in isolation. The existing
:mod:`tests.game.test_lore_embedding` suite hammers
:func:`retrieve_lore_context` and :func:`embed_pending_fragments` via
``_FakeClient`` directly — which means you could delete the body of
:meth:`WebSocketSessionHandler._retrieve_lore_for_turn` and the whole
suite would still pass. This test dispatches a real
:class:`PlayerActionMessage` through the handler with a patched
:class:`DaemonClient` and asserts:

1. ``retrieve_lore_context`` was invoked with the player's action text
   (pre-turn RAG retrieval fires against the real session handler
   pipeline, not an isolated helper call).
2. The retrieved ``<lore>`` block reaches :class:`TurnContext` and lands
   in the narrator prompt (verified via the mock Claude client's
   captured ``send_with_session`` prompt).
3. ``_dispatch_embed_worker`` was invoked after the turn and stored a
   lifecycle-tracked task on ``_SessionData.embed_task``.
4. ``cleanup()`` cancels the in-flight embed task before closing the
   SQLite store — the round-trip #4 HIGH fix.
5. Double-dispatch is skipped while a previous worker is still running.

CLAUDE.md "Every Test Suite Needs a Wiring Test" rule. The round-3
reviewer flagged this as a HIGH blocker: three independent subagents
(preflight, test-analyzer, rule-checker) confirmed that no test
exercised the ``WebSocketSessionHandler → _retrieve_lore_for_turn →
retrieve_lore_context → TurnContext → narrator prompt`` chain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sidequest.game.lore_store import LoreCategory, LoreFragment, LoreSource
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
    make_mock_claude_client,
    mock_claude_client_factory,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fake DaemonClient that tracks calls for both retrieval and worker paths
# ---------------------------------------------------------------------------


class _WiringFakeClient:
    """Stand-in for :class:`DaemonClient` that records every embed call.

    Returns a deterministic 2-d embedding per text so
    :func:`retrieve_lore_context` can actually produce a non-empty hit
    list against the seeded fragment.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        embedding: list[float] | None = None,
    ) -> None:
        self._available = available
        self._embedding = embedding or [1.0, 0.0]
        self.socket_path = Path("/tmp/fake-sock")
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        return {
            "embedding": list(self._embedding),
            "model": "fake-model",
            "latency_ms": 1,
        }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_claude() -> Any:
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("content pack not found")
    return make_mock_claude_client()


@pytest.fixture
def handler(tmp_path: Path, mock_claude: Any) -> WebSocketSessionHandler:
    return WebSocketSessionHandler(
        claude_client_factory=lambda: mock_claude,
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )


async def _connect_and_confirm(handler: WebSocketSessionHandler) -> None:
    """Walk the shortest chargen path to Playing state."""
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(handler._save_dir, genre="caverns_and_claudes", world="grimvault")
    attach_default_room_context(handler)
    await handler.handle_message(
        SessionEventMessage(
            payload=SessionEventPayload(
                event="connect",
                player_name="WiringTester",
                game_slug=slug,
            ),
            player_id="",
        )
    )
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    assert sd.builder is not None

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


def _seed_embedded_fragment(handler: WebSocketSessionHandler) -> LoreFragment:
    """Inject (1) an already-embedded fragment the retriever can hit AND
    (2) a pending fragment the post-turn worker can process.

    Confirmation already seeded chargen fragments, but the opening turn
    in ``_connect_and_confirm`` embeds them all via the fake — leaving
    an empty pending queue that would short-circuit
    ``_dispatch_embed_worker`` before it could prove the wiring. Adding
    a second, still-pending fragment keeps the dispatch path honest.

    The retrievable fragment's id sorts lexicographically before the
    chargen-seeded ``lore_char_creation_*`` fragments so the top-k
    result is deterministic: they all score cosine=1.0 against the
    fake's constant ``[1.0, 0.0]`` embedding, and ties break on id.
    """
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    frag = LoreFragment.new(
        id="aaa_wiring_seed_fragment",
        category=LoreCategory.History,
        content="The wiring test seeded this knowledge into the lore store.",
        source=LoreSource.GenrePack,
    )
    frag.embedding = [1.0, 0.0]
    frag.embedding_pending = False
    sd.lore_store.add(frag)
    # Pending fragment for the dispatch-worker path.
    pending = LoreFragment.new(
        id="aaa_wiring_pending_fragment",
        category=LoreCategory.Event,
        content="A fragment awaiting background embedding on the next turn.",
        source=LoreSource.GenrePack,
    )
    sd.lore_store.add(pending)
    return frag


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------


class TestLoreRagWiring:
    def test_player_action_drives_full_lore_pipeline(
        self,
        handler: WebSocketSessionHandler,
        mock_claude: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: PLAYER_ACTION → retrieve → TurnContext → prompt → worker dispatch."""

        fake_client = _WiringFakeClient()
        # Patch the DaemonClient symbol imported into lore_embedding so
        # the retrieve + worker helpers both see our fake. This is the
        # exact site production code reaches via ``if client is None:
        # client = DaemonClient()`` in both helpers.
        monkeypatch.setattr("sidequest.game.lore_embedding.DaemonClient", lambda: fake_client)

        async def body() -> None:
            await _connect_and_confirm(handler)

            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd is not None
            # Chargen confirmation runs an opening narration turn that
            # already dispatched a worker. Drain it so we can observe the
            # *action*-induced dispatch cleanly below.
            opening_task = sd.embed_task
            if opening_task is not None:
                await opening_task
            sd.embed_task = None

            seeded = _seed_embedded_fragment(handler)
            # Pre-populate fake.calls so we can distinguish embed calls
            # made by the action's retrieval from any earlier chargen-
            # opening embeds.
            pre_action_calls = list(fake_client.calls)

            action_text = "I look around the dusty cavern"
            result = await handler.handle_message(
                PlayerActionMessage(
                    payload=PlayerActionPayload(action=action_text),
                    player_id="pid",
                )
            )
            # Handler must not crash on the RAG path; at least one
            # outbound message (narration) must surface.
            assert result, "player action must produce outbound messages"

            # (1) The retrieve path called embed() with the action text.
            new_calls = fake_client.calls[len(pre_action_calls) :]
            assert action_text in new_calls, (
                f"retrieve_lore_context must embed the player action; "
                f"post-action calls seen: {new_calls}"
            )

            # (2) The narrator prompt received the <lore> block with
            # the seeded fragment id. This proves retrieve_lore_context's
            # return value flowed through TurnContext → build_narrator_prompt.
            # Post-ADR-098: narrator path uses send_stateless; system_prompt
            # and user_message ride on kwargs.
            send_calls = mock_claude.send_stateless.call_args_list
            assert send_calls, "narrator must be invoked at least once"

            def _prompt_of(call: Any) -> str:
                # Combine system_prompt + user_message — the lore block
                # is registered as a dynamic section so it lands in user.
                return " ".join(
                    str(call.kwargs.get(k, "")) for k in ("system_prompt", "user_message")
                )

            all_prompts = [_prompt_of(c) for c in send_calls]
            lore_prompts = [p for p in all_prompts if "<lore>" in p]
            assert lore_prompts, (
                "at least one narrator prompt on this turn must carry the "
                f"<lore> block (saw {len(all_prompts)} prompts)"
            )
            assert any(seeded.id in p for p in lore_prompts), (
                f"seeded fragment id {seeded.id!r} must appear in the "
                "formatted <lore> block (proves query → retrieval → "
                "format → prompt is wired)"
            )

            # (3) Post-turn dispatch stored a lifecycle-tracked task.
            assert sd.embed_task is not None, (
                "_dispatch_embed_worker must store the task on _SessionData "
                "so cleanup() can cancel it"
            )

            # Let the background worker run so we don't leak a pending task.
            await sd.embed_task

        asyncio.run(body())

    def test_cleanup_cancels_in_flight_embed_task(
        self,
        handler: WebSocketSessionHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH round-trip #4 fix: ``cleanup()`` must cancel the embed task
        BEFORE closing the SQLite store, otherwise the worker writes to an
        orphaned in-memory lore_store after the session disconnects.
        """

        async def body() -> None:
            await _connect_and_confirm(handler)
            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd is not None
            # Drain any worker dispatched by the chargen opening turn
            # before arming the slow fake — we want a deterministic
            # blocker.wait() path, not a race.
            opening_task = sd.embed_task
            if opening_task is not None:
                await opening_task
            sd.embed_task = None

            # Arm the slow fake AFTER draining. Its first embed call
            # awaits forever so the worker is guaranteed mid-flight
            # when cleanup fires.
            blocker = asyncio.Event()

            class _SlowFake(_WiringFakeClient):
                async def embed(self, text: str) -> dict[str, Any]:
                    self.calls.append(text)
                    await blocker.wait()
                    raise AssertionError("embed should have been cancelled")

            monkeypatch.setattr(
                "sidequest.game.lore_embedding.DaemonClient",
                lambda: _SlowFake(),
            )

            # Force a pending fragment so dispatch actually spawns a worker.
            sd.lore_store.add(
                LoreFragment.new(
                    id="cleanup_pending",
                    category=LoreCategory.History,
                    content="unembedded content awaiting worker",
                    source=LoreSource.GenrePack,
                )
            )
            handler._dispatch_embed_worker(sd)  # type: ignore[attr-defined]
            task = sd.embed_task
            assert task is not None
            # Yield so the worker reaches ``await blocker.wait()``.
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()

            await handler.cleanup()

            # Post-cleanup: the task is finished (cancelled or swallowed)
            # and never reached the ``raise AssertionError`` path.
            assert task.done()

        asyncio.run(body())

    def test_double_dispatch_skipped_while_worker_running(
        self,
        handler: WebSocketSessionHandler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH round-trip #4 guard: two rapid turns must not spawn two
        concurrent workers that race at the ``await client.embed()`` yield
        point and double-increment the retry counter.
        """

        async def body() -> None:
            await _connect_and_confirm(handler)
            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd is not None
            # Drain the chargen-opening worker.
            opening_task = sd.embed_task
            if opening_task is not None:
                await opening_task
            sd.embed_task = None

            blocker = asyncio.Event()

            class _SlowFake(_WiringFakeClient):
                async def embed(self, text: str) -> dict[str, Any]:
                    self.calls.append(text)
                    await blocker.wait()
                    return {
                        "embedding": [1.0, 0.0],
                        "model": "fake",
                        "latency_ms": 1,
                    }

            monkeypatch.setattr(
                "sidequest.game.lore_embedding.DaemonClient",
                lambda: _SlowFake(),
            )

            sd.lore_store.add(
                LoreFragment.new(
                    id="double_dispatch_pending",
                    category=LoreCategory.History,
                    content="still pending",
                    source=LoreSource.GenrePack,
                )
            )

            # Capture every _watcher_publish call the dispatcher fires so
            # we can assert the double-dispatch-skipped event is actually
            # emitted (round-5 fix: the skip path gets OTEL visibility).
            watcher_calls: list[tuple[str, dict[str, Any]]] = []

            def _capture_publish(kind: str, payload: dict[str, Any], **kwargs: Any) -> None:
                watcher_calls.append((kind, dict(payload)))

            # Phase 3 of session_handler decomposition moved the dispatch
            # body into sidequest.server.dispatch.lore_embed; the skip-path
            # _watcher_publish now resolves through that module's namespace.
            monkeypatch.setattr(
                "sidequest.server.dispatch.lore_embed._watcher_publish",
                _capture_publish,
            )

            handler._dispatch_embed_worker(sd)  # type: ignore[attr-defined]
            first_task = sd.embed_task
            assert first_task is not None
            # Cooperative yield: give the event loop enough ticks to let
            # the worker enter ``await blocker.wait()`` before we re-
            # dispatch. 5 ticks exceeds the worker's current yield depth
            # from task creation to the first blocking await; bump if a
            # future worker refactor adds intermediate awaits.
            for _ in range(5):
                await asyncio.sleep(0)
            assert not first_task.done()

            # Second dispatch while the first is still running — must be
            # skipped so ``sd.embed_task`` keeps pointing at the live task.
            handler._dispatch_embed_worker(sd)  # type: ignore[attr-defined]
            assert sd.embed_task is first_task

            # The skip path must emit a watcher event so the GM panel
            # state_transition stream sees the backpressure signal.
            skip_events = [
                p
                for kind, p in watcher_calls
                if kind == "state_transition"
                and p.get("op") == "skipped"
                and p.get("reason") == "worker_still_running"
            ]
            assert skip_events, (
                "double-dispatch skip must publish a state_transition watcher "
                f"event; saw: {watcher_calls}"
            )

            # Unblock and drain.
            blocker.set()
            await first_task

        asyncio.run(body())


# ---------------------------------------------------------------------------
# Regression guard — make sure the factory wiring is used so a refactor
# that reverts to a module-level singleton trips this test.
# ---------------------------------------------------------------------------


def test_mock_claude_client_factory_is_reachable() -> None:
    """Sanity check the conftest factory resolves; protects against an
    import-path rename breaking every wiring test above with a confusing
    ``ModuleNotFoundError`` at collection time instead of a clear failure.
    """
    factory = mock_claude_client_factory()
    client = factory()
    assert client is not None
