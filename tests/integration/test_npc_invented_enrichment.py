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
from sidequest.game.disposition import Disposition
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
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert promoted is not None
    # Not silently None, not an empty dict masquerading as "wired".
    assert promoted.ocean is not None
    assert promoted.ocean != {}
    # A real Big-Five profile: round-trips through OceanProfile with all five
    # dimensions present. Pin the documented seed *policy* — the flat baseline
    # (every dimension 5.0) — not an always-true [0,10] range check (which a
    # broken 1000.0 default or an unclamped field would still pass).
    profile = OceanProfile(**promoted.ocean)
    dims = (
        profile.openness,
        profile.conscientiousness,
        profile.extraversion,
        profile.agreeableness,
        profile.neuroticism,
    )
    assert all(d == 5.0 for d in dims)


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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert promoted is not None
    # AC-2 is the spawn *value* (0). The value→attitude mapping is Disposition's
    # own tested behavior and depends on the process-global attitude thresholds
    # (mutable by sibling tests under xdist), so asserting attitude() here would
    # couple this test to global state it does not control — assert the value only.
    assert int(promoted.disposition) == 0


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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert promoted is not None
    assert snapshot.scenario_state is not None
    # Registered as the default walk-on role — never the pre-selected guilty
    # suspect. (Asserting == Innocent already proves != Guilty, so no redundant
    # tautological `!= Guilty` line.)
    assert "Wexley" in snapshot.scenario_state.npc_roles
    assert snapshot.scenario_state.npc_roles["Wexley"] == ScenarioRole.Innocent


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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert promoted is not None
    assert isinstance(promoted.belief_state, BeliefState)
    # The load-bearing 72-9 contract: a walk-on starts with an EMPTY belief
    # bubble — no authored ``initial_beliefs`` leaked in from the pack (which
    # would happen if the seed accidentally registered the invented NPC as an
    # authored scenario actor). `isinstance` alone would pass with zero
    # implementation, so pin the empty-start invariant explicitly.
    assert promoted.belief_state.beliefs == []
    # And the surface is live: a belief added now is readable back.
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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`
    assert promoted is not None

    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    fields = evt["fields"]
    assert fields["npc_name"] == "Wexley"
    assert fields["ocean_seeded"] is True
    assert fields["disposition"] == 0
    assert fields["scenario_registered"] is True
    assert fields["role"] == ScenarioRole.Innocent
    # The span must carry the REAL turn it fired on, not a hardcoded default.
    # ``resolve_status_target`` has ``turn_num`` in scope; a constant 0 would
    # collapse every invented-NPC seed onto turn 0 in the GM timeline (the
    # "lie-detector lies" failure the OTEL principle exists to prevent).
    assert fields["turn_number"] == 7


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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`
    assert promoted is not None

    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    fields = evt["fields"]
    assert fields["npc_name"] == "Wexley"
    assert fields["ocean_seeded"] is True
    assert fields["scenario_registered"] is False
    # Contract: ``role`` is empty when no scenario is active (documented in the
    # span/SpanRoute). If production set a role here despite no scenario, this
    # would catch the contract violation.
    assert fields["role"] == ""


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
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

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
    must NOT re-run the seed — a belief learned in between survives, and the
    identity-seed span fires exactly ONCE (the authoritative lie-detector
    signal that no re-seed occurred)."""
    captured = await _setup(monkeypatch, "test-no-clobber-reseed")

    snapshot = _invented_snapshot()

    first = resolve_status_target(snapshot, actor_name="Wexley", turn_num=3, trigger="test")
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`
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
    # Snapshot the seeded OCEAN as an independent COPY before re-engaging. The
    # earlier `seeded_ocean = first.ocean` alias made the later comparison
    # `first.ocean == first.ocean` (tautological — `second is first`); a copy
    # detects an in-place re-roll of the dict.
    seeded_ocean = dict(first.ocean)

    # Re-engage: resolve_status_target finds the existing Npc (npcs shadow the
    # pool) and returns it — no second promotion, no re-seed.
    second = resolve_status_target(snapshot, actor_name="Wexley", turn_num=6, trigger="test")
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert second is not None
    assert second is first  # same object, not a fresh promotion
    assert second.ocean == seeded_ocean  # OCEAN not re-rolled / clobbered (vs independent copy)
    assert second.belief_state.beliefs_about("Wexley")  # learned belief survived
    # Exactly one stateful Npc for Wexley — no duplicate promotion.
    assert sum(1 for n in snapshot.npcs if n.core.name == "Wexley") == 1
    # The authoritative no-re-seed signal: the identity-seed span fired exactly
    # once (on the first promotion), never on re-engagement.
    identity_events = [
        e for e in captured if e.get("fields", {}).get("field") == IDENTITY_SEEDED_FIELD
    ]
    assert len(identity_events) == 1


# ---------------------------------------------------------------------------
# Edge — scenario-role clobber: invented registration must NOT overwrite an
# existing authored role (the murderer must stay guilty).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_does_not_clobber_existing_scenario_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge (HIGH — mystery integrity): ``npc_roles`` is keyed over *all*
    ``scenario_pack.npcs`` at bind time, including the guilty NPC, even ones
    not yet materialized into ``snapshot.npcs``. The narrator-invented mint
    path checks ``npcs``/``npc_pool`` for collisions but NOT ``npc_roles`` —
    so a walk-on whose name matches an authored suspect can be promoted via
    ``resolve_status_target``. Seeding must NOT overwrite that NPC's existing
    role: registering ``Innocent`` over an authored ``Guilty`` would turn the
    murderer innocent and make the mystery unwinnable.

    Drives the membership-guard fix: register the invented NPC only when its
    name is not already a scenario participant.
    """
    await _setup(monkeypatch, "test-no-clobber-scenario-role")

    # "Wexley" is the pre-selected guilty suspect, present in npc_roles but NOT
    # yet materialized into snapshot.npcs (off-screen). The pool holds a
    # narrator-invented "Wexley" walk-on (see _invented_snapshot).
    scenario = ScenarioState(
        guilty_npc="wexley_id",
        npc_roles={"Wexley": ScenarioRole.Guilty},
    )
    snapshot = _invented_snapshot(scenario=scenario)

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=8, trigger="test")
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert promoted is not None
    # The authored Guilty role MUST survive — never silently demoted to Innocent.
    assert snapshot.scenario_state.npc_roles["Wexley"] == ScenarioRole.Guilty


# ---------------------------------------------------------------------------
# Edge — disposition carry-through (72-2 × 72-9): a non-zero carried
# disposition survives seeding and is reported honestly by the span.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_seed_preserves_carried_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge (72-2 × 72-9): a ``narrator_invented`` pool member that accrued a
    non-zero disposition before promotion (e.g. the table befriended it) must
    keep that value — the OCEAN/scenario seed must not flatten it — and the
    identity-seed span must report the *carried* value, not a hardcoded 0.
    Guards the 72-2 preservation contract against 72-9 regression and pins
    that the span's ``disposition`` is the real promotion-time value."""
    captured = await _setup(monkeypatch, "test-invented-carried-disposition")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[
            NpcPoolMember(
                name="Wexley",
                drawn_from="narrator_invented",
                disposition=Disposition(value=30),
            )
        ],
    )

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=5, trigger="test")
    await asyncio.sleep(0)  # yield: let the WatcherHub broadcast coroutine deliver to `captured`

    assert promoted is not None
    # 72-2 carry-through survives the 72-9 seed.
    assert int(promoted.disposition) == 30
    assert promoted.ocean is not None  # still enriched
    # The span reports the real carried disposition, not a "neutral spawn" 0.
    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    assert evt["fields"]["disposition"] == 30
