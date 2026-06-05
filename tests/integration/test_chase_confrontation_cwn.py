"""Story 86-3 (Plan 3): CWN chase confrontation — end-to-end + OTEL.

The named integration test for Plan 3. The GM panel is the lie detector
(CLAUDE.md OTEL Observability Principle): a chase must fire a typed span on
each beat carrying the §2.6.2 pace/pursuit *decision*, so we can tell the
mechanical engine engaged rather than the narrator improvising a chase.

Drives a chase round through the production seam against a real
``TracerProvider`` + ``WatcherSpanProcessor`` (same harness as
``tests/integration/test_rig_two_pool_combat.py``) and asserts:

  - a ``chase.pursuit_resolved`` state_transition span (component="chase")
    fires *per round* — the "confrontation.* spans on each chase beat"
    requirement — carrying pace / pursuer_effective / outcome;
  - the outcome is driven by the §2.6.2 math (resolve_pursuit), not prose;
  - a pursuer that beats the pace converges into vehicle combat (the Plan 2
    hand-off), and one that does not, does not.

**Architecture-agnostic by design** (following the 86-2 precedent): this
pins the *behavior + telemetry* the wiring must satisfy, not an internal
routing name. The pre-rolled ``fleeing_drive_total`` / ``pursuer_total``
stand in for the server-rolled Drive checks so the test does not depend on
RNG.

Proposed seam (TEA contract, open to Dev refinement):
    resolve_chase_round(
        *, fleeing_drive_total, pursuer_total,
        situational_modifier=0, passenger_hinder_successes=0,
        fleeing_id, pursuer_id, location=None,
    ) -> ChaseRoundResult
      — computes the CWN §2.6.2 pace/pursuit outcome via
        chase_pace.resolve_pursuit, emits a chase.pursuit_resolved span
        (component="chase"), and reports converges_to_combat.

RED until Dev wires the chase resolver + its span route.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


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


def _chase_events(captured: list[dict]) -> list[dict]:
    """The routed chase state_transition events (component="chase")."""
    return [
        e
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("component") == "chase"
    ]


@pytest.mark.asyncio
async def test_chase_round_fires_pursuit_resolved_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved chase round must publish a typed ``chase.pursuit_resolved``
    span through the real watcher route so the GM panel sees the §2.6.2
    decision — not improvised chase prose."""
    from sidequest.game.chase_pace import resolve_chase_round

    captured = await _setup(monkeypatch, "test-chase-span")
    await asyncio.sleep(0.05)
    captured.clear()

    resolve_chase_round(
        fleeing_drive_total=15,
        pursuer_total=17,
        fleeing_id="player",
        pursuer_id="war_party",
        location="hardpan_flats",
    )
    await asyncio.sleep(0.05)

    events = _chase_events(captured)
    ops = [e["fields"].get("op") for e in events]
    assert "pursuit_resolved" in ops, (
        f"chase.pursuit_resolved must fire on a chase round (got {ops})"
    )


@pytest.mark.asyncio
async def test_chase_span_carries_the_srd_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The span must carry the §2.6.2 math (pace, pursuer_effective, outcome)
    — ADR-031 Layer-2: the span records what was decided, so the GM panel
    renders the chase deterministically without re-deriving it."""
    from sidequest.game.chase_pace import resolve_chase_round

    captured = await _setup(monkeypatch, "test-chase-fields")
    await asyncio.sleep(0.05)
    captured.clear()

    # 17 pursuer − 2 (can't see) = 15 effective, tie with pace 15 → evade.
    resolve_chase_round(
        fleeing_drive_total=15,
        pursuer_total=17,
        situational_modifier=-2,
        fleeing_id="player",
        pursuer_id="war_party",
    )
    await asyncio.sleep(0.05)

    resolved = [
        e for e in _chase_events(captured) if e["fields"].get("op") == "pursuit_resolved"
    ]
    assert len(resolved) == 1, f"expected one resolved round (got {len(resolved)})"
    f = resolved[0]["fields"]
    assert f["pace"] == 15
    assert f["pursuer_effective"] == 15, "must report situational-adjusted roll"
    assert f["outcome"] == "evaded", "tie-after-modifier loses (strict beat)"


@pytest.mark.asyncio
async def test_caught_round_converges_into_vehicle_combat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beating the pace closes the chase into Plan 2 vehicle combat — the
    §2.6.2 '→ vehicle combat' hand-off. The result must report the
    convergence so the confrontation layer can switch encounter types."""
    from sidequest.game.chase_pace import resolve_chase_round

    await _setup(monkeypatch, "test-chase-converge")

    result = resolve_chase_round(
        fleeing_drive_total=14,
        pursuer_total=18,
        fleeing_id="player",
        pursuer_id="war_party",
    )
    await asyncio.sleep(0.05)

    assert result.converges_to_combat is True, (
        "a pursuer that beats the pace must converge into vehicle combat"
    )


@pytest.mark.asyncio
async def test_evaded_round_does_not_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the convergence gate: a chase the pursuer does NOT win must not
    spuriously drop into combat (which would make every chase a firefight)."""
    from sidequest.game.chase_pace import resolve_chase_round

    await _setup(monkeypatch, "test-chase-no-converge")

    result = resolve_chase_round(
        fleeing_drive_total=18,
        pursuer_total=12,
        fleeing_id="player",
        pursuer_id="war_party",
    )
    await asyncio.sleep(0.05)

    assert result.converges_to_combat is False, (
        "an unsuccessful pursuit must stay a chase, not convert to combat"
    )


@pytest.mark.asyncio
async def test_each_round_fires_its_own_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'OTEL confrontation.* spans on each chase beat' — two rounds must
    produce two spans, so a multi-beat chase is fully auditable, not just
    the final result."""
    from sidequest.game.chase_pace import resolve_chase_round

    captured = await _setup(monkeypatch, "test-chase-per-round")
    await asyncio.sleep(0.05)
    captured.clear()

    resolve_chase_round(
        fleeing_drive_total=16,
        pursuer_total=12,
        fleeing_id="player",
        pursuer_id="war_party",
    )
    resolve_chase_round(
        fleeing_drive_total=14,
        pursuer_total=18,
        fleeing_id="player",
        pursuer_id="war_party",
    )
    await asyncio.sleep(0.05)

    resolved = [
        e for e in _chase_events(captured) if e["fields"].get("op") == "pursuit_resolved"
    ]
    assert len(resolved) == 2, (
        f"each chase round must fire its own span (got {len(resolved)})"
    )
