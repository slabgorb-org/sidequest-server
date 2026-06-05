"""End-to-end wiring for the affinity tier-promotion engine (ADR-021 track 2).

Story 82-7. ``AffinityState`` (``tier``/``progress``) and ``Affinity.tier_thresholds``
already exist as live data, but nothing ever advances a character's affinity tier
from accumulated progress — it is P6-deferred dead data. This is the gap: a
runtime engine (``apply_affinity_tier_ups``) that consumes the live
``AffinityState.progress``, resolves the tier via :func:`resolve_affinity_tier`,
bumps ``AffinityState.tier`` on a real crossing, and emits an OTEL/watcher event
the GM panel can read (the lie-detector — mirroring the ``progression.level_up``
emit landed for the sibling track-1 story 82-6).

Same harness shape as ``tests/integration/test_levelup_otel_wiring.py``.

Contract pinned by these tests (mirror of 82-6):
- ``apply_affinity_tier_ups(snapshot, progression) -> list[AffinityTierUp]``
  lives in ``sidequest.server.dispatch.encounter_lifecycle``.
- Each crossing emits a ``state_transition`` watcher event with
  ``component="progression"`` and ``field="progression.affinity_tier_up"``
  carrying ``character_name``, ``affinity_id``, ``before``, ``after``, ``driver``.
- ``AffinityTierUp`` (``sidequest.protocol.models``) carries
  ``character_name``/``affinity_id``/``before``/``after``/``driver``.
- The engine clears + repopulates ``Character.last_affinity_tier_ups`` (a list —
  a character can advance several affinities in one turn).

RED: ``apply_affinity_tier_ups`` / ``AffinityTierUp`` /
``Character.last_affinity_tier_ups`` do not exist yet — this module fails to
import on current ``develop``.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import AffinityState, Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.progression import Affinity, ProgressionConfig
from sidequest.protocol.models import AffinityTierUp
from sidequest.server.dispatch.encounter_lifecycle import apply_affinity_tier_ups
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

# Field name on the progression state_transition watcher event. Mirrors
# ``progression.level_up`` (component=progression) from the sibling track-1 story.
AFFINITY_TIER_FIELD = "progression.affinity_tier_up"


def _make_pc(name: str, *, affinities: list[AffinityState] | None = None) -> Character:
    core = CreatureCore(
        name=name,
        description="x",
        personality="x",
        inventory=Inventory(),
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory=f"{name}",
        affinities=list(affinities or []),
    )


def _affinity(name: str, thresholds: list[int]) -> Affinity:
    return Affinity(name=name, description="x", tier_thresholds=thresholds)


def _progression(*defs: Affinity) -> ProgressionConfig:
    return ProgressionConfig(affinities=list(defs))


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
        await asyncio.sleep(0.01)  # poll the async publish queue
    raise AssertionError(
        f"Expected state_transition with field={field_value!r} within {timeout_s}s; "
        f"captured: {[(e.get('event_type'), e.get('fields', {}).get('field')) for e in captured]}"
    )


@pytest.mark.asyncio
async def test_tier_up_mutates_tier_and_publishes_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character whose affinity progress is past the top threshold is promoted
    and the engine emits a typed ``state_transition`` with
    ``component=progression`` and ``field=progression.affinity_tier_up`` carrying
    the affinity_id and before/after tiers, so the GM panel can confirm the
    subsystem engaged."""
    captured = await _setup(monkeypatch, "test-affinity-tier-wiring")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=0, progress=100.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    apply_affinity_tier_ups(snapshot, _progression(_affinity("fire", [10, 25, 50])))
    await asyncio.sleep(0)  # let the async watcher publish drain

    assert snapshot.characters[0].affinities[0].tier == 3, "must clamp to the top tier"

    evt = await _wait_for_event(captured, AFFINITY_TIER_FIELD)
    assert evt["component"] == "progression"
    assert evt["fields"]["character_name"] == "Rux"
    assert evt["fields"]["affinity_id"] == "fire"
    assert evt["fields"]["before"] == 0
    assert evt["fields"]["after"] == 3


@pytest.mark.asyncio
async def test_tier_up_returns_player_facing_delta_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine returns a structured ``AffinityTierUp`` — character, affinity,
    before, after, and the driver — so a player-facing surface can render *what
    advanced and why*, not just a silent tier bump. Distinct from the GM/OTEL
    emit above."""
    await _setup(monkeypatch, "test-affinity-tier-delta")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=0, progress=100.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    deltas = apply_affinity_tier_ups(snapshot, _progression(_affinity("fire", [10, 25, 50])))

    assert deltas, "a crossing must return at least one AffinityTierUp delta"
    delta = deltas[0]
    assert isinstance(delta, AffinityTierUp)
    assert delta.character_name == "Rux"
    assert delta.affinity_id == "fire"
    assert delta.before == 0
    assert delta.after == 3
    # Pin the exact driver value (lang-review #6: a bare truthy check would pass
    # for any non-empty string and miss a regression that renamed the driver).
    assert delta.driver == "affinity"


@pytest.mark.asyncio
async def test_below_first_threshold_is_silent_no_tier_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An affinity below its first threshold must NOT promote and must NOT emit
    a tier-up event — no phantom advancement (the lie-detector fires only on a
    real crossing)."""
    captured = await _setup(monkeypatch, "test-affinity-tier-noop")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=0, progress=5.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    deltas = apply_affinity_tier_ups(snapshot, _progression(_affinity("fire", [10, 25, 50])))
    await asyncio.sleep(0.05)

    assert snapshot.characters[0].affinities[0].tier == 0
    assert deltas == []
    assert not [e for e in captured if e.get("fields", {}).get("field") == AFFINITY_TIER_FIELD]


@pytest.mark.asyncio
async def test_unmatched_affinity_id_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Silent Fallbacks: a character whose ``AffinityState.affinity_id`` has
    no matching authored ``Affinity`` def must NOT be promoted against some other
    affinity's ladder — it is skipped cleanly, no crash, no phantom event. (The
    engine must not fabricate a tier for an affinity the pack never declared.)"""
    captured = await _setup(monkeypatch, "test-affinity-tier-unmatched")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=0, progress=100.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    # Only an "ice" ladder is authored — nothing matches the character's "fire".
    deltas = apply_affinity_tier_ups(snapshot, _progression(_affinity("ice", [10, 25, 50])))
    await asyncio.sleep(0.05)

    assert snapshot.characters[0].affinities[0].tier == 0, "unmatched affinity must not promote"
    assert deltas == []
    assert not [e for e in captured if e.get("fields", {}).get("field") == AFFINITY_TIER_FIELD]


@pytest.mark.asyncio
async def test_affinity_with_no_thresholds_never_promotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Silent Fallbacks: an authored affinity that declares no
    ``tier_thresholds`` has no ladder — even huge progress must not promote it,
    and no ZeroDivision/IndexError, no phantom event."""
    captured = await _setup(monkeypatch, "test-affinity-tier-noladder")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=0, progress=100.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    deltas = apply_affinity_tier_ups(snapshot, _progression(_affinity("fire", [])))
    await asyncio.sleep(0.05)

    assert snapshot.characters[0].affinities[0].tier == 0
    assert deltas == []
    assert not [e for e in captured if e.get("fields", {}).get("field") == AFFINITY_TIER_FIELD]


@pytest.mark.asyncio
async def test_already_at_top_tier_does_not_re_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-downgrade / no-re-fire guard (``new_tier <= before``): an affinity
    already AT the top tier with progress past the ceiling must NOT re-promote
    and must NOT emit a phantom event. (A ``<=`` → ``<`` regression would spam
    the GM panel with phantom crossings every turn for a maxed affinity.)"""
    captured = await _setup(monkeypatch, "test-affinity-tier-at-top")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=3, progress=100.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    deltas = apply_affinity_tier_ups(snapshot, _progression(_affinity("fire", [10, 25, 50])))
    await asyncio.sleep(0.05)

    assert snapshot.characters[0].affinities[0].tier == 3, "must not re-fire at the top tier"
    assert deltas == []
    assert not [e for e in captured if e.get("fields", {}).get("field") == AFFINITY_TIER_FIELD]


@pytest.mark.asyncio
async def test_last_affinity_tier_ups_cleared_on_a_later_no_crossing_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-turn notification is transient: a character who crossed on a prior
    call must have ``last_affinity_tier_ups`` reset to ``[]`` on a later call that
    produces no new crossing — otherwise a stale "your fire affinity advanced!"
    delta would re-surface every turn forever."""
    await _setup(monkeypatch, "test-affinity-tier-reset")

    pc = _make_pc("Rux", affinities=[AffinityState(affinity_id="fire", tier=0, progress=100.0)])
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])
    progression = _progression(_affinity("fire", [10, 25, 50]))

    # First call: a real crossing sets the player-facing delta list.
    first = apply_affinity_tier_ups(snapshot, progression)
    assert first, "first call must produce a crossing"
    assert len(pc.last_affinity_tier_ups) == 1
    assert pc.last_affinity_tier_ups[0].after == 3

    # Second call: already at the top → no new crossing → delta list cleared.
    second = apply_affinity_tier_ups(snapshot, progression)
    assert second == []
    assert pc.last_affinity_tier_ups == [], "stale deltas must be cleared on a no-crossing turn"


@pytest.mark.asyncio
async def test_multi_affinity_only_crossers_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character with several affinities: only the one whose progress crossed
    advances and emits — proving the per-affinity loop and correct ``affinity_id``
    attribution. The bystander affinity stays put."""
    captured = await _setup(monkeypatch, "test-affinity-tier-multi-affinity")

    pc = _make_pc(
        "Rux",
        affinities=[
            AffinityState(affinity_id="fire", tier=0, progress=100.0),
            AffinityState(affinity_id="ice", tier=0, progress=0.0),
        ],
    )
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[pc])

    deltas = apply_affinity_tier_ups(
        snapshot,
        _progression(_affinity("fire", [10, 25, 50]), _affinity("ice", [10, 25, 50])),
    )
    await asyncio.sleep(0.05)

    fire = next(a for a in pc.affinities if a.affinity_id == "fire")
    ice = next(a for a in pc.affinities if a.affinity_id == "ice")
    assert fire.tier == 3
    assert ice.tier == 0
    assert [d.affinity_id for d in deltas] == ["fire"]

    events = [e for e in captured if e.get("fields", {}).get("field") == AFFINITY_TIER_FIELD]
    assert len(events) == 1, f"exactly one affinity crossed; got {len(events)} events"
    assert events[0]["fields"]["affinity_id"] == "fire"


@pytest.mark.asyncio
async def test_multi_character_snapshot_promotes_each_pc_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine iterates every PC: in a party where one character's affinity
    crosses and another's doesn't, only the crosser advances and emits — proving
    the per-character loop (not a break/return after the first) and correct
    ``character_name`` attribution on the event."""
    captured = await _setup(monkeypatch, "test-affinity-tier-multi-pc")

    crosser = _make_pc(
        "Ritali", affinities=[AffinityState(affinity_id="fire", tier=0, progress=100.0)]
    )
    bystander = _make_pc(
        "Catalina", affinities=[AffinityState(affinity_id="fire", tier=0, progress=0.0)]
    )
    snapshot = GameSnapshot(genre_slug="elemental_harmony", characters=[crosser, bystander])

    deltas = apply_affinity_tier_ups(snapshot, _progression(_affinity("fire", [10, 25, 50])))
    await asyncio.sleep(0.05)

    assert crosser.affinities[0].tier == 3
    assert bystander.affinities[0].tier == 0
    assert [d.character_name for d in deltas] == ["Ritali"]

    events = [e for e in captured if e.get("fields", {}).get("field") == AFFINITY_TIER_FIELD]
    assert len(events) == 1, f"exactly one PC crossed; got {len(events)} events"
    assert events[0]["fields"]["character_name"] == "Ritali"
