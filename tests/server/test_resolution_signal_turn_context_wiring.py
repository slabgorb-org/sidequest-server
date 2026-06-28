"""Resolution-signal bridge into the narrator turn (Story 158-48, ADR-143).

RED premise (root cause, documented at ``orchestrator.py`` TurnContext
``pending_resolution_signal``): the ``[ENCOUNTER RESOLVED]`` zone is DORMANT in
production. ``snapshot.pending_resolution_signal`` IS stamped on an encounter
resolution (``dispatch/dice.py``'s ``_emit_player_beat_resolution_close`` for a
player_victory, ``narration_apply.py`` for dial/yield closes), but the construction
site — ``_build_turn_context`` in ``session_helpers.py`` — never copies it into
``TurnContext.pending_resolution_signal``. The only code that ever threaded it was
the module-level ``run_narration_turn`` wrapper deleted in story 49-5; the 49-5
follow-up (re-thread it, and clear it after consumption) was never done.

The painful symptom is the WWN combat KILL turn (sq-playtest 2026-06-27,
beneath_sunden, session 16570 — the resolution-turn twin of #1086): the player's
DICE_THROW resolves the encounter (``in_combat`` legitimately flips False the
instant it resolves), the signal is stamped on the snapshot, but it never reaches
the prompt. So on the victory turn the narrator gets NEITHER the de-nativized
"the throw already resolved; just narrate it; do NOT call dice/beat tools"
directive NOR a suppressed start-a-confrontation menu — instead the "AVAILABLE
ENCOUNTER TYPES" menu fires (it is gated on ``pending_resolution_signal is None``,
which is always true today), and the narrator flails its SDK tool loop past
``max_turns=8`` (AnthropicSdkLoopExceeded). The victory NARRATION is therefore
never generated or persisted, and the client falls back to the opening card
(ADR-133 downstream).

This module pins the DICE-path seam (``_build_turn_context``, the site the
DiceThrowHandler builds the victory-turn context through). The sibling refresh
seam (free-nav narrator-beat resolution) is pinned in
``tests/agents/test_wwn_combat_denativization.py``.

**Wiring-test discipline (server CLAUDE.md "No Source-Text Wiring Tests").** The
proof is BEHAVIORAL — drive the real ``_build_turn_context`` and observe (a) the
threaded ``TurnContext.pending_resolution_signal`` object, (b) the GM-panel
watcher event, and (c) the rendered narrator prompt via real
``build_narrator_prompt`` — never a grep of handler source. Mirrors
``tests/server/test_pacing_hint_turn_context_wiring.py`` (story 81-3), the direct
precedent for reviving a declared-and-consumed-but-never-populated TurnContext
field.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.agents.orchestrator import Orchestrator
from sidequest.game.resolution_signal import ResolutionSignal
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
from tests.agents.fakes.fake_anthropic_sdk_client import FakeAnthropicSdkClient

# The de-nativized resolution directive the [ENCOUNTER RESOLVED] zone renders
# (narrator.py build_encounter_context resolution short-circuit). These two
# substrings ARE the "throw already resolved; narrate the outcome; do NOT call
# dice/beat tools" directive the kill turn must receive.
_RESOLVED_DIRECTIVE = "Do NOT emit beat_selections"
_RESOLVED_ZONE_MARKER = "[ENCOUNTER RESOLVED]"
# The start-a-confrontation menu that MUST be suppressed once a resolution is
# pending (orchestrator.py:2190 — the menu that told the narrator to START a
# fresh fight on the victory turn and drove the loop to max_turns).
_START_MENU_MARKER = "AVAILABLE ENCOUNTER TYPES"
# The GM-panel watcher op (twin of cold_seat_context_refreshed, #1086) the
# resolution-turn bridge must surface so the lie detector can confirm the
# victory turn was handed the resolution zone.
_RESOLUTION_OP = "resolution_context_refreshed"


@pytest.fixture(scope="module")
def _loader() -> GenreLoader:
    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS)


def _make_sd(loader: GenreLoader, *, signal: ResolutionSignal | None):
    """A minimal real ``_SessionData`` for caverns_and_claudes (WWN) with an
    optional ``pending_resolution_signal`` stamped on the snapshot — the exact
    post-resolution snapshot shape the DiceThrowHandler builds the victory-turn
    context from (encounter husk reaped, signal stamped).
    """
    from sidequest.server.session_handler import _SessionData

    try:
        pack = loader.load("caverns_and_claudes")
    except Exception:  # pragma: no cover - environment guard
        pytest.skip("caverns_and_claudes pack not loadable in this checkout")
    # caverns_and_claudes is the WWN genre (the story's genre); a NON-procedural
    # world (sunken_keep) is used so _project_current_region returns cleanly and
    # the bridge is isolated from dungeon projection — same rationale as #1086's
    # "test_nondungeon". The [ENCOUNTER RESOLVED] zone is genre-agnostic, so the
    # directive renders identically to the beneath_sunden kill turn.
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Curly"] = "Main Hall"
    snap.player_seats["player:Curly"] = "Curly"
    # The resolution turn: the encounter has resolved and been reaped, so there
    # is NO live encounter on the snapshot — only the one-shot signal carries the
    # "narrate the close" payload into this single turn.
    snap.pending_resolution_signal = signal
    repo = MagicMock()
    repo.recent_narrative.return_value = []
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="Curly",
        player_id="player:Curly",
        snapshot=snap,
        repository=repo,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.game_slug = "2026-06-27-caverns_and_claudes_sunken_keep-1"
    return sd


def _victory_signal() -> ResolutionSignal:
    return ResolutionSignal(
        encounter_type="combat",
        outcome="player_victory",
        final_player_metric=0,
        final_opponent_metric=0,
    )


# ---------------------------------------------------------------------------
# AC1/AC2 precondition — _build_turn_context threads the snapshot signal
# ---------------------------------------------------------------------------


def test_build_turn_context_threads_resolution_signal_from_snapshot(_loader) -> None:
    """The bridge: ``_build_turn_context`` must copy
    ``snapshot.pending_resolution_signal`` into
    ``TurnContext.pending_resolution_signal``.

    RED on current develop: the construction site omits it, so the field is None
    on the victory turn and the [ENCOUNTER RESOLVED] zone stays dormant — the
    documented root cause of the 158-48 kill-turn crash."""
    from sidequest.server.session_handler import _build_turn_context

    sig = _victory_signal()
    sd = _make_sd(_loader, signal=sig)

    ctx = _build_turn_context(sd)

    assert ctx.pending_resolution_signal is not None, (
        "_build_turn_context must thread snapshot.pending_resolution_signal into "
        "TurnContext (story 158-48 / 49-5 follow-up) — it is None on develop, so "
        "the [ENCOUNTER RESOLVED] zone is dormant and the WN kill turn crashes"
    )
    assert ctx.pending_resolution_signal.outcome == "player_victory"
    assert ctx.pending_resolution_signal.encounter_type == "combat"


def test_no_snapshot_signal_leaves_context_signal_none(_loader) -> None:
    """AC4 (no false positive): a turn with NO stamped resolution leaves
    ``TurnContext.pending_resolution_signal`` None, so the resolution zone never
    fires on a non-resolution turn. Guards against the bridge inventing a
    resolution every turn (which would re-fire the close forever)."""
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, signal=None)

    ctx = _build_turn_context(sd)

    assert ctx.pending_resolution_signal is None, (
        "with no snapshot resolution signal the context field must stay None — a "
        "non-resolution turn must not render the [ENCOUNTER RESOLVED] zone"
    )


# ---------------------------------------------------------------------------
# AC3 — the resolution-turn bridge surfaces a GM-panel watcher event
# ---------------------------------------------------------------------------


def test_build_turn_context_emits_resolution_turn_watcher_event(
    _loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OTEL lie-detector (CLAUDE.md OTEL principle), twin of #1086's
    ``cold_seat_context_refreshed``: threading the signal on the victory turn
    must publish a ``state_transition`` op=resolution_context_refreshed watcher
    event (component="encounter") so the GM panel can confirm the resolution turn
    was handed the zone. A turn with no resolution publishes none (no false
    positive)."""
    from sidequest.server.session_handler import _build_turn_context

    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr("sidequest.telemetry.watcher_hub.publish_event", _spy)

    # No resolution: no event.
    _build_turn_context(_make_sd(_loader, signal=None))
    assert not [e for e in events if e["fields"].get("op") == _RESOLUTION_OP], (
        "a non-resolution turn must not emit resolution_context_refreshed"
    )

    # The victory turn: exactly one event surfaces the resolution to the panel.
    _build_turn_context(_make_sd(_loader, signal=_victory_signal()))
    resolved = [e for e in events if e["fields"].get("op") == _RESOLUTION_OP]
    assert len(resolved) == 1, (
        "the resolution-turn bridge must surface exactly one GM-panel watcher "
        f"event (twin of cold_seat_context_refreshed); got ops="
        f"{[e['fields'].get('op') for e in events]}"
    )
    assert resolved[0]["component"] == "encounter"
    assert resolved[0]["fields"].get("outcome") == "player_victory"


# ---------------------------------------------------------------------------
# AC1/AC3 — end-to-end: real bridge -> real build_narrator_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_resolution_signal_fires_denativized_zone_and_drops_start_menu(
    _loader,
) -> None:
    """The canonical wiring test (CLAUDE.md "Every Test Suite Needs a Wiring
    Test"): a stamped victory signal flows through the real
    ``_build_turn_context`` into ``Orchestrator.build_narrator_prompt`` and lands
    as the de-nativized [ENCOUNTER RESOLVED] directive WHILE the
    start-a-confrontation menu is suppressed.

    RED on current develop end to end: the bridge leaves the field None, so the
    [ENCOUNTER RESOLVED] zone never renders AND the start menu DOES (it is gated
    on ``pending_resolution_signal is None``) — the exact prompt that ground the
    SDK loop to a crash on the victory turn."""
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, signal=_victory_signal())
    ctx = _build_turn_context(sd)
    assert ctx.pending_resolution_signal is not None, "bridge precondition for the wiring test"
    # Precondition discriminator: the pack offers confrontations, so the start
    # menu WOULD render but for the pending resolution (proves the suppression is
    # the signal's doing, not an empty menu).
    assert ctx.available_confrontations, (
        "fixture sanity: caverns_and_claudes must offer confrontations so the "
        "start-menu suppression is a real discriminator"
    )

    orch = Orchestrator(client=FakeAnthropicSdkClient(responses=[]))
    prompt_text, _registry = await orch.build_narrator_prompt(
        "I wrench my axe free of the Pale Thing and let it fall.", ctx
    )

    assert _RESOLVED_ZONE_MARKER in prompt_text and _RESOLVED_DIRECTIVE in prompt_text, (
        "the victory turn must carry the de-nativized [ENCOUNTER RESOLVED] "
        "directive (throw already resolved; narrate the outcome; do NOT emit "
        "beat_selections) — absent on develop (signal never threaded)"
    )
    assert _START_MENU_MARKER not in prompt_text, (
        "the start-a-confrontation menu must be suppressed on the resolution turn "
        "(it told the narrator to START a fresh fight and drove the max_turns "
        "crash) — it renders on develop because pending_resolution_signal is None"
    )


# ---------------------------------------------------------------------------
# AC4 — one-shot lifecycle: the signal is CLEARED after the orchestrator consumes
# it, so the resolution zone fires on the resolution turn only and a later
# non-resolution turn does not re-render the close (ResolutionSignal docstring:
# "reads this slot on the next turn and clears it"; the 49-5 follow-up).
# Drives the real ``_execute_narration_turn`` with a fake orchestrator (mirrors
# ``tests/server/test_turn_record_wiring.py``).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_signal_cleared_after_consuming_turn(session_fixture) -> None:
    """After a narration turn consumes the signal, the session handler clears
    ``snapshot.pending_resolution_signal`` so the NEXT turn does not re-thread the
    stale signal and re-narrate the close. Without the clear the [ENCOUNTER
    RESOLVED] zone fires every turn forever (violates AC4)."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from tests.server.conftest import _build_turn_context_for_test

    sd, handler = session_fixture
    handler._validator = None
    # The session_fixture pack is a MagicMock; pin effective_bestiary to a real
    # empty 2-tuple so the per-turn monster_manual injection step does not crash
    # on an unpackable auto-mock (same real-defaults pattern the fixture already
    # applies to progression / drama_thresholds / rules).
    sd.genre_pack.effective_bestiary = MagicMock(return_value=(None, "genre"))
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The Pale Thing folds into the black water. Silence.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )
    sd.snapshot.pending_resolution_signal = _victory_signal()
    turn_context = _build_turn_context_for_test(sd)

    await handler._execute_narration_turn(sd, "[BEAT_RESOLVED] the kill", turn_context)

    assert sd.snapshot.pending_resolution_signal is None, (
        "the one-shot resolution signal must be cleared from the snapshot after the "
        "orchestrator consumes it — a stale signal re-fires the [ENCOUNTER RESOLVED] "
        "zone on every subsequent turn"
    )


@pytest.mark.asyncio
async def test_resolution_signal_cleared_on_degraded_turn(session_fixture) -> None:
    """The clear runs even when the narrator degrades (AnthropicSdkLoopExceeded):
    a degraded turn must not leave the signal armed for the next action."""
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkLoopExceeded
    from tests.server.conftest import _build_turn_context_for_test

    sd, handler = session_fixture
    handler._validator = None
    sd.genre_pack.effective_bestiary = MagicMock(return_value=(None, "genre"))
    sd.orchestrator.run_narration_turn = AsyncMock(
        side_effect=AnthropicSdkLoopExceeded("Reached maximum number of turns (8)")
    )
    sd.snapshot.pending_resolution_signal = _victory_signal()
    turn_context = _build_turn_context_for_test(sd)

    # Degrades gracefully (ADR-006 / 158-41) — returns an error frame, does not raise.
    await handler._execute_narration_turn(sd, "[BEAT_RESOLVED] the kill", turn_context)

    assert sd.snapshot.pending_resolution_signal is None, (
        "the one-shot resolution signal must be cleared even on the degraded path — "
        "a degraded turn must not leave it armed for the next action"
    )
