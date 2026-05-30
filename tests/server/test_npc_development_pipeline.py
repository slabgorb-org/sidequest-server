"""RED tests for Story 72-1 — Revive the dormant NPC development pipeline.

The development tick rides the existing ``npcs_hit`` branch of
``_apply_npc_mentions`` (``narration_apply.py`` ~1349-1377) — the canonical
"this NPC was engaged this turn" signal that already stamps ``last_seen_*``
and fires ``npc.referenced(match_strategy="npcs_hit")``. On each such
engagement the tick must, per ADR-014 (Diamonds and Coal) / ADR-020 (NPC
Disposition):

  1. increment ``Npc.non_transactional_interactions`` (AC1)
  2. escalate ``Npc.resolution_tier`` past ``"spawn"`` at thresholds (AC2)
  3. drift ``Npc.disposition`` emergently via the ``Disposition`` wrapper (AC3)
  4. emit a development-tick OTEL span carrying npc_name + count + tier
     old/new — also the story's WIRING test (AC4)
  5. emit the existing ``disposition.shift`` span when disposition changes (AC5)
  6. leave the transactional-only promotion paths untouched (AC6)

Per server CLAUDE.md "No Source-Text Wiring Tests", every AC is verified by
driving the real ``_apply_npc_mentions`` flow over a synthetic snapshot and
asserting on mutated ``Npc`` state / emitted spans — never by grepping source.

Design decisions the spec hands to Dev, pinned here so they can't silently
regress (see the session-file deviation log):
  * Tier names/thresholds are Dev's choice; these tests assert *behavior*
    (monotonic, starts at "spawn", eventually escalates, crisp boundary)
    discovered empirically — no threshold literal is hardcoded.
  * Disposition drift sign = positive (the documented "neutral engagement
    warms slightly" default). Magnitude assumed small (single-digit/turn).
  * De-dup rule: the same name twice in one turn's mention list is ONE
    engagement event → +1 (interest is boolean-per-turn, not per-utterance).
  * Dev-tick span: identified here by its payload (carries
    ``non_transactional_interactions``); name asserted to follow the
    ``npc.*`` convention (recommended: ``npc.developed``). Pinned attribute
    keys: ``npc_name`` (NOT the reserved ``name``),
    ``non_transactional_interactions``, ``resolution_tier_before``,
    ``resolution_tier_after``.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import (
    Attitude,
    AttitudeThresholds,
    configure_attitude_thresholds,
    reset_attitude_thresholds,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.narration_apply import _apply_npc_mentions
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

# Recommended span name (Dev may rename within the npc.* family; tests
# identify the span by payload, not this literal).
RECOMMENDED_DEV_SPAN_NAME = "npc.developed"
_DEV_SPAN_COUNT_ATTR = "non_transactional_interactions"


# ---------------------------------------------------------------------------
# Helpers — mirror tests/server/test_npc_pool_narration_apply.py
# ---------------------------------------------------------------------------


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _pc(name: str) -> Character:
    return Character(
        core=_core(name),
        backstory="A wanderer.",
        char_class="adventurer",
        race="human",
    )


def _mention(
    name: str,
    *,
    role: str = "",
    pronouns: str = "",
    appearance: str = "",
) -> NpcMention:
    return NpcMention(name=name, role=role, pronouns=pronouns, appearance=appearance)


def _snapshot(*npcs: Npc, pc: str = "Hero", location: str = "TavernRow") -> GameSnapshot:
    """A snapshot with one PC at ``location`` and the given stateful NPCs.

    Mirrors the known-good setup in
    ``test_cite_known_npc_updates_last_seen_on_npc`` so the ``npcs_hit``
    branch's ``party_location(perspective=...)`` call resolves cleanly.
    """
    snap = GameSnapshot(characters=[_pc(pc)], npcs=list(npcs))
    snap.character_locations[pc] = location
    return snap


def _engage(snapshot: GameSnapshot, name: str, *, turn: int, acting: str = "Hero") -> None:
    """Drive a single ``npcs_hit`` engagement of ``name`` on ``snapshot``."""
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention(name)],
        turn_num=turn,
        acting_character_name=acting,
    )


def _drive(snapshot: GameSnapshot, name: str, *, turns: int) -> list[Npc]:
    """Engage ``name`` once per turn for ``turns`` turns; return a snapshot of
    the live ``Npc`` after each turn (same instance — read fields per step)."""
    npc = next(n for n in snapshot.npcs if n.core.name == name)
    steps: list[Npc] = []
    for t in range(1, turns + 1):
        _engage(snapshot, name, turn=t)
        steps.append(npc)
    return steps


@pytest.fixture(autouse=True)
def _isolate_thresholds():
    """Genre-pack attitude thresholds are process-global; reset around every
    test so the band-crossing test can't leak its custom bands."""
    reset_attitude_thresholds()
    try:
        yield
    finally:
        reset_attitude_thresholds()


@pytest.fixture
def otel_capture():
    """SDK provider + in-memory exporter (pattern from
    tests/integration/test_npc_edge_publish_wiring.py)."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ===========================================================================
# AC1 — Interest signal increments on non-transactional engagement
# ===========================================================================


def test_npcs_hit_increments_interaction_counter() -> None:
    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Boris", turn=1)
    assert snap.npcs[0].non_transactional_interactions == 1


def test_repeated_engagement_accumulates_one_per_turn() -> None:
    snap = _snapshot(Npc(core=_core("Boris")))
    for t in range(1, 6):
        _engage(snap, "Boris", turn=t)
    assert snap.npcs[0].non_transactional_interactions == 5


def test_distinct_npcs_increment_independently() -> None:
    snap = _snapshot(Npc(core=_core("Boris")), Npc(core=_core("Clara")))
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Boris"), _mention("Clara")],
        turn_num=1,
        acting_character_name="Hero",
    )
    by_name = {n.core.name: n for n in snap.npcs}
    assert by_name["Boris"].non_transactional_interactions == 1
    assert by_name["Clara"].non_transactional_interactions == 1


def test_same_npc_named_twice_in_one_turn_increments_once() -> None:
    """Interest is boolean-per-turn: a name repeated in one mention list is a
    single engagement event (de-dup). Pins the de-dup rule (see deviation log)."""
    snap = _snapshot(Npc(core=_core("Boris")))
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Boris"), _mention("Boris")],
        turn_num=1,
        acting_character_name="Hero",
    )
    assert snap.npcs[0].non_transactional_interactions == 1


def test_pool_only_mention_does_not_increment_any_npc() -> None:
    """A ``pool_hit`` has no stateful ``Npc`` to carry a counter — the tick
    must not fire for it."""
    snap = _snapshot()
    snap.npc_pool.append(NpcPoolMember(name="Marya", drawn_from="legacy_registry"))
    _engage(snap, "Marya", turn=1)
    assert snap.npcs == []  # no Npc materialized
    assert all(n.non_transactional_interactions == 0 for n in snap.npcs)


def test_invented_mention_does_not_increment_existing_npc() -> None:
    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Stranger", turn=1)  # invented — not Boris
    assert snap.npcs[0].non_transactional_interactions == 0
    assert {m.name for m in snap.npc_pool} == {"Stranger"}


# ===========================================================================
# AC2 — resolution_tier escalates past "spawn" at thresholds (monotonic)
# ===========================================================================


def test_fresh_npc_starts_at_spawn_tier() -> None:
    assert Npc(core=_core("Boris")).resolution_tier == "spawn"


def test_single_engagement_does_not_escalate_tier() -> None:
    """Below the first threshold the tier stays ``"spawn"`` — one engagement
    must not promote (first threshold >= 2; see deviation log)."""
    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Boris", turn=1)
    assert snap.npcs[0].resolution_tier == "spawn"


def test_sustained_engagement_eventually_escalates_past_spawn() -> None:
    snap = _snapshot(Npc(core=_core("Boris")))
    steps = _drive(snap, "Boris", turns=40)
    final_tier = steps[-1].resolution_tier
    assert final_tier != "spawn", (
        "40 turns of engagement never escalated resolution_tier past 'spawn' — "
        "the development pipeline is still dormant."
    )


def test_tier_escalation_is_monotonic_non_decreasing() -> None:
    """Once the tier leaves a band it never returns. Encodes 'monotonic' via
    first-seen order — no threshold literal required."""
    snap = _snapshot(Npc(core=_core("Boris")))
    tiers = [npc.resolution_tier for npc in _drive(snap, "Boris", turns=60)]

    rank: dict[str, int] = {}
    for tier in tiers:
        if tier not in rank:
            rank[tier] = len(rank)
    ranks = [rank[t] for t in tiers]
    assert ranks == sorted(ranks), f"resolution_tier de-escalated across turns: {tiers}"
    assert tiers[0] != "spawn" or rank.get("spawn") == 0
    assert len(rank) >= 2, f"tier never advanced beyond a single band: {set(tiers)}"


def test_tier_boundary_is_crisp() -> None:
    """At the turn the tier first changes, the prior turn was still the old
    tier — escalation happens on a single defined boundary, not a smear.
    Discovers the boundary empirically (no hardcoded threshold)."""
    snap = _snapshot(Npc(core=_core("Boris")))
    tiers = [npc.resolution_tier for npc in _drive(snap, "Boris", turns=60)]

    first_change = next((i for i in range(1, len(tiers)) if tiers[i] != tiers[i - 1]), None)
    assert first_change is not None, f"tier never changed in 60 turns: {set(tiers)}"
    assert tiers[first_change - 1] == "spawn", (
        "tier changed but the turn before the boundary was not 'spawn' — "
        f"escalation is not crisp: {tiers[: first_change + 1]}"
    )
    assert tiers[first_change] != "spawn"


def test_top_tier_saturates_without_error() -> None:
    """A heavily-engaged NPC reaches the top of the ladder and stays there —
    no overflow, no exception, no de-escalation."""
    snap = _snapshot(Npc(core=_core("Boris")))
    tiers = [npc.resolution_tier for npc in _drive(snap, "Boris", turns=200)]
    assert tiers[-1] == tiers[-2], f"tier still churning at saturation: {tiers[-5:]}"


# ===========================================================================
# AC3 — Disposition drifts emergently on sustained engagement (ADR-020)
# ===========================================================================


def test_sustained_engagement_warms_disposition() -> None:
    snap = _snapshot(Npc(core=_core("Boris"), disposition=0))
    steps = _drive(snap, "Boris", turns=8)
    values = [int(npc.disposition) for npc in steps]
    assert values[-1] > 0, f"disposition never warmed across engagement: {values}"
    # Monotonic non-decreasing in the documented (positive) drift direction.
    assert values == sorted(values), f"disposition drift was not monotonic: {values}"


def test_disposition_drift_respects_plus_100_clamp() -> None:
    snap = _snapshot(Npc(core=_core("Boris"), disposition=99))
    steps = _drive(snap, "Boris", turns=10)
    values = [int(npc.disposition) for npc in steps]
    assert max(values) <= 100, f"disposition exceeded the +-100 clamp: {values}"
    assert values[-1] == 100, (
        f"sustained warming from 99 never saturated at the +100 clamp: {values}"
    )


def test_band_derivation_honors_genre_configured_thresholds() -> None:
    """Band reads must go through ``Disposition.attitude()`` so a pack's custom
    ``friendly_at`` is honored — never a hardcoded +-10. With friendly_at=2,
    the attitude must flip to FRIENDLY the moment value exceeds 2 (it would
    still read NEUTRAL under the default friendly_at=10)."""
    configure_attitude_thresholds(AttitudeThresholds(friendly_at=2, hostile_at=-2))
    snap = _snapshot(Npc(core=_core("Boris"), disposition=0))
    steps = _drive(snap, "Boris", turns=15)

    for npc in steps:
        value = int(npc.disposition)
        is_friendly = npc.disposition.attitude() == Attitude.FRIENDLY
        assert is_friendly == (value > 2), (
            f"attitude band ignores the configured friendly_at=2 boundary at "
            f"value={value}: attitude={npc.disposition.attitude()!r}"
        )
    # Drift must actually have pushed the NPC past the configured boundary,
    # otherwise the invariant above is vacuously satisfied at value 0.
    assert int(steps[-1].disposition) > 2
    assert steps[-1].disposition.attitude() == Attitude.FRIENDLY


def test_disposition_does_not_drift_without_a_stateful_npc() -> None:
    """A ``pool_hit`` engagement has no ``Npc`` — there is nothing to drift,
    and no Npc may be spuriously created to carry a disposition."""
    snap = _snapshot()
    snap.npc_pool.append(NpcPoolMember(name="Marya", drawn_from="legacy_registry"))
    for t in range(1, 6):
        _engage(snap, "Marya", turn=t)
    assert snap.npcs == []


# ===========================================================================
# AC4 — Development-tick OTEL span fires on the increment / escalation legs
# ===========================================================================


def _dev_spans(exporter: InMemorySpanExporter) -> list:
    return [
        s for s in exporter.get_finished_spans() if _DEV_SPAN_COUNT_ATTR in dict(s.attributes or {})
    ]


def test_development_tick_span_fires_on_engagement(otel_capture) -> None:
    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Boris", turn=4)

    spans = _dev_spans(otel_capture)
    assert len(spans) == 1, (
        "expected exactly one development-tick span carrying "
        f"{_DEV_SPAN_COUNT_ATTR!r}; got names="
        f"{[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    span = spans[0]
    attrs = dict(span.attributes or {})
    assert span.name.startswith("npc."), (
        f"dev-tick span name {span.name!r} should follow the npc.* convention "
        f"(recommended {RECOMMENDED_DEV_SPAN_NAME!r})"
    )
    # ``npc_name`` not the OTEL-reserved ``name`` attribute.
    assert attrs.get("npc_name") == "Boris"
    assert "name" not in attrs or attrs.get("name") != "Boris"
    assert attrs.get(_DEV_SPAN_COUNT_ATTR) == 1
    assert attrs.get("resolution_tier_before") == "spawn"
    assert attrs.get("resolution_tier_after") == "spawn"


def test_development_tick_span_fires_even_without_tier_change(otel_capture) -> None:
    """The GM panel must see the engine *counting*, not only the rarer
    escalations — a sub-threshold tick still emits, with old == new tier."""
    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Boris", turn=1)

    spans = _dev_spans(otel_capture)
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["resolution_tier_before"] == attrs["resolution_tier_after"] == "spawn"
    assert attrs[_DEV_SPAN_COUNT_ATTR] == 1


def test_development_tick_span_records_tier_change_on_escalation(otel_capture) -> None:
    snap = _snapshot(Npc(core=_core("Boris")))
    for t in range(1, 41):
        _engage(snap, "Boris", turn=t)

    spans = _dev_spans(otel_capture)
    changed = [
        dict(s.attributes or {})
        for s in spans
        if dict(s.attributes or {}).get("resolution_tier_before")
        != dict(s.attributes or {}).get("resolution_tier_after")
    ]
    assert changed, "no development-tick span ever recorded a tier escalation (before != after)"
    first = changed[0]
    assert first["resolution_tier_before"] == "spawn"
    assert first["resolution_tier_after"] != "spawn"


# ---- AC4 dedicated WIRING test: tick reaches the GM panel via the hub ------


async def _hub_setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Local watcher hub + monkeypatched tracer (pattern from
    tests/integration/test_npc_edge_publish_wiring.py)."""
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


@pytest.mark.asyncio
async def test_development_tick_reaches_watcher_hub_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring guard: driving a real ``npcs_hit`` engagement must surface a
    routed ``state_transition`` event carrying the development-tick payload
    on the GM-panel hub. Proves the new span registers a SpanRoute and is
    reachable from production ``_apply_npc_mentions`` — refactor-stable, no
    source grep (server CLAUDE.md)."""
    captured = await _hub_setup(monkeypatch, "test-npc-development-wiring")

    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Boris", turn=4)
    await asyncio.sleep(0.05)

    dev_events = [
        e
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get(_DEV_SPAN_COUNT_ATTR) is not None
    ]
    assert len(dev_events) >= 1, (
        "no development-tick state_transition reached the hub — the tick span "
        "is unrouted or production never opened it. "
        f"captured event_types={[e.get('event_type') for e in captured]!r}"
    )
    fields = dev_events[0]["fields"]
    assert fields.get("npc_name") == "Boris"
    assert int(fields[_DEV_SPAN_COUNT_ATTR]) == 1


# ===========================================================================
# AC5 — Disposition-drift leg emits the existing disposition.shift contract
# ===========================================================================


def _shift_spans(exporter: InMemorySpanExporter) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == "disposition.shift"]


def test_disposition_drift_emits_shift_span_with_crossed_true(otel_capture) -> None:
    """A band-crossing drift fires ``disposition.shift`` with ``crossed=True``
    and matching before/after attitudes — the exact contract reused from the
    narrator ``npc_attitudes`` path (session.py:1416)."""
    configure_attitude_thresholds(AttitudeThresholds(friendly_at=2, hostile_at=-2))
    snap = _snapshot(Npc(core=_core("Boris"), disposition=0))
    for t in range(1, 13):
        _engage(snap, "Boris", turn=t)

    crossed = [s for s in _shift_spans(otel_capture) if dict(s.attributes or {}).get("crossed")]
    assert crossed, "no disposition.shift span fired with crossed=True across a band crossing"
    attrs = dict(crossed[0].attributes or {})
    assert attrs.get("npc_name") == "Boris"
    assert attrs.get("before_attitude") == "neutral"
    assert attrs.get("after_attitude") == "friendly"
    assert int(attrs.get("after", 0)) > int(attrs.get("before", 0))


def test_intra_band_drift_emits_shift_span_with_crossed_false(otel_capture) -> None:
    """Intra-band drift is still observable: a single small warm from neutral
    fires the span with ``crossed=False`` and a non-zero delta."""
    snap = _snapshot(Npc(core=_core("Boris"), disposition=0))
    _engage(snap, "Boris", turn=1)

    spans = _shift_spans(otel_capture)
    assert len(spans) == 1, (
        f"intra-band drift produced {len(spans)} disposition.shift spans (expected exactly 1)"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("crossed") is False
    assert int(attrs.get("delta", 0)) != 0
    assert attrs.get("before_attitude") == attrs.get("after_attitude") == "neutral"


def test_clamped_engagement_emits_no_phantom_shift(otel_capture) -> None:
    """At the +100 clamp the value cannot move — the GM panel must not see a
    phantom shift. Either no span, or a span with delta==0/crossed==False.
    The counter still increments (the engagement is real)."""
    snap = _snapshot(Npc(core=_core("Boris"), disposition=100))
    _engage(snap, "Boris", turn=1)

    assert snap.npcs[0].non_transactional_interactions == 1  # engagement is real
    for span in _shift_spans(otel_capture):
        attrs = dict(span.attributes or {})
        assert int(attrs.get("delta", 0)) == 0, "phantom disposition.shift at the clamp"
        assert attrs.get("crossed") is False


# ===========================================================================
# AC6 — Transactional-only paths remain unchanged (regression guard)
# ===========================================================================


def test_unmentioned_npc_is_not_developed() -> None:
    """The tick is gated to the engaged NPC only — a co-present NPC the
    narrator did not cite this turn must not increment or escalate."""
    snap = _snapshot(Npc(core=_core("Boris")), Npc(core=_core("Clara")))
    _engage(snap, "Boris", turn=1)
    clara = next(n for n in snap.npcs if n.core.name == "Clara")
    boris = next(n for n in snap.npcs if n.core.name == "Boris")
    assert boris.non_transactional_interactions == 1  # engaged → developed
    assert clara.non_transactional_interactions == 0  # untouched
    assert clara.resolution_tier == "spawn"


def test_materialized_npc_baseline_is_undeveloped() -> None:
    """A freshly-materialized Npc (as the transactional promotion/MM paths
    build it) carries the undeveloped baseline — the tick adds the interest
    leg, it does not pre-seed it onto materialization."""
    npc = Npc(core=_core("Crawling Scavenger"), creature_id="scavenger", threat_level=1)
    assert npc.non_transactional_interactions == 0
    assert npc.resolution_tier == "spawn"


def test_no_engagement_no_tick(otel_capture) -> None:
    """An apply call with no resolvable ``npcs_hit`` mention fires no
    development-tick span at all."""
    snap = _snapshot(Npc(core=_core("Boris")))
    _engage(snap, "Stranger", turn=1)  # invented — never resolves to Boris
    assert _dev_spans(otel_capture) == []
    assert snap.npcs[0].non_transactional_interactions == 0
