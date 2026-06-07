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


def test_party_member_from_character_populates_advancement_after_level_up() -> None:
    """AC3 (behavioral wiring): the field EXISTING is not enough — prove the
    full chain reaches the player. Run the real ``apply_level_ups`` engine so it
    sets ``Character.last_advancement``, then build the player-facing
    ``PartyMember`` via ``party_member_from_character`` and assert the delta is
    actually copied (before/after/driver). A regression that drops the
    ``advancement=character.last_advancement`` line in views.py fails HERE — the
    reflection test above would still pass. Mirrors the
    ``test_reference_url_attach`` synthetic-_SessionData shape (fixture-driven,
    no live packs)."""
    from unittest.mock import MagicMock

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import apply_level_ups
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.views import party_member_from_character

    genre_pack = MagicMock()
    genre_pack.classes = []
    genre_pack.inventory = None
    # Epic 94: resolve_inventory traverses pack.worlds world-first; stub empty so
    # the MagicMock pack falls through to the (None) genre-tier inventory.
    genre_pack.worlds = {}
    genre_pack.rules.survivability_pool_label = None
    genre_pack.progression.wealth_tiers = []

    character = Character(
        core=CreatureCore(
            name="Rux",
            description="A stoic fighter.",
            personality="stoic",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
            xp=100_000,
            level=1,
        ),
        backstory="A wandering fighter.",
        char_class="Fighter",
        race="Human",
    )

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        turn_manager=TurnManager(interaction=1),
        characters=[character],
    )

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="Keith",
        player_id="p1",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )

    # Real engine sets character.last_advancement on the crossing.
    crossings = apply_level_ups(
        snapshot,
        ProgressionConfig(milestone_categories=["combat"], milestones_per_level=3, max_level=5),
    )
    assert crossings, "engine must produce a crossing for the seeded xp"

    member = party_member_from_character(
        MagicMock(),  # handler — only sd is used in this path
        sd,
        character,
        player_id="p1",
        player_name="Keith",
    )

    assert member.advancement is not None, (
        "party_member_from_character must copy Character.last_advancement into "
        "PartyMember.advancement — the player-facing populate (AC3)"
    )
    assert member.advancement.after == 5
    assert member.advancement.before == 1
    assert member.advancement.driver == "milestone"
    assert member.advancement.character_name == "Rux"


def test_party_member_advancement_is_none_without_a_level_up() -> None:
    """The companion negative: a character that did NOT level this turn
    surfaces ``advancement = None`` — the field is populated only on a real
    crossing, never a spurious delta on an ordinary turn."""
    from unittest.mock import MagicMock

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.session_handler import _SessionData
    from sidequest.server.views import party_member_from_character

    genre_pack = MagicMock()
    genre_pack.classes = []
    genre_pack.inventory = None
    # Epic 94: resolve_inventory traverses pack.worlds world-first; stub empty so
    # the MagicMock pack falls through to the (None) genre-tier inventory.
    genre_pack.worlds = {}
    genre_pack.rules.survivability_pool_label = None
    genre_pack.progression.wealth_tiers = []

    character = Character(
        core=CreatureCore(
            name="Rux",
            description="A stoic fighter.",
            personality="stoic",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory="A wandering fighter.",
        char_class="Fighter",
        race="Human",
    )
    # No level-up occurred → last_advancement stays at its default None.
    assert character.last_advancement is None

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        turn_manager=TurnManager(interaction=1),
        characters=[character],
    )
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="Keith",
        player_id="p1",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )

    member = party_member_from_character(
        MagicMock(), sd, character, player_id="p1", player_name="Keith"
    )
    assert member.advancement is None
