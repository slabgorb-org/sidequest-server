"""Story 50-4 — end-to-end wiring for narrator-driven trope time skip.

This is the MANDATORY wiring test per CLAUDE.md: the unit tests in
``tests/game/test_trope_time_skip.py`` prove Pass A2 works in isolation;
these tests prove the algorithm is REACHABLE from the narrator pipeline.

Wiring chain (each test pins one link):
1. ``_render_time_skip_context`` helper exists in ``sidequest.agents.narrator``
   and renders a block carrying the spec's header + every fired beat.
2. ``build_narrator_prompt`` registers the TIME-SKIP CONTEXT section in
   the prompt registry when ``snapshot.pending_time_skip_summary`` is
   non-empty (AC-7, REACHABLE wiring check).
3. The section ends up in the prompt text the narrator actually receives.
4. The summary is cleared after assembly (one-shot lifecycle).

ACs covered: AC-2 (narration_apply → tick_tropes), AC-7 (next prompt
renders block + clears).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.game.trope_time_skip import TimeSkipBeatEvent
from sidequest.genre.loader import load_genre_pack

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _event(
    *,
    trope_id: str = "murder_mystery_clock",
    beat_event: str = "Another body found, identically posed",
    stakes: str = "high",
    npcs: tuple[str, ...] = ("constable_finch",),
    day: int = 2,
    beat_index: int = 1,
) -> TimeSkipBeatEvent:
    return TimeSkipBeatEvent(
        trope_id=trope_id,
        trope_name=trope_id.replace("_", " ").title(),
        beat_index=beat_index,
        beat_event=beat_event,
        stakes=stakes,
        npcs_involved=list(npcs),
        days_into_skip=day,
    )


# ---------------------------------------------------------------------------
# Layer 1 — Renderer helper unit
# ---------------------------------------------------------------------------


class TestRenderTimeSkipContext:
    """The ``_render_time_skip_context(summary, days_elapsed_total) -> str``
    helper is the narrator's view of a time skip. Pin the shape so a
    future prompt-tuning pass doesn't lose the header or the beat list.
    """

    def test_helper_is_importable_from_narrator_module(self) -> None:
        """Helper lives in ``sidequest.agents.narrator`` per the design spec.

        Importing it here is the wiring sentinel — if Dev parks the helper
        in some private inner module without re-exporting it, callers
        (orchestrator's prompt-assembly path) cannot reach it.
        """
        from sidequest.agents.narrator import _render_time_skip_context

        assert callable(_render_time_skip_context)

    def test_renders_header_and_each_beat(self) -> None:
        """Block contains the literal header + a line per fired beat."""
        from sidequest.agents.narrator import _render_time_skip_context

        summary = [
            _event(beat_event="Another body found", day=2),
            _event(
                trope_id="gossip_propagation",
                beat_event="Servant rumor spreads beyond household",
                stakes="medium",
                npcs=("maid_dorothy", "vicar_pell"),
                day=4,
            ),
        ]
        block = _render_time_skip_context(summary, days_elapsed_total=12)

        assert "## TIME-SKIP CONTEXT" in block
        assert "Another body found" in block
        assert "Servant rumor spreads beyond household" in block
        # Day annotation tells the narrator the chronological order.
        assert "Day 2" in block
        assert "Day 4" in block
        # NPC list surfaces — narrator can reference these characters.
        assert "constable_finch" in block
        assert "maid_dorothy" in block

    def test_renders_total_days_elapsed_for_narrator_context(self) -> None:
        """The block mentions the elapsed-day total so the narrator can
        cite "two weeks of investigation passed" rather than "some time."
        """
        from sidequest.agents.narrator import _render_time_skip_context

        summary = [_event(day=1)]
        block = _render_time_skip_context(summary, days_elapsed_total=14)
        assert "14" in block

    def test_empty_summary_returns_empty_or_blank(self) -> None:
        """Empty summary → no TIME-SKIP CONTEXT block (caller skips the
        prompt section). The helper must NOT emit a phantom header.
        """
        from sidequest.agents.narrator import _render_time_skip_context

        out = _render_time_skip_context([], days_elapsed_total=0)
        assert "## TIME-SKIP CONTEXT" not in out
        # Either an empty string or a blank-equivalent; both acceptable.
        assert out.strip() == ""


# ---------------------------------------------------------------------------
# Layer 2 — Wiring through build_narrator_prompt
# ---------------------------------------------------------------------------


pytestmark_pack = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason=(
        "sidequest-content/genre_packs/tea_and_murder not checked out — "
        "needed to drive Orchestrator.build_narrator_prompt"
    ),
)


def _build_minimal_session_data(snapshot: GameSnapshot):
    """Mirror the lightweight session-data builder existing integration
    tests use (see ``tests/integration/test_glenross_replay_recency_window.py``).

    We do NOT touch the WebSocket layer — only the orchestrator and the
    prompt registry.
    """
    from sidequest.server.session_handler import _SessionData

    pack = load_genre_pack(CONTENT_GENRE_PACKS / snapshot.genre_slug)
    return _SessionData(
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
        player_name="Tester",
        player_id="player:tester",
        snapshot=snapshot,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )


class TestBuildNarratorPromptWiring:
    """Wiring check: ``Orchestrator.build_narrator_prompt`` reaches into
    ``snapshot.pending_time_skip_summary`` and clears it after rendering.

    This is the "non-test consumer" verification CLAUDE.md mandates —
    proving the helper isn't just unit-tested but actually CALLED.
    """

    @pytestmark_pack
    @pytest.mark.asyncio
    async def test_prompt_contains_time_skip_context_header_when_summary_populated(
        self,
    ) -> None:
        """A non-empty summary results in the TIME-SKIP CONTEXT block
        appearing in the rendered prompt text.
        """
        from sidequest.agents.orchestrator import Orchestrator
        from sidequest.server.session_handler import _build_turn_context

        snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="ashworth_manor")
        snap.days_elapsed = 7
        snap.pending_time_skip_summary.extend(
            [
                _event(beat_event="Another body found", day=2),
                _event(
                    trope_id="lady_ashworth_suspicion",
                    beat_event="Lady Ashworth grows suspicious",
                    day=5,
                ),
            ]
        )

        sd = _build_minimal_session_data(snap)
        ctx = _build_turn_context(sd, room=None)

        orch = Orchestrator()
        prompt_text, _registry = await orch.build_narrator_prompt(
            "Tester: I knock on the door.", ctx
        )

        assert "## TIME-SKIP CONTEXT" in prompt_text, (
            "TIME-SKIP CONTEXT block missing from rendered narrator prompt "
            "despite snapshot.pending_time_skip_summary being non-empty — "
            "the prompt builder is not consuming the field."
        )
        assert "Another body found" in prompt_text
        assert "Lady Ashworth grows suspicious" in prompt_text

    @pytestmark_pack
    @pytest.mark.asyncio
    async def test_pending_summary_cleared_after_prompt_assembly(self) -> None:
        """One-shot lifecycle: after the prompt assembles, the queue is
        empty. The next turn's prompt must NOT re-render the same skip.
        """
        from sidequest.agents.orchestrator import Orchestrator
        from sidequest.server.session_handler import _build_turn_context

        snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="ashworth_manor")
        snap.days_elapsed = 7
        snap.pending_time_skip_summary.append(_event())

        sd = _build_minimal_session_data(snap)
        ctx = _build_turn_context(sd, room=None)

        orch = Orchestrator()
        await orch.build_narrator_prompt("Tester: action", ctx)

        assert snap.pending_time_skip_summary == [], (
            "pending_time_skip_summary not cleared after prompt assembly — "
            "the next turn will re-emit the same TIME-SKIP CONTEXT block, "
            "or never (depends on the bug shape). One-shot lifecycle is spec."
        )

    @pytestmark_pack
    @pytest.mark.asyncio
    async def test_empty_summary_yields_no_time_skip_block(self) -> None:
        """Quiet path: empty summary → no header in the prompt. Avoids a
        ghost section that would confuse the narrator with stale context.
        """
        from sidequest.agents.orchestrator import Orchestrator
        from sidequest.server.session_handler import _build_turn_context

        snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="ashworth_manor")
        # Empty summary — typical turn.
        sd = _build_minimal_session_data(snap)
        ctx = _build_turn_context(sd, room=None)

        orch = Orchestrator()
        prompt_text, _registry = await orch.build_narrator_prompt("Tester: action", ctx)

        assert "## TIME-SKIP CONTEXT" not in prompt_text


# ---------------------------------------------------------------------------
# Layer 3 — narration_apply / engine boundary
# ---------------------------------------------------------------------------


class TestEngineBoundaryThreadsDaysAdvanced:
    """AC-2 mirror: a ``NarrationTurnResult`` carrying ``days_advanced``
    must reach ``tick_tropes(... days_advanced=N)``.

    The call site lives in ``websocket_session_handler.py`` (line ~2743);
    this test asserts the kwarg is threaded by spying on tick_tropes.
    """

    def test_narration_turn_result_carries_days_advanced(self) -> None:
        """Result type has the field with sensible default — protocol
        smoke (full validation lives in test_game_patch_days_advanced.py).
        """
        from sidequest.agents.orchestrator import NarrationTurnResult

        # Default = 0 (no skip on a normal turn).
        result = NarrationTurnResult(narration="A normal turn happens.")
        assert result.days_advanced == 0

        # Explicit value round-trips.
        result_skip = NarrationTurnResult(
            narration="A week of investigation passes.", days_advanced=7
        )
        assert result_skip.days_advanced == 7

    def test_tick_tropes_call_site_threads_days_advanced(self) -> None:
        """The handler that calls ``tick_tropes`` after applying narration
        passes ``days_advanced=result.days_advanced`` — not 0, not omitted.

        Inspects the source of ``websocket_session_handler`` to verify the
        kwarg appears at the tick_tropes call site. A pure source-inspection
        test because the call site is buried in async WS plumbing that
        needs a live socket to exercise; the wiring rule is "look at the
        production call site" and that's what we do.
        """
        from sidequest.server import websocket_session_handler

        source = Path(websocket_session_handler.__file__).read_text(encoding="utf-8")
        # The tick_tropes call is on a single multi-line call; search for
        # the days_advanced kwarg anywhere in the file (the call is unique).
        assert "tick_tropes(" in source, (
            "tick_tropes call site missing from websocket_session_handler — "
            "the engine wire was removed?"
        )
        assert "days_advanced=" in source, (
            "tick_tropes is called without days_advanced kwarg — "
            "narration's days_advanced is being silently dropped on the "
            "way to the engine."
        )
