"""Unit + wire tests for player-turn author tagging (Story 45-22).

Regression evidence: Playtest 3 Felix's save logged 71 narrative_log
entries, all `author='narrator'`. Sebastien's GM panel could not
distinguish player input from narrator inference because the
player-turn append site was never wired into ``_execute_narration_turn``.

Two layers of coverage:

1. **Unit** — ``NarrativeEntry.author`` rejects blank values at
   construction (Pydantic field_validator), preventing every call
   site (current + future) from silently logging an unattributed
   entry. AC4 "fail loudly" enforced at the schema, not just one seam.

2. **Wire** — ``_execute_narration_turn`` appends a player entry
   (``author='player'``) before the narrator entry on real player
   turns, and skips the player append on the opening turn (no real
   player input). AC1, AC2, AC3 covered by the production-path
   wire test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.session import NarrativeEntry
from tests.server.conftest import _build_turn_context_for_test

# ---------------------------------------------------------------------------
# Layer 1 — Schema validator (AC4)
# ---------------------------------------------------------------------------


class TestNarrativeEntryAuthorValidator:
    """``author`` must be non-blank — the schema is the silent-fallback
    backstop for AC4."""

    def test_blank_author_rejected(self) -> None:
        with pytest.raises(ValueError, match="author cannot be blank"):
            NarrativeEntry(author="", content="x")

    def test_whitespace_only_author_rejected(self) -> None:
        with pytest.raises(ValueError, match="author cannot be blank"):
            NarrativeEntry(author="   ", content="x")

    def test_tab_only_author_rejected(self) -> None:
        with pytest.raises(ValueError, match="author cannot be blank"):
            NarrativeEntry(author="\t", content="x")

    @pytest.mark.parametrize("author", ["player", "narrator", "Felix", "system"])
    def test_legitimate_authors_accepted(self, author: str) -> None:
        entry = NarrativeEntry(author=author, content="x")
        assert entry.author == author

    def test_missing_author_now_fails_loudly(self) -> None:
        """Previously ``author`` defaulted to ``""`` — the playtest leak's
        root cause. Removing the default + field_validator makes silent
        construction impossible."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NarrativeEntry(content="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Layer 2 — Wire tests on _execute_narration_turn (AC1, AC2, AC3)
# ---------------------------------------------------------------------------


def _captured_narrative_entries(sd) -> list[NarrativeEntry]:
    """Pull every NarrativeEntry passed to ``sd.store.append_narrative``.

    The conftest ``session_fixture`` mocks the store, so reads via
    ``recent_narrative`` would return nothing. The MagicMock retains
    the calls — that's the production-path observation surface here.
    """
    return [
        call.args[0]
        for call in sd.store.append_narrative.call_args_list
        if call.args and isinstance(call.args[0], NarrativeEntry)
    ]


class TestPlayerTurnAuthorWiring:
    """Regression pin for Felix's save: a real player turn produces
    BOTH a player-author and a narrator-author entry in the
    narrative_log, in that order."""

    @pytest.mark.asyncio
    async def test_player_turn_logs_both_authors(self, session_fixture) -> None:
        sd, handler = session_fixture

        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="The torch flickers as you approach.",
                agent_duration_ms=1,
            )
        )

        mock_validator = MagicMock()
        mock_validator.submit = AsyncMock()
        mock_validator.is_running = MagicMock(return_value=True)
        handler._validator = mock_validator

        turn_context = _build_turn_context_for_test(sd)
        player_action = "I light the torch."

        await handler._execute_narration_turn(sd, player_action, turn_context)

        entries = _captured_narrative_entries(sd)
        authors = [e.author for e in entries]

        # AC3: log shows BOTH author values
        assert "player" in authors, f"player turn must produce author='player' entry; got {authors}"
        assert "narrator" in authors, (
            f"narrator turn must continue producing author='narrator'; got {authors}"
        )

        # AC1: player entry carries the raw player input
        player_entries = [e for e in entries if e.author == "player"]
        assert len(player_entries) >= 1
        assert any(e.content == player_action for e in player_entries), (
            f"player NarrativeEntry must carry the raw action text; "
            f"got contents {[e.content for e in player_entries]!r}"
        )

        # AC2: narrator entry carries the narration prose
        narrator_entries = [e for e in entries if e.author == "narrator"]
        assert any(e.content == "The torch flickers as you approach." for e in narrator_entries)

    @pytest.mark.asyncio
    async def test_player_entry_speaker_is_acting_character(
        self,
        session_fixture,
    ) -> None:
        """The player entry's ``speaker`` field carries the character
        name so dashboards can attribute the line at identity granularity
        while ``author`` stays low-cardinality."""
        sd, handler = session_fixture

        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="…",
                agent_duration_ms=1,
            )
        )

        mock_validator = MagicMock()
        mock_validator.submit = AsyncMock()
        mock_validator.is_running = MagicMock(return_value=True)
        handler._validator = mock_validator

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I look around.", turn_context)

        entries = _captured_narrative_entries(sd)
        player_entries = [e for e in entries if e.author == "player"]
        assert player_entries, "expected a player entry"
        # speaker is the resolved acting character name (or session.player_name
        # in the empty-snapshot fallback). Either way it is non-None and
        # non-empty so dashboards can group by speaker.
        speakers = [e.speaker for e in player_entries]
        assert all(s for s in speakers), f"player entry speaker must be set; got {speakers}"

    @pytest.mark.asyncio
    async def test_player_entry_round_matches_interaction(
        self,
        session_fixture,
    ) -> None:
        sd, handler = session_fixture

        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="…",
                agent_duration_ms=1,
            )
        )

        mock_validator = MagicMock()
        mock_validator.submit = AsyncMock()
        mock_validator.is_running = MagicMock(return_value=True)
        handler._validator = mock_validator

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I wait.", turn_context)

        entries = _captured_narrative_entries(sd)
        player_entries = [e for e in entries if e.author == "player"]
        narrator_entries = [e for e in entries if e.author == "narrator"]
        assert player_entries and narrator_entries
        # Both entries belong to the same round number — the player
        # input and the narrator response are one round of exchange.
        # The 45-11 round_invariant span depends on this lockstep.
        assert player_entries[-1].round == narrator_entries[-1].round


class TestOpeningTurnSkipsPlayerAppend:
    """``is_opening_turn=True`` carries a programmatic seed action,
    not real player input — appending a player entry would mis-attribute
    the chargen-confirmation seed to the player. Only the narrator
    entry should land for the opening turn."""

    @pytest.mark.asyncio
    async def test_opening_turn_logs_only_narrator(self, session_fixture) -> None:
        sd, handler = session_fixture

        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="The dome looms ahead.",
                agent_duration_ms=1,
            )
        )

        mock_validator = MagicMock()
        mock_validator.submit = AsyncMock()
        mock_validator.is_running = MagicMock(return_value=True)
        handler._validator = mock_validator

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(
            sd,
            "(opening seed)",
            turn_context,
            is_opening_turn=True,
        )

        entries = _captured_narrative_entries(sd)
        authors = [e.author for e in entries]
        assert "player" not in authors, (
            "opening turn must not log a player entry — the seed action "
            "is programmatic, not player-driven"
        )
        assert "narrator" in authors
