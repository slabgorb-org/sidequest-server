"""Threshold-crossing fields on ``SPAN_DISPOSITION_SHIFT`` (story 50-11).

The GM panel can see numeric ``before``/``after``/``delta`` (story 50-9's
cold-subsystem promotion), but it cannot tell whether a shift *crossed
a band boundary* — e.g. neutral→friendly — without re-deriving the band
client-side. Story 50-11 extends the existing span with three fields:

- ``before_attitude``: attitude band string before the shift
- ``after_attitude``: attitude band string after the shift
- ``crossed``: True iff the bands differ

Bands follow ADR-020's three-tier mapping (strict boundaries):

- ``disposition > 10`` → ``"friendly"``
- ``disposition < -10`` → ``"hostile"``
- otherwise → ``"neutral"``

Story 50-10 centralised the attitude derivation into ``Attitude`` enum +
``Disposition.attitude()``. The source of truth is now
``Disposition.attitude()``; the legacy ``disposition_attitude`` helper
has been removed in 50-10 and these tests assert directly against the
``Attitude`` enum's ``.value`` literals, which the wiring tests in
``tests/game/test_disposition_call_site_migration.py`` enforce as the
only production-callable path.

These tests assert on the watcher-emitted event fields (the
``state_transition`` payload reachable by the GM panel), not on the raw
OTEL span — that's the user-visible surface and the integration boundary
that matters.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, WorldStatePatch
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


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


def _make_npc(name: str, disposition: int) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=disposition,
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


def _apply_shift(npc_name: str, *, before: int, delta: int) -> tuple[GameSnapshot, int]:
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npcs=[_make_npc(npc_name, disposition=before)],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={npc_name: delta}))
    return snapshot, int(snapshot.npcs[0].disposition)


# ---------------------------------------------------------------------------
# AC1 / AC3 — span carries before_attitude, after_attitude, crossed (strings + bool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disposition_shift_emits_attitude_strings_and_crossed_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher-visible state_transition event for ``disposition.shift``
    must include ``before_attitude``, ``after_attitude``, and ``crossed``."""
    captured = await _setup(monkeypatch, "test-disp-thresh-fields-present")
    _apply_shift("Bartender", before=10, delta=5)  # 10 (neutral) → 15 (friendly)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    fields = evt["fields"]

    # AC1: three new fields present
    assert "before_attitude" in fields, f"missing before_attitude: {fields!r}"
    assert "after_attitude" in fields, f"missing after_attitude: {fields!r}"
    assert "crossed" in fields, f"missing crossed: {fields!r}"

    # AC3: attitudes are strings (the enum's string representation)
    assert isinstance(fields["before_attitude"], str)
    assert isinstance(fields["after_attitude"], str)
    # crossed is a boolean — not 0/1, not "true"
    assert isinstance(fields["crossed"], bool), (
        f"crossed must be bool, got {type(fields['crossed']).__name__}: {fields['crossed']!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — crossed=True when the band changes; False when it doesn't
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neutral_to_friendly_marks_crossed_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 → 15 crosses the upper boundary into the friendly band."""
    captured = await _setup(monkeypatch, "test-disp-thresh-neutral-friendly")
    _apply_shift("Guard", before=10, delta=5)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "neutral"
    assert evt["fields"]["after_attitude"] == "friendly"
    assert evt["fields"]["crossed"] is True


@pytest.mark.asyncio
async def test_neutral_to_hostile_marks_crossed_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """-10 → -15 crosses the lower boundary into the hostile band."""
    captured = await _setup(monkeypatch, "test-disp-thresh-neutral-hostile")
    _apply_shift("Thief", before=-10, delta=-5)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "neutral"
    assert evt["fields"]["after_attitude"] == "hostile"
    assert evt["fields"]["crossed"] is True


@pytest.mark.asyncio
async def test_friendly_to_hostile_two_band_jump_still_crossed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single large shift that skips the neutral band still counts
    as crossed — the field is a band-identity flag, not a step counter."""
    captured = await _setup(monkeypatch, "test-disp-thresh-two-band-jump")
    _apply_shift("Rival", before=15, delta=-40)  # 15 (friendly) → -25 (hostile)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "friendly"
    assert evt["fields"]["after_attitude"] == "hostile"
    assert evt["fields"]["crossed"] is True


@pytest.mark.asyncio
async def test_shift_within_friendly_band_marks_crossed_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15 → 25 stays inside friendly; numeric delta is non-trivial but
    no band crossed."""
    captured = await _setup(monkeypatch, "test-disp-thresh-within-friendly")
    _apply_shift("Patron", before=15, delta=10)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "friendly"
    assert evt["fields"]["after_attitude"] == "friendly"
    assert evt["fields"]["crossed"] is False


@pytest.mark.asyncio
async def test_shift_within_neutral_band_marks_crossed_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 → 8 stays neutral. Neither boundary touched."""
    captured = await _setup(monkeypatch, "test-disp-thresh-within-neutral")
    _apply_shift("Stranger", before=5, delta=3)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "neutral"
    assert evt["fields"]["after_attitude"] == "neutral"
    assert evt["fields"]["crossed"] is False


@pytest.mark.asyncio
async def test_back_crossing_friendly_to_neutral_marks_crossed_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11 → 10 is a one-point downshift that crosses back into neutral.
    Bands flip → crossed=True regardless of delta magnitude."""
    captured = await _setup(monkeypatch, "test-disp-thresh-back-crossing")
    _apply_shift("Mentor", before=11, delta=-1)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "friendly"
    assert evt["fields"]["after_attitude"] == "neutral"
    assert evt["fields"]["crossed"] is True


@pytest.mark.asyncio
async def test_clamped_at_bound_uses_clamped_attitude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """before=95 (friendly), delta=+50 clamps to 100 (still friendly).
    after_attitude must reflect the clamped value, crossed must be False."""
    captured = await _setup(monkeypatch, "test-disp-thresh-clamp")
    _apply_shift("Ally", before=95, delta=50)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    # numeric fields stay aligned with the existing contract
    assert evt["fields"]["before"] == 95
    assert evt["fields"]["after"] == 100
    # band stayed friendly across the clamp
    assert evt["fields"]["before_attitude"] == "friendly"
    assert evt["fields"]["after_attitude"] == "friendly"
    assert evt["fields"]["crossed"] is False


# ---------------------------------------------------------------------------
# AC6 — derived from the *band*, not from "abs(delta) > threshold"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_delta_within_same_band_does_not_set_crossed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20 → 35 has a numeric delta of 15 (larger than the ±10 threshold
    constant). A naive implementation written as
    ``crossed = abs(delta) > 10`` would mark this True — but both
    endpoints are squarely inside the friendly band, so the correct
    answer is False. This test is the canary against the magic-number
    trap."""
    captured = await _setup(monkeypatch, "test-disp-thresh-large-delta-same-band")
    _apply_shift("Patron2", before=20, delta=15)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "friendly"
    assert evt["fields"]["after_attitude"] == "friendly"
    assert evt["fields"]["crossed"] is False, (
        "crossed must be derived from band identity, not |delta| vs a "
        "hardcoded constant — a 15-point delta inside the friendly band "
        "is not a crossing"
    )


@pytest.mark.asyncio
async def test_tiny_delta_across_boundary_sets_crossed_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 → 11 has a numeric delta of 1. A naive
    ``crossed = abs(delta) > 10`` would mark this False — but the
    bands flip neutral→friendly, so crossed must be True. The other
    canary."""
    captured = await _setup(monkeypatch, "test-disp-thresh-tiny-delta-across")
    _apply_shift("Tipsy", before=10, delta=1)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == "neutral"
    assert evt["fields"]["after_attitude"] == "friendly"
    assert evt["fields"]["crossed"] is True, (
        "crossed must be True for a 1-point delta that flips the band; "
        "abs(delta) < threshold is not a safe predicate"
    )


# ---------------------------------------------------------------------------
# AC3 — attitude strings match the existing helper's vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attitude_strings_match_attitude_enum_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strings emitted must match the ``Attitude`` enum's vocabulary.
    A refactor that renames a member's value from ``"friendly"`` to
    ``"warm"`` (or otherwise drifts the wire contract) is caught here."""
    from sidequest.game.disposition import Attitude

    captured = await _setup(monkeypatch, "test-disp-thresh-vocab")
    _apply_shift("Vocab", before=10, delta=5)  # neutral → friendly
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before_attitude"] == Attitude.NEUTRAL.value
    assert evt["fields"]["after_attitude"] == Attitude.FRIENDLY.value
    # Locked literal strings — the wire contract the GM panel matches on.
    assert evt["fields"]["before_attitude"] == "neutral"
    assert evt["fields"]["after_attitude"] == "friendly"


# ---------------------------------------------------------------------------
# AC5 — emitted at the same callsite (no double-fire)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_shift_emits_exactly_one_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``npc_attitudes`` patch entry must produce exactly one
    ``disposition.shift`` state_transition event. Adding the threshold
    fields must not duplicate the emission."""
    captured = await _setup(monkeypatch, "test-disp-thresh-single-emit")
    _apply_shift("Single", before=0, delta=20)  # neutral → friendly
    await asyncio.sleep(0)
    # Give any spurious duplicates a chance to land
    await asyncio.sleep(0.05)

    shift_events = [
        e
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get("field") == "disposition.shift"
    ]
    assert len(shift_events) == 1, (
        f"expected exactly 1 disposition.shift event, got {len(shift_events)}: {shift_events!r}"
    )


# ---------------------------------------------------------------------------
# AC1 — numeric fields preserved alongside new fields (no regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_numeric_fields_preserved_alongside_new_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding ``before_attitude``/``after_attitude``/``crossed`` must not
    drop the existing ``before``/``after``/``delta``/``npc_name`` fields
    that 50-9 already wires for the GM panel."""
    captured = await _setup(monkeypatch, "test-disp-thresh-numeric-preserved")
    _apply_shift("Both", before=8, delta=4)  # neutral → friendly (12)
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    fields = evt["fields"]
    assert fields["npc_name"] == "Both"
    assert fields["before"] == 8
    assert fields["after"] == 12
    assert fields["delta"] == 4
    assert fields["before_attitude"] == "neutral"
    assert fields["after_attitude"] == "friendly"
    assert fields["crossed"] is True
