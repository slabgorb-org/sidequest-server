"""Wiring for OCEAN + disposition + scenario belief_state enrichment of
narrator-invented NPCs (story 72-9, epic 72 — NPC Identity Hardening).

A human DM who invents a stranger at the table gives that stranger a
personality, an attitude toward the party, and — in a mystery — a stake
in the plot. The engine does not: the invented-mint path appends a bare
``NpcPoolMember(drawn_from="narrator_invented")`` and, when that name
first needs mechanical state, ``_promote_pool_member_to_npc`` builds an
``Npc`` with **no OCEAN** (``ocean=None``), an **empty** ``BeliefState``,
and **no** scenario registration. An invented suspect can therefore never
develop a personality (ADR-042), never carry a suspicion, never be
questioned-as-tracked (ADR-053).

Story 72-9 seeds the three identity surfaces at the single promotion seam
(``_promote_pool_member_to_npc`` via its production caller
``resolve_status_target``):

- AC-1  OCEAN — invented NPC gets a real, non-``None`` ``OceanProfile``.
- AC-2  Disposition — invented NPC spawns **neutral** (0), never -20.
- AC-3  Scenario — when a scenario is active, the invented NPC is
        registered into ``scenario_state.npc_roles`` (role ``Innocent``,
        never the pre-selected ``guilty_npc``) with a live ``BeliefState``.
- AC-4  No active scenario → OCEAN/disposition still seeded, no scenario
        wiring attempted, nothing fails.
- AC-5  A new OCEAN/belief-seed OTEL span fires **from the production
        path** so the GM panel can verify the wiring engaged (the
        lie-detector) — not a direct helper call.

Same harness as ``test_npc_spawn_disposition_otel.py``: drive the *real*
materialization seam (``resolve_status_target`` →
``_promote_pool_member_to_npc``) and assert both the resulting ``Npc``
state and the routed watcher event.

The OTEL contract this story must satisfy (the SpanRoute Dev registers):
a ``state_transition`` watcher event with ``fields.field`` equal to
``IDENTITY_SEEDED_FIELD`` carrying ``npc_name`` / ``ocean_seeded`` (bool) /
``disposition`` (int) / ``scenario_registered`` (bool) / ``role`` (the
scenario role assigned, empty string when not registered).
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.belief_state import (
    BeliefFact,
    BeliefSourceWitnessed,
    BeliefState,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.scenario_state import ScenarioRole, ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.ocean import OceanProfile
from sidequest.server.narration_apply import resolve_status_target
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

# The watcher ``field`` for story 72-9's OCEAN/belief-seed span. Dev registers
# a SpanRoute mapping the new span to a ``state_transition`` event carrying
# this ``field`` so the GM panel can extract it (context §OTEL). The exact
# span *constant* name is Dev's choice; this ``field`` value is the contract
# the wiring assertions below pin.
IDENTITY_SEEDED_FIELD = "npc.identity_seeded"


def _make_pc(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        char_class="Fighter",
        race="Human",
        backstory=f"{name} test",
    )


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Bind a fresh watcher loop + in-memory tracer, return the captured
    event list. Mirrors ``test_npc_spawn_disposition_otel.py::_setup``."""
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
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return captured


async def _wait_for_event(
    captured: list[dict], field_value: str, *, timeout_s: float = 1.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("field") == field_value
            ):
                return evt
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Expected state_transition with field={field_value!r} within {timeout_s}s; "
        f"captured: {[(e.get('event_type'), e.get('fields', {}).get('field')) for e in captured]}"
    )


def _has_event(captured: list[dict], field_value: str) -> bool:
    return any(
        evt.get("event_type") == "state_transition"
        and evt.get("fields", {}).get("field") == field_value
        for evt in captured
    )


def _invented_snapshot(*, scenario: ScenarioState | None = None) -> GameSnapshot:
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[NpcPoolMember(name="Wexley", drawn_from="narrator_invented")],
    )
    if scenario is not None:
        snapshot.scenario_state = scenario
    return snapshot


# ---------------------------------------------------------------------------
# AC-1 — invented NPC gets a real OCEAN profile.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_seeds_ocean_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1: promoting a ``narrator_invented`` pool member yields an ``Npc``
    whose ``ocean`` is a real, serialized ``OceanProfile`` — not ``None`` and
    not an empty ``{}`` (No Stubbing / No Silent Fallbacks)."""
    await _setup(monkeypatch, "test-invented-ocean")

    snapshot = _invented_snapshot()
    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=3, trigger="test")
    await asyncio.sleep(0)

    assert promoted is not None
    # Not silently None, not an empty dict masquerading as "wired".
    assert promoted.ocean is not None
    assert promoted.ocean != {}
    # A real Big-Five profile: round-trips through OceanProfile with all five
    # dimensions present and in-range.
    profile = OceanProfile(**promoted.ocean)
    dims = (
        profile.openness,
        profile.conscientiousness,
        profile.extraversion,
        profile.agreeableness,
        profile.neuroticism,
    )
    assert all(0.0 <= d <= 10.0 for d in dims)


# ---------------------------------------------------------------------------
# AC-2 — invented NPC spawns neutral, never born-hostile.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_spawns_neutral_disposition(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: the seeded invented NPC keeps a **neutral** disposition (0 →
    ``Attitude.neutral``), explicitly not the -20 born-hostile creature
    default. Guards that adding OCEAN/belief seeding does not perturb the
    72-2/72-5 neutral-spawn contract."""
    await _setup(monkeypatch, "test-invented-neutral")

    snapshot = _invented_snapshot()
    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=3, trigger="test")
    await asyncio.sleep(0)

    assert promoted is not None
    assert int(promoted.disposition) == 0
    assert promoted.disposition.attitude().value == "neutral"


# ---------------------------------------------------------------------------
# AC-3 — scenario registration when a scenario is active.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_registered_into_active_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: with ``snapshot.scenario_state`` set, the promoted invented NPC
    is registered into ``npc_roles`` with the default ``Innocent`` role — an
    invented walk-on is never the pre-selected ``guilty_npc``."""
    await _setup(monkeypatch, "test-invented-scenario-role")

    scenario = ScenarioState(guilty_npc="the_real_culprit")
    snapshot = _invented_snapshot(scenario=scenario)

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=4, trigger="test")
    await asyncio.sleep(0)

    assert promoted is not None
    assert snapshot.scenario_state is not None
    assert "Wexley" in snapshot.scenario_state.npc_roles
    assert snapshot.scenario_state.npc_roles["Wexley"] == ScenarioRole.Innocent
    # Never minted as the guilty suspect.
    assert snapshot.scenario_state.npc_roles["Wexley"] != ScenarioRole.Guilty


@pytest.mark.asyncio
async def test_invented_npc_carries_live_belief_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: the registered invented NPC carries a live ``BeliefState`` — a
    real mutation surface (gossip/questioning can ``add_belief`` later), not a
    frozen placeholder. Starts empty (a walk-on has no pack initial_beliefs)
    but reflects mutations."""
    await _setup(monkeypatch, "test-invented-belief-surface")

    scenario = ScenarioState(guilty_npc="the_real_culprit")
    snapshot = _invented_snapshot(scenario=scenario)

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=4, trigger="test")
    await asyncio.sleep(0)

    assert promoted is not None
    assert isinstance(promoted.belief_state, BeliefState)
    # Live surface: a belief added now is readable back.
    promoted.belief_state.add_belief(
        BeliefFact(
            subject="Wexley",
            content="was seen near the conservatory",
            turn_learned=4,
            source=BeliefSourceWitnessed(),
        )
    )
    assert promoted.belief_state.beliefs_about("Wexley")


# ---------------------------------------------------------------------------
# AC-4 — no scenario → OCEAN/disposition still seeded, no scenario wiring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scenario_still_seeds_ocean_no_scenario_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4: with ``scenario_state is None`` the invented NPC still gets OCEAN
    (AC-1) and a neutral disposition (AC-2); no scenario registration is
    attempted and nothing raises."""
    await _setup(monkeypatch, "test-invented-no-scenario")

    snapshot = _invented_snapshot(scenario=None)
    assert snapshot.scenario_state is None

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=2, trigger="test")
    await asyncio.sleep(0)

    assert promoted is not None
    assert promoted.ocean is not None  # OCEAN seeded regardless of scenario
    assert int(promoted.disposition) == 0
    # No scenario was conjured into existence as a side effect.
    assert snapshot.scenario_state is None


# ---------------------------------------------------------------------------
# AC-5 — the OCEAN/belief-seed span fires from the production path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_seeded_span_fires_from_production_path_with_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-5 (load-bearing wiring): driving the *real* production caller
    (``resolve_status_target`` — not a direct ``_promote_pool_member_to_npc``
    call) fires the OCEAN/belief-seed span, routed to a ``state_transition``
    watcher event the GM panel can read. With a scenario active the event
    reports ``scenario_registered=True`` and the assigned ``role``."""
    captured = await _setup(monkeypatch, "test-identity-seeded-span-scenario")

    scenario = ScenarioState(guilty_npc="the_real_culprit")
    snapshot = _invented_snapshot(scenario=scenario)

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=7, trigger="test")
    await asyncio.sleep(0)
    assert promoted is not None

    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    fields = evt["fields"]
    assert fields["npc_name"] == "Wexley"
    assert fields["ocean_seeded"] is True
    assert fields["disposition"] == 0
    assert fields["scenario_registered"] is True
    assert fields["role"] == ScenarioRole.Innocent


@pytest.mark.asyncio
async def test_identity_seeded_span_reports_unregistered_without_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-5 / AC-4: with no scenario the seed span still fires (OCEAN was
    seeded) but reports ``scenario_registered=False`` — the GM panel can tell
    "enriched, no mystery here" from a silently-skipped enrichment."""
    captured = await _setup(monkeypatch, "test-identity-seeded-span-no-scenario")

    snapshot = _invented_snapshot(scenario=None)
    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=7, trigger="test")
    await asyncio.sleep(0)
    assert promoted is not None

    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    fields = evt["fields"]
    assert fields["npc_name"] == "Wexley"
    assert fields["ocean_seeded"] is True
    assert fields["scenario_registered"] is False


# ---------------------------------------------------------------------------
# Edge — lineage guard: the seed fires ONLY for narrator_invented.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_authored_promotion_does_not_fire_invented_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge (scope boundary): promoting a ``world_authored`` pool member must
    NOT fire the 72-9 invented-enrichment seed — authored NPCs receive OCEAN /
    belief at chargen via ``world_materialization``, and the seed keys off the
    ``narrator_invented`` lineage, not all ``Npc`` construction. Re-seeding
    here would risk clobbering authored identity (context §No-double-wire)."""
    captured = await _setup(monkeypatch, "test-authored-no-invented-seed")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[NpcPoolMember(name="Magistrate Vane", drawn_from="world_authored")],
    )

    promoted = resolve_status_target(
        snapshot, actor_name="Magistrate Vane", turn_num=5, trigger="test"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    # The invented-only seed did not run for an authored member.
    assert promoted.ocean is None
    assert not _has_event(captured, IDENTITY_SEEDED_FIELD)


# ---------------------------------------------------------------------------
# Edge — no clobber: re-engaging an already-seeded NPC does not re-seed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reengagement_does_not_clobber_seeded_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge (no double-wire / no belief clobber): once an invented NPC is
    promoted and seeded, a later engagement resolves the existing ``Npc`` and
    must NOT re-run the seed — a belief learned in between survives."""
    await _setup(monkeypatch, "test-no-clobber-reseed")

    snapshot = _invented_snapshot()

    first = resolve_status_target(snapshot, actor_name="Wexley", turn_num=3, trigger="test")
    await asyncio.sleep(0)
    assert first is not None
    assert first.ocean is not None

    # A belief is learned about Wexley between engagements.
    first.belief_state.add_belief(
        BeliefFact(
            subject="Wexley",
            content="lied about the timeline",
            turn_learned=3,
            source=BeliefSourceWitnessed(),
        )
    )
    seeded_ocean = first.ocean

    # Re-engage: resolve_status_target finds the existing Npc (npcs shadow the
    # pool) and returns it — no second promotion, no re-seed.
    second = resolve_status_target(snapshot, actor_name="Wexley", turn_num=6, trigger="test")
    await asyncio.sleep(0)

    assert second is not None
    assert second is first  # same object, not a fresh promotion
    assert second.ocean == seeded_ocean  # OCEAN not re-rolled / clobbered
    assert second.belief_state.beliefs_about("Wexley")  # learned belief survived
    # Exactly one stateful Npc for Wexley — no duplicate promotion.
    assert sum(1 for n in snapshot.npcs if n.core.name == "Wexley") == 1
