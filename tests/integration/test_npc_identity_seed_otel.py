"""RED-phase contract for story 72-9 — wire OCEAN / disposition / scenario
``belief_state`` onto *narrator-invented* NPCs at the moment they become
mechanical.

Epic 72 (NPC Identity Hardening) DEEP-DIVE: when the narrator names a person
in neither store, the invented-mint path appends a bare
``NpcPoolMember(drawn_from="narrator_invented")``. When that scaffold first
needs mechanical state it is promoted to an ``Npc`` via
``_promote_pool_member_to_npc`` (reached in production through
``resolve_status_target``). Today that promotion builds an ``Npc`` with
``ocean=None``, an empty ``BeliefState``, and never touches
``snapshot.scenario_state`` — so an invented person is identity-thin: it
cannot develop a personality (ADR-042), and a suspect the narrator invents
mid-investigation can never carry a belief or be tracked in the scenario
graph (ADR-053).

This story seeds the three identity surfaces onto invented NPCs and emits a
new ``npc.identity_seeded`` ``state_transition`` watcher event so the GM panel
(Keith-as-dev lie-detector) can confirm the wiring fired rather than the
narrator improvising identity.

Test doctrine (server CLAUDE.md "No Source-Text Wiring Tests"): every test
drives the *real* invented-mint→promotion seam on a synthetic ``GameSnapshot``
and asserts (a) the resulting ``Npc`` carries the seeded fields and (b) the
new watcher event fired. No ``read_text()`` of production source.

Mirrors the harness in ``test_npc_spawn_disposition_otel.py`` (72-5).

== New watcher-event contract this story must satisfy (drives implementation) ==
A ``state_transition`` event routed from a new span:
  component: "npc_identity"
  fields.field: "npc.identity_seeded"
  fields.npc_name: <name>
  fields.ocean_seeded: bool        # True when a real OceanProfile was seeded
  fields.disposition: int          # the spawn disposition (neutral 0 for invented)
  fields.scenario_registered: bool # True iff a scenario was active and the NPC
                                   #   was registered into npc_roles
  fields.scenario_role: str        # role assigned when scenario active ("innocent")
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.belief_state import BeliefFact, BeliefSourceWitnessed
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Attitude, Disposition
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.narration_apply import resolve_status_target
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

_OCEAN_DIMS = {
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
}

# The new watcher-event field name this story introduces. A module-level
# constant so a rename is a one-line change, not a scatter of string literals.
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


def _find_event(captured: list[dict], field_value: str) -> dict | None:
    """Non-blocking lookup — returns the first matching event or ``None``.

    Used by guard tests that assert an event did NOT fire.
    """
    for evt in captured:
        if (
            evt.get("event_type") == "state_transition"
            and evt.get("fields", {}).get("field") == field_value
        ):
            return evt
    return None


def _invented_snapshot(
    *,
    npc_name: str = "Wexley",
    scenario_state: ScenarioState | None = None,
) -> GameSnapshot:
    """Synthetic snapshot whose only NPC is a narrator-invented pool scaffold
    awaiting promotion — the exact shape the production invented-mint path
    leaves behind."""
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[NpcPoolMember(name=npc_name, drawn_from="narrator_invented")],
        scenario_state=scenario_state,
    )


# ---------------------------------------------------------------------------
# AC-1 — invented NPC gets a real OCEAN profile (not None, not empty {})
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_gets_ocean_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1: a narrator-invented NPC promoted through the production path
    carries a real, non-``None`` OCEAN profile — a serialized ``OceanProfile``
    with all five Big-Five dimensions, never ``None`` and never an empty
    ``{}`` stub (No Stubbing)."""
    await _setup(monkeypatch, "test-identity-seed-ocean")
    snapshot = _invented_snapshot()

    promoted = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=3, trigger="test"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    assert promoted.ocean is not None, "invented NPC must be seeded with an OCEAN profile"
    assert promoted.ocean != {}, "an empty {} is a stub, not a wired OCEAN profile"
    assert _OCEAN_DIMS.issubset(promoted.ocean.keys()), (
        f"OCEAN profile must carry all Big-Five dimensions; got {sorted(promoted.ocean)}"
    )
    for dim in _OCEAN_DIMS:
        value = promoted.ocean[dim]
        assert isinstance(value, (int, float)), f"{dim} must be a real numeric value"
        assert 0.0 <= float(value) <= 10.0, f"{dim}={value} out of OCEAN 0–10 band"


# ---------------------------------------------------------------------------
# AC-2 — invented NPC spawns neutral, never born-hostile
# (regression guard: already satisfied by 72-2/72-5; locks it for this story)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_spawns_neutral_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-2: the invented NPC spawns with a *neutral* ``Disposition``
    (value 0 → ``Attitude.NEUTRAL`` per ADR-020), explicitly NOT the ``-20``
    born-hostile creature default. Guards that the OCEAN/belief seeding does
    not perturb the disposition 72-5 fixed."""
    await _setup(monkeypatch, "test-identity-seed-disposition")
    snapshot = _invented_snapshot()

    promoted = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=3, trigger="test"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    assert int(promoted.disposition) == 0, "invented NPC must spawn neutral, not -20"
    assert promoted.disposition.attitude() == Attitude.NEUTRAL


# ---------------------------------------------------------------------------
# AC-3 — scenario registration when a scenario is active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_npc_registered_in_active_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: with ``snapshot.scenario_state is not None``, promoting an
    invented NPC registers it into the scenario graph — its name appears in
    ``npc_roles`` with a default ``innocent`` role (never the pre-selected
    ``guilty_npc``) and it carries a live ``BeliefState`` mutation surface
    (mirrors ``bind_scenario``)."""
    await _setup(monkeypatch, "test-identity-seed-scenario")
    # A scenario whose guilty party is some *other* id — an invented walk-on
    # is never the pre-selected culprit.
    scenario = ScenarioState(guilty_npc="dr_mortimer")
    snapshot = _invented_snapshot(scenario_state=scenario)

    promoted = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=3, trigger="test"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    state = snapshot.scenario_state
    assert state is not None
    assert "Wexley" in state.npc_roles, (
        "invented NPC must be registered into scenario npc_roles when a scenario is active"
    )
    assert state.npc_roles["Wexley"].lower() == "innocent", (
        f"invented walk-on must default to innocent; got {state.npc_roles['Wexley']!r}"
    )
    assert "Wexley" != state.guilty_npc, "invented NPC must never be the pre-selected culprit"
    # Live mutation surface — gossip/questioning can add beliefs later.
    assert isinstance(promoted.belief_state.beliefs, list)


# ---------------------------------------------------------------------------
# AC-4 — no active scenario → OCEAN/disposition still seeded, no scenario wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scenario_still_seeds_ocean_and_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4 (edge): with ``scenario_state is None`` the invented NPC still
    gets OCEAN (AC-1) and neutral disposition (AC-2), no scenario registration
    is attempted, and nothing raises. ``scenario_state`` stays ``None``."""
    captured = await _setup(monkeypatch, "test-identity-seed-no-scenario")
    snapshot = _invented_snapshot(scenario_state=None)

    promoted = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=3, trigger="test"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    assert promoted.ocean is not None and promoted.ocean != {}
    assert int(promoted.disposition) == 0
    assert snapshot.scenario_state is None, "no scenario must be invented out of thin air"

    # The seed span must still fire, reporting scenario_registered=False.
    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    assert evt["fields"]["scenario_registered"] is False


# ---------------------------------------------------------------------------
# AC-5 — wiring + OTEL span reached from the production path (load-bearing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_seed_span_fires_from_production_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-5: driving the *real* invented-mint→promotion flow
    (``resolve_status_target``, not a direct helper call) emits the new
    ``npc.identity_seeded`` watcher event with the expected attributes. This
    is the refactor-stable wiring assertion required by "Verify Wiring, Not
    Just Existence" — the GM-panel lie-detector for this subsystem."""
    captured = await _setup(monkeypatch, "test-identity-seed-span")
    scenario = ScenarioState(guilty_npc="dr_mortimer")
    snapshot = _invented_snapshot(scenario_state=scenario)

    promoted = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=7, trigger="test"
    )
    await asyncio.sleep(0)
    assert promoted is not None

    evt = await _wait_for_event(captured, IDENTITY_SEEDED_FIELD)
    assert evt["component"] == "npc_identity"
    assert evt["fields"]["npc_name"] == "Wexley"
    assert evt["fields"]["ocean_seeded"] is True
    assert evt["fields"]["disposition"] == 0
    assert evt["fields"]["scenario_registered"] is True
    assert evt["fields"]["scenario_role"].lower() == "innocent"


# ---------------------------------------------------------------------------
# Edge — idempotency / no-clobber: an already-seeded NPC is not re-seeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_seeded_npc_not_reseeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge (no double-wire / no belief clobber): an NPC already mechanical in
    ``snapshot.npcs`` — already carrying a non-default OCEAN and a learned
    belief — is resolved without being re-seeded. Re-seeding would clobber
    learned facts. The seed must key off the invented-promotion lineage, not
    fire for every name resolution."""
    captured = await _setup(monkeypatch, "test-identity-seed-no-clobber")

    learned = BeliefFact(
        subject="Wexley",
        content="saw the courier at midnight",
        turn_learned=2,
        source=BeliefSourceWitnessed(),
    )
    existing = Npc(
        core=CreatureCore(
            name="Wexley",
            description="known informant",
            personality="cagey",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        pool_origin="Wexley",
        ocean={
            "openness": 9.0,
            "conscientiousness": 2.0,
            "extraversion": 8.0,
            "agreeableness": 3.0,
            "neuroticism": 7.0,
        },
    )
    existing.belief_state.add_belief(learned)

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npcs=[existing],
        npc_pool=[NpcPoolMember(name="Wexley", drawn_from="narrator_invented")],
    )

    resolved = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=9, trigger="test"
    )
    await asyncio.sleep(0)

    assert resolved is not None
    # Learned belief survives — not clobbered by a fresh empty BeliefState.
    assert any(
        b.content == "saw the courier at midnight" for b in resolved.belief_state.beliefs
    ), "an already-learned belief must survive name resolution (no clobber)"
    # The distinctive authored OCEAN survives — not flattened to a baseline.
    assert resolved.ocean is not None
    assert resolved.ocean["openness"] == 9.0, "existing OCEAN must not be re-seeded"
    # And the seed span must NOT fire for an already-mechanical NPC.
    assert _find_event(captured, IDENTITY_SEEDED_FIELD) is None, (
        "identity-seed must not fire for an NPC already present in snapshot.npcs"
    )


# ---------------------------------------------------------------------------
# Edge — lineage guard: a non-invented pool member is not invented-seeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_authored_promotion_does_not_fire_invented_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge (lineage guard): promoting a ``world_authored`` pool member must
    not fire the narrator-invented identity-seed event. Per the story scope
    the seed keys off ``drawn_from="narrator_invented"`` only — authored NPCs
    get their identity through ``world_materialization``, not this seam, so
    double-wiring them here would be wrong."""
    captured = await _setup(monkeypatch, "test-identity-seed-lineage-guard")
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[
            NpcPoolMember(
                name="Magistrate Vell",
                drawn_from="world_authored",
                disposition=Disposition(18),
            )
        ],
    )

    promoted = resolve_status_target(
        snapshot, actor_name="Magistrate Vell", turn_num=4, trigger="test"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    # 72-2 carry-through is untouched: the authored friendly disposition survives.
    assert int(promoted.disposition) == 18
    assert _find_event(captured, IDENTITY_SEEDED_FIELD) is None, (
        "invented identity-seed must not fire for a world_authored pool member"
    )
