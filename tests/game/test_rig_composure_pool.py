"""RigComposurePool — vessel-attached pool extending the EdgePool framework.

Story 53-1, foundation for Epic 53 (Road Warrior). Mirrors the EdgePool API
(``current`` / ``max`` / ``base_max``, ``apply_delta``, clamping at ``[0, max]``,
strict ``extra='forbid'`` for save-file safety) and adds vessel binding:

  - ``character_id`` — the character controlling the rig
  - ``chassis_id`` — the rig vessel instance (see :mod:`sidequest.game.chassis`)

The pool also detects zero-crossings on the downward edge so the 53-3 crash
handler can fire injury tags and Edge loss WITHOUT this story owning crash
logic. ``is_destroyed`` returns the current snapshot; ``apply_delta`` returns
the result object so callers can distinguish "took damage but still alive"
from "this delta destroyed the rig".

Per CLAUDE.md OTEL principle, every state mutation emits a span:
  - ``rig_pool.created`` — at construction time
  - ``rig_pool.delta`` — on every apply_delta call
  - ``rig_pool.zero_crossing`` — only on the downward transition to current=0

The crash handler (story 53-3) listens to ``rig_pool.zero_crossing`` to fire
injury tags + Edge hits + dismount. RigComposurePool itself stays inert —
it reports the crossing and lets a downstream subsystem decide consequences.

Tests are RED until Dev implements the class.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import ValidationError


def _fresh_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Install an in-memory span exporter and return (provider, exporter)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


# ---------------------------------------------------------------------------
# Import wiring: RigComposurePool must be reachable from production paths.
# ---------------------------------------------------------------------------


def test_rig_composure_pool_imports_from_game_package() -> None:
    """Production code paths must reach RigComposurePool via ``sidequest.game``.

    Per CLAUDE.md "Every Test Suite Needs a Wiring Test": this asserts the
    class is re-exported from the package root, not just hidden inside its
    own module. Dev chooses the file location; the import surface is fixed.
    """
    from sidequest.game import RigComposurePool  # noqa: F401


def test_rig_composure_pool_in_game_package_all() -> None:
    """``RigComposurePool`` listed in ``sidequest.game.__all__``."""
    import sidequest.game as game_pkg

    assert "RigComposurePool" in game_pkg.__all__


def test_rig_pool_delta_result_exported() -> None:
    """The delta result type is also part of the public surface."""
    from sidequest.game import RigComposureDeltaResult  # noqa: F401


# ---------------------------------------------------------------------------
# Construction + invariants
# ---------------------------------------------------------------------------


def test_rig_composure_pool_constructs_with_required_fields() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=20,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    assert pool.current == 20
    assert pool.max == 20
    assert pool.base_max == 20
    assert pool.character_id == "player_character_1"
    assert pool.chassis_id == "kestrel"


def test_rig_composure_pool_requires_character_id() -> None:
    """character_id is mandatory — no default — to prevent unbound pools."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(  # type: ignore[call-arg]
            current=20, max=20, base_max=20, chassis_id="kestrel"
        )


def test_rig_composure_pool_requires_chassis_id() -> None:
    """chassis_id is mandatory — no default — the pool tracks a specific rig."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(  # type: ignore[call-arg]
            current=20, max=20, base_max=20, character_id="player_character_1"
        )


def test_rig_composure_pool_rejects_blank_character_id() -> None:
    """An empty character_id is a bound-to-nothing pool — must fail loud."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(
            current=20,
            max=20,
            base_max=20,
            character_id="",
            chassis_id="kestrel",
        )


def test_rig_composure_pool_rejects_blank_chassis_id() -> None:
    """An empty chassis_id breaks the materializer→pool binding."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(
            current=20,
            max=20,
            base_max=20,
            character_id="player_character_1",
            chassis_id="",
        )


def test_rig_composure_pool_strict_extra_forbid() -> None:
    """Mirrors EdgePool/ResourcePool: malformed save data must fail loud.

    CLAUDE.md "No Silent Fallbacks" — pydantic ``extra='forbid'`` is the
    project-wide pattern for save-surface models.
    """
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(  # type: ignore[call-arg]
            current=20,
            max=20,
            base_max=20,
            character_id="player_character_1",
            chassis_id="kestrel",
            mystery_field=True,
        )


def test_rig_composure_pool_rejects_negative_current() -> None:
    """Construction with a negative ``current`` is a malformed save."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(
            current=-1,
            max=20,
            base_max=20,
            character_id="player_character_1",
            chassis_id="kestrel",
        )


def test_rig_composure_pool_rejects_current_above_max() -> None:
    """Construction with ``current > max`` is a malformed save."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(
            current=25,
            max=20,
            base_max=20,
            character_id="player_character_1",
            chassis_id="kestrel",
        )


def test_rig_composure_pool_rejects_zero_or_negative_max() -> None:
    """``max <= 0`` would make ``is_destroyed`` true at construction —
    fail loud rather than ship a born-dead rig."""
    from sidequest.game import RigComposurePool

    with pytest.raises(ValidationError):
        RigComposurePool(
            current=0,
            max=0,
            base_max=0,
            character_id="player_character_1",
            chassis_id="kestrel",
        )


# ---------------------------------------------------------------------------
# apply_delta — gain, loss, clamping, return shape
# ---------------------------------------------------------------------------


def test_rig_composure_pool_apply_positive_delta_increases_current() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=10,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(5)
    assert pool.current == 15
    assert result.new_current == 15
    assert result.old_current == 10


def test_rig_composure_pool_apply_negative_delta_decreases_current() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=10,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(-3)
    assert pool.current == 7
    assert result.new_current == 7
    assert result.old_current == 10


def test_rig_composure_pool_clamps_to_max_on_positive_overflow() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=18,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(50)
    assert pool.current == 20
    assert result.new_current == 20


def test_rig_composure_pool_floors_at_zero_on_negative_overflow() -> None:
    """Composure cannot go below 0 — that's the zero-crossing boundary."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=5,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(-100)
    assert pool.current == 0
    assert result.new_current == 0


def test_rig_composure_pool_zero_delta_is_noop_in_value() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=10,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(0)
    assert pool.current == 10
    assert result.new_current == 10
    assert result.old_current == 10
    assert result.zero_crossed is False


# ---------------------------------------------------------------------------
# Zero-crossing detection (53-1 detects; 53-3 acts on it)
# ---------------------------------------------------------------------------


def test_rig_composure_pool_zero_crossing_flagged_when_current_reaches_zero() -> None:
    """Going from a positive value to 0 fires zero_crossed=True.

    This is the signal 53-3's crash handler subscribes to. RigComposurePool
    itself MUST NOT apply damage, injury tags, or Edge loss — it just reports.
    """
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=3,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(-3)
    assert pool.current == 0
    assert result.zero_crossed is True


def test_rig_composure_pool_zero_crossing_flagged_on_overflow_below_zero() -> None:
    """Massive negative delta still crosses zero (floored, but crossing detected)."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=15,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(-500)
    assert pool.current == 0
    assert result.zero_crossed is True


def test_rig_composure_pool_no_crossing_when_damage_does_not_reach_zero() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=20,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(-5)
    assert pool.current == 15
    assert result.zero_crossed is False


def test_rig_composure_pool_no_crossing_when_already_at_zero() -> None:
    """A pool already at 0 hit by more damage does NOT re-fire zero-crossing.

    The crash handler should fire once per destruction event, not every
    turn the rig stays wrecked. Re-crossing requires repair → re-zero.
    """
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=0,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(-5)
    assert pool.current == 0
    assert result.zero_crossed is False


def test_rig_composure_pool_no_crossing_when_healing_through_zero_upward() -> None:
    """Upward crossing from 0 back to positive is repair, not crash.

    Only the downward edge fires zero_crossed.
    """
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=0,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    result = pool.apply_delta(10)
    assert pool.current == 10
    assert result.zero_crossed is False


def test_rig_composure_pool_recrosses_after_repair_and_redestruction() -> None:
    """Heal-then-destroy cycle re-arms the zero-crossing signal."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=0,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    # Repair
    repair = pool.apply_delta(10)
    assert repair.zero_crossed is False
    # Re-destruction
    redestroy = pool.apply_delta(-15)
    assert pool.current == 0
    assert redestroy.zero_crossed is True


# ---------------------------------------------------------------------------
# is_destroyed snapshot
# ---------------------------------------------------------------------------


def test_rig_composure_pool_is_destroyed_returns_true_at_zero() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=0,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    assert pool.is_destroyed() is True


def test_rig_composure_pool_is_destroyed_returns_false_above_zero() -> None:
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=1,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    assert pool.is_destroyed() is False


# ---------------------------------------------------------------------------
# No damage application — 53-1 strictly detects; 53-3 acts
# ---------------------------------------------------------------------------


def test_rig_composure_pool_apply_delta_does_not_mutate_character_or_chassis_id() -> None:
    """Reaching zero must NOT clear the binding fields — the crash handler
    needs them to fire injury tags on the bound character (53-3)."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=5,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    pool.apply_delta(-100)
    assert pool.character_id == "player_character_1"
    assert pool.chassis_id == "kestrel"


# ---------------------------------------------------------------------------
# Serialization round-trip — 53-2 materializer + session persistence
# ---------------------------------------------------------------------------


def test_rig_composure_pool_round_trip_via_model_dump_and_validate() -> None:
    """Save → load must be lossless. 53-2 materializer depends on this."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=12,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    dumped = pool.model_dump()
    restored = RigComposurePool.model_validate(dumped)
    assert restored.current == 12
    assert restored.max == 20
    assert restored.base_max == 20
    assert restored.character_id == "player_character_1"
    assert restored.chassis_id == "kestrel"


def test_rig_composure_pool_round_trip_via_json() -> None:
    """Save-file JSON round-trip — the realistic persistence path."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=7,
        max=15,
        base_max=15,
        character_id="player_character_1",
        chassis_id="coyote",
    )
    raw = pool.model_dump_json()
    restored = RigComposurePool.model_validate_json(raw)
    assert restored == pool


def test_rig_composure_pool_dump_contains_all_binding_fields() -> None:
    """Persistence must include character_id + chassis_id so the loader
    can reattach the pool to the right rig + character (53-2 seam)."""
    from sidequest.game import RigComposurePool

    pool = RigComposurePool(
        current=10,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )
    dumped = pool.model_dump()
    assert dumped["character_id"] == "player_character_1"
    assert dumped["chassis_id"] == "kestrel"
    assert dumped["current"] == 10
    assert dumped["max"] == 20
    assert dumped["base_max"] == 20


# ---------------------------------------------------------------------------
# OTEL spans — created / delta / zero_crossing
# ---------------------------------------------------------------------------


def test_rig_pool_span_constants_exist() -> None:
    from sidequest.telemetry.spans import (
        SPAN_RIG_POOL_CREATED,
        SPAN_RIG_POOL_DELTA,
        SPAN_RIG_POOL_ZERO_CROSSING,
    )

    assert SPAN_RIG_POOL_CREATED == "rig_pool.created"
    assert SPAN_RIG_POOL_DELTA == "rig_pool.delta"
    assert SPAN_RIG_POOL_ZERO_CROSSING == "rig_pool.zero_crossing"


def test_rig_pool_spans_are_routed_state_transition() -> None:
    """Story 53-4 (ADR-031): the three pool-lifecycle spans are routed to
    typed ``state_transition`` events with ``component="rig"`` so the GM
    panel sees them on the Subsystems tab rather than only as generic
    ``agent_span_close`` events on the firehose.

    Before 53-4 these were FLAT_ONLY (the 53-1 ship state). The migration
    to ``SPAN_ROUTES`` is the wiring this story owns.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_RIG_POOL_CREATED,
        SPAN_RIG_POOL_DELTA,
        SPAN_RIG_POOL_ZERO_CROSSING,
        SPAN_ROUTES,
    )

    for name in (SPAN_RIG_POOL_CREATED, SPAN_RIG_POOL_DELTA, SPAN_RIG_POOL_ZERO_CROSSING):
        assert name in SPAN_ROUTES, f"{name!r} must be registered in SPAN_ROUTES"
        assert name not in FLAT_ONLY_SPANS, (
            f"{name!r} must not be in FLAT_ONLY_SPANS now that it is routed"
        )
        route = SPAN_ROUTES[name]
        assert route.event_type == "state_transition", (
            f"{name!r} route.event_type should be 'state_transition' "
            f"(audio.py / chargen.py precedent), got {route.event_type!r}"
        )
        assert route.component == "rig", (
            f"{name!r} route.component should be 'rig', got {route.component!r}"
        )


def test_rig_pool_construction_emits_created_span(monkeypatch) -> None:
    """Constructing a RigComposurePool fires ``rig_pool.created``.

    Per CLAUDE.md OTEL principle: every subsystem decision emits a span.
    The GM panel uses this to verify pools are actually being instantiated
    (vs. narrator improvising rig damage on a pool that doesn't exist).
    """
    from sidequest.game import RigComposurePool
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CREATED

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    RigComposurePool(
        current=20,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_CREATED]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs["character_id"] == "player_character_1"
    assert attrs["chassis_id"] == "kestrel"
    assert attrs["current"] == 20
    assert attrs["max"] == 20


def test_rig_pool_apply_delta_emits_delta_span(monkeypatch) -> None:
    """Every apply_delta call fires ``rig_pool.delta`` with old/new + delta."""
    from sidequest.game import RigComposurePool
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import (
        SPAN_RIG_POOL_CREATED,
        SPAN_RIG_POOL_DELTA,
    )

    pool = RigComposurePool(
        current=20,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )

    # Install exporter AFTER construction so we only capture delta spans here.
    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    pool.apply_delta(-5)

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_DELTA]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs["character_id"] == "player_character_1"
    assert attrs["chassis_id"] == "kestrel"
    assert attrs["delta"] == -5
    assert attrs["old_current"] == 20
    assert attrs["new_current"] == 15

    # No zero-crossing span fired on a non-fatal hit.
    crossing = [s for s in finished if s.name == "rig_pool.zero_crossing"]
    assert crossing == []
    # And no spurious creation span re-fired.
    creates = [s for s in finished if s.name == SPAN_RIG_POOL_CREATED]
    assert creates == []


def test_rig_pool_zero_crossing_emits_zero_crossing_span(monkeypatch) -> None:
    """Downward-crossing-to-0 fires both ``rig_pool.delta`` AND
    ``rig_pool.zero_crossing`` so the crash handler (53-3) can subscribe
    to the dedicated channel without filtering every delta."""
    from sidequest.game import RigComposurePool
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import (
        SPAN_RIG_POOL_DELTA,
        SPAN_RIG_POOL_ZERO_CROSSING,
    )

    pool = RigComposurePool(
        current=4,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    pool.apply_delta(-10)

    finished = exporter.get_finished_spans()
    crossings = [s for s in finished if s.name == SPAN_RIG_POOL_ZERO_CROSSING]
    assert len(crossings) == 1
    attrs = crossings[0].attributes
    assert attrs["character_id"] == "player_character_1"
    assert attrs["chassis_id"] == "kestrel"
    assert attrs["old_current"] == 4
    # delta was -10 but current floored at 0 — the span reports the realized
    # damage, not the requested damage, so the crash handler has the truth.
    assert attrs["new_current"] == 0

    # Delta span also fired.
    deltas = [s for s in finished if s.name == SPAN_RIG_POOL_DELTA]
    assert len(deltas) == 1


def test_rig_pool_zero_crossing_span_does_not_fire_on_already_zero(monkeypatch) -> None:
    """No re-crossing → no zero_crossing span. Avoids spamming the crash
    handler every turn while the rig sits wrecked."""
    from sidequest.game import RigComposurePool
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_ZERO_CROSSING

    pool = RigComposurePool(
        current=0,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    pool.apply_delta(-5)

    finished = exporter.get_finished_spans()
    crossings = [s for s in finished if s.name == SPAN_RIG_POOL_ZERO_CROSSING]
    assert crossings == []


def test_rig_pool_zero_crossing_span_does_not_fire_on_upward_zero_crossing(
    monkeypatch,
) -> None:
    """Healing from 0 back to positive is repair, not crash — no span."""
    from sidequest.game import RigComposurePool
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_ZERO_CROSSING

    pool = RigComposurePool(
        current=0,
        max=20,
        base_max=20,
        character_id="player_character_1",
        chassis_id="kestrel",
    )

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    pool.apply_delta(10)

    finished = exporter.get_finished_spans()
    crossings = [s for s in finished if s.name == SPAN_RIG_POOL_ZERO_CROSSING]
    assert crossings == []
