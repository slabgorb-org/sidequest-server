"""Wiring + player-facing surface for milestone → level-up (ADR-021 track 1).

Story 82-6. Two things the unit/engine tests can't prove:

1. **AC4 — the engine actually runs inside a real turn.** ``apply_level_ups``
   being unit-callable means nothing if no production path calls it (CLAUDE.md
   "Verify Wiring, Not Just Existence"). This drives the real
   ``_execute_narration_turn`` and asserts a level-up crossing fires its OTEL
   event *through the turn* — refactor-stable, NOT a source-text grep
   (CLAUDE.md "No Source-Text Wiring Tests").
2. **AC3 — the advancement delta is legible on a player-facing surface.** The
   GM/OTEL emit is the lie-detector (dev-facing); the player needs the delta
   too. Recommended surface: a field on ``PartyMember`` mirroring how the
   track-3 wealth label rides ``PartyMember`` (``views.party_member_from_character``
   → ``resolve_wealth_tier``). Reflection tripwire, the blessed exception.

RED: no engine call in the turn, no player-facing field — both fail on
current ``develop``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.protocol.models import PartyMember
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests.server.conftest import _build_turn_context_for_test

LEVEL_UP_FIELD = "progression.level_up"


async def _subscribe_capture(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]
    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    monkeypatch.setattr(spans_module, "tracer", lambda: provider.get_tracer(label))
    return captured


@pytest.mark.asyncio
async def test_level_up_fires_inside_the_real_narration_turn(
    session_handler_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a real turn for a character seeded past the level-up ceiling and
    assert the engine engaged *from the production turn path*: the character's
    level rose and a ``progression.level_up`` watcher event was published.

    Fails on develop because nothing in ``_execute_narration_turn`` drives the
    milestone → level-up engine after ``award_turn_xp``."""
    captured = await _subscribe_capture(monkeypatch, "test-levelup-turn-wiring")

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._validator = None

    # caverns_and_claudes doesn't author progression; inject a real config so
    # the engine has thresholds to cross. Huge xp → caps at max_level under any
    # xp→milestone conversion.
    monkeypatch.setattr(
        sd.genre_pack,
        "progression",
        ProgressionConfig(
            milestone_categories=["combat"],
            milestones_per_level=3,
            max_level=5,
        ),
    )
    sd.snapshot.characters[0].core.xp = 100_000
    sd.snapshot.characters[0].core.level = 1

    sd.orchestrator.run_narration_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=NarrationTurnResult(
            narration="You vanquish the last goblin.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I finish the fight.", turn_context)
    await asyncio.sleep(0)

    assert sd.snapshot.characters[0].core.level == 5, (
        "engine must level the character up to the cap during the turn"
    )

    deadline = asyncio.get_event_loop().time() + 1.0
    found = None
    while asyncio.get_event_loop().time() < deadline and found is None:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("field") == LEVEL_UP_FIELD
            ):
                found = evt
                break
        await asyncio.sleep(0.01)

    assert found is not None, (
        "a progression.level_up state_transition must fire from the real turn; "
        f"captured fields: {[e.get('fields', {}).get('field') for e in captured]}"
    )
    assert found["component"] == "progression"
    assert found["fields"]["after"] == 5


def test_party_member_exposes_advancement_delta_field() -> None:
    """AC3 (player-facing surface): the player's party projection must carry an
    advancement delta so the level change is *legible to the player*, not just
    emitted to the GM panel. Recommended surface — a ``PartyMember`` field,
    mirroring the track-3 wealth label. Reflection tripwire (CLAUDE.md blesses
    runtime type checks as the non-source-text exception)."""
    assert "advancement" in PartyMember.model_fields, (
        "PartyMember must surface an 'advancement' delta (before/after/driver) "
        "so the player sees the level-up, not a silent stat bump (AC3)"
    )
