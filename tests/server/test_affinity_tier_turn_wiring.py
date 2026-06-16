"""Wiring + player-facing surface for affinity tier promotion (ADR-021 track 2).

Story 82-7. Three things the unit/engine tests can't prove:

1. **The engine actually runs inside a real turn.** ``apply_affinity_tier_ups``
   being unit-callable means nothing if no production path calls it (CLAUDE.md
   "Verify Wiring, Not Just Existence"). This drives the real
   ``_execute_narration_turn`` and asserts an affinity crossing fires its OTEL
   event *through the turn* — refactor-stable, NOT a source-text grep
   (CLAUDE.md "No Source-Text Wiring Tests"). Mirrors the track-1 wiring at
   ``test_levelup_turn_wiring.py``, where ``apply_affinity_tier_ups`` runs in
   the turn pipeline alongside ``apply_level_ups`` after ``award_turn_xp``.
2. **The advancement delta is legible on a player-facing surface.** The GM/OTEL
   emit is the lie-detector (dev-facing); the player needs the delta too. The
   surface is a list field on ``PartyMember`` (``affinity_advancements``) — a
   list because a character can advance several affinities in one turn —
   mirroring how the track-1 ``advancement`` delta rides ``PartyMember``.
3. **The transient list is not a shared mutable default** (lang-review #2): two
   fresh characters must not alias the same ``last_affinity_tier_ups`` list.

RED: no engine call in the turn, no player-facing field, no transient list —
all fail on current ``develop``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.character import AffinityState, Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.progression import Affinity, ProgressionConfig
from sidequest.protocol.models import PartyMember
from sidequest.server.dispatch.encounter_lifecycle import apply_affinity_tier_ups
from sidequest.server.session_handler import _SessionData
from sidequest.server.views import party_member_from_character
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests.server.conftest import _build_turn_context_for_test

AFFINITY_TIER_FIELD = "progression.affinity_tier_up"


def _progression_with_fire() -> ProgressionConfig:
    return ProgressionConfig(
        affinities=[Affinity(name="fire", description="x", tier_thresholds=[10, 25, 50])]
    )


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
async def test_affinity_tier_up_fires_inside_the_real_narration_turn(
    session_handler_factory,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a real turn for a character whose affinity progress is past the top
    threshold and assert the engine engaged *from the production turn path*: the
    affinity tier rose and a ``progression.affinity_tier_up`` watcher event was
    published.

    Fails on develop because nothing in ``_execute_narration_turn`` drives the
    affinity tier-promotion engine."""
    captured = await _subscribe_capture(monkeypatch, "test-affinity-tier-turn-wiring")

    sd, handler = session_handler_factory(genre="elemental_harmony")
    handler._validator = None

    # Inject a real config with an authored fire ladder and seed the character's
    # fire affinity past the top threshold so the crossing is deterministic.
    monkeypatch.setattr(sd.genre_pack, "progression", _progression_with_fire())
    sd.snapshot.characters[0].affinities = [
        AffinityState(affinity_id="fire", tier=0, progress=100.0)
    ]

    sd.orchestrator.run_narration_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=NarrationTurnResult(
            narration="Flame answers your call.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I channel the fire.", turn_context)
    await asyncio.sleep(0)

    assert sd.snapshot.characters[0].affinities[0].tier == 3, (
        "engine must promote the affinity to the top tier during the turn"
    )

    deadline = asyncio.get_event_loop().time() + 1.0
    found = None
    while asyncio.get_event_loop().time() < deadline and found is None:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("field") == AFFINITY_TIER_FIELD
            ):
                found = evt
                break
        await asyncio.sleep(0.01)

    assert found is not None, (
        "a progression.affinity_tier_up state_transition must fire from the real turn; "
        f"captured fields: {[e.get('fields', {}).get('field') for e in captured]}"
    )
    assert found["component"] == "progression"
    assert found["fields"]["affinity_id"] == "fire"
    assert found["fields"]["after"] == 3


def test_party_member_exposes_affinity_advancements_field() -> None:
    """Player-facing surface (reflection tripwire — CLAUDE.md blesses runtime
    type checks as the non-source-text exception): the player's party projection
    must carry an affinity-advancements list so a tier change is *legible to the
    player*, not just emitted to the GM panel."""
    assert "affinity_advancements" in PartyMember.model_fields, (
        "PartyMember must surface an 'affinity_advancements' list "
        "(affinity_id/before/after/driver) so the player sees the promotion"
    )


def _synthetic_sd(character: Character) -> _SessionData:
    """A fixture-driven ``_SessionData`` (no live packs), mirroring the
    ``test_levelup_turn_wiring`` synthetic shape."""
    genre_pack = MagicMock()
    genre_pack.classes = []
    genre_pack.inventory = None
    # Epic 94: resolve_inventory traverses pack.worlds world-first; stub empty so
    # the MagicMock pack falls through to the (None) genre-tier inventory.
    genre_pack.worlds = {}
    genre_pack.rules.survivability_pool_label = None
    genre_pack.progression.wealth_tiers = []

    snapshot = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="cinder_reach",
        turn_manager=TurnManager(interaction=1),
        characters=[character],
    )
    return _SessionData(
        genre_slug="elemental_harmony",
        world_slug="cinder_reach",
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


def _mage(name: str, *, progress: float, tier: int = 0) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="A flame-touched adept.",
            personality="intense",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory="A wandering mage.",
        char_class="Mage",
        race="Human",
        affinities=[AffinityState(affinity_id="fire", tier=tier, progress=progress)],
    )


def test_party_member_from_character_populates_affinity_advancements_after_tier_up() -> None:
    """Behavioral wiring: the field EXISTING is not enough — prove the full chain
    reaches the player. Run the real ``apply_affinity_tier_ups`` engine so it
    sets ``Character.last_affinity_tier_ups``, then build the player-facing
    ``PartyMember`` via ``party_member_from_character`` and assert the delta is
    actually copied. A regression that drops the populate line in views.py fails
    HERE — the reflection test above would still pass."""
    character = _mage("Rux", progress=100.0)
    sd = _synthetic_sd(character)

    crossings = apply_affinity_tier_ups(
        sd.snapshot,
        ProgressionConfig(
            affinities=[Affinity(name="fire", description="x", tier_thresholds=[10, 25, 50])]
        ),
    )
    assert crossings, "engine must produce a crossing for the seeded progress"

    member = party_member_from_character(
        MagicMock(),  # handler — only sd is used in this path
        sd,
        character,
        player_id="p1",
        player_name="Keith",
    )

    assert member.affinity_advancements, (
        "party_member_from_character must copy Character.last_affinity_tier_ups into "
        "PartyMember.affinity_advancements — the player-facing populate"
    )
    delta = member.affinity_advancements[0]
    assert delta.affinity_id == "fire"
    assert delta.before == 0
    assert delta.after == 3
    assert delta.driver == "affinity"
    assert delta.character_name == "Rux"


def test_party_member_affinity_advancements_empty_without_a_tier_up() -> None:
    """The companion negative: a character that did NOT advance an affinity this
    turn surfaces an empty ``affinity_advancements`` list — populated only on a
    real crossing, never a spurious delta on an ordinary turn."""
    character = _mage("Rux", progress=0.0)
    assert character.last_affinity_tier_ups == []  # default: nothing advanced
    sd = _synthetic_sd(character)

    member = party_member_from_character(
        MagicMock(), sd, character, player_id="p1", player_name="Keith"
    )
    assert member.affinity_advancements == []


def test_last_affinity_tier_ups_is_not_a_shared_mutable_default() -> None:
    """lang-review #2 (mutable default arguments / shared class state): two
    freshly constructed characters must NOT alias the same
    ``last_affinity_tier_ups`` list. A bare ``list`` default (instead of
    ``Field(default_factory=list)``) would share one list across every Character
    and leak one PC's advancements onto another."""
    a = _mage("A", progress=0.0)
    b = _mage("B", progress=0.0)
    assert a.last_affinity_tier_ups is not b.last_affinity_tier_ups, (
        "each Character must own a distinct last_affinity_tier_ups list"
    )
