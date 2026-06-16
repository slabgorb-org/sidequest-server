"""Rig-pool binding — wire a vessel inventory item into a RigComposurePool.

Story 53-2, Epic 53 (Road Warrior). The materializer's binding helper:

  1. Scans ``core.inventory.items`` for a ``vessel``-tagged item.
  2. Parses its composure / composure_max tags.
  3. Instantiates a :class:`RigComposurePool` bound to
     ``(character_id, item['id'])``.
  4. Assigns the pool to ``core.rig_pool`` (new optional field on
     :class:`CreatureCore`).
  5. Returns the pool (or ``None`` if no vessel item was present).

Covered ACs: 2 (instantiate + bind), 3 (snapshot round-trip on CreatureCore),
4 (``rig_pool.created`` span fires via the existing ``model_post_init``), 6
(no silent fallback on malformed vessel item).

These tests are RED until Dev wires the binding helper and extends
:class:`CreatureCore`.
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
    """Install an in-memory span exporter for the rig.* spans."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _vessel_item_dict(
    *,
    item_id: str = "rig_tier_1_prospect",
    composure: int = 4,
    composure_max: int = 4,
) -> dict:
    return {
        "id": item_id,
        "name": "Prospect Rig",
        "category": "vessel",
        "tags": [
            "vessel",
            "rig",
            "tier-1",
            f"composure:{composure}",
            f"composure_max:{composure_max}",
        ],
    }


def _non_vessel_item_dict(item_id: str = "tool_kit") -> dict:
    return {
        "id": item_id,
        "name": "Tool Kit",
        "category": "tool",
        "tags": ["tool", "kit"],
    }


def _core_with_items(items: list[dict], *, name: str = "Mira"):
    """Build a CreatureCore with the given inventory items.

    Kept inside the test function via local import so the test file can
    load even before Dev extends CreatureCore — pytest collection still
    finds tests, they just fail at call time (the desired RED state).
    """
    from sidequest.game import CreatureCore, Inventory

    return CreatureCore(
        name=name,
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(items=list(items)),
        statuses=[],
        acquired_advancements=[],
    )


# ---------------------------------------------------------------------------
# Import wiring — the binding helpers must be reachable from production paths.
# ---------------------------------------------------------------------------


def test_bind_rig_pool_from_inventory_importable() -> None:
    """The per-character binding helper is part of the public API surface."""
    from sidequest.game import bind_rig_pool_from_inventory  # noqa: F401


def test_bind_rig_pools_importable() -> None:
    """The snapshot-walker variant is also public — production callers
    use it from chargen-complete / session-start handlers (mirrors the
    :func:`rebind_chassis_bonds_to_character` pattern)."""
    from sidequest.game import bind_rig_pools  # noqa: F401


def test_find_vessel_item_importable() -> None:
    """Inventory scan helper is public so non-binding callers (e.g. UI
    state mirror) can answer 'does this character have a rig?' without
    pulling the binding logic."""
    from sidequest.game import find_vessel_item  # noqa: F401


# ---------------------------------------------------------------------------
# CreatureCore extension — optional rig_pool field.
# ---------------------------------------------------------------------------


def test_creature_core_accepts_optional_rig_pool() -> None:
    """CreatureCore exposes an optional ``rig_pool`` field, default None.

    Story 53-1 left CreatureCore at edge-only; 53-2 adds the second pool
    so the snapshot can carry vessel state through the save round-trip.
    """
    from sidequest.game import CreatureCore, Inventory

    core = CreatureCore(
        name="Mira",
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        acquired_advancements=[],
    )
    assert core.rig_pool is None


def test_creature_core_strict_extra_forbid_still_holds() -> None:
    """Adding rig_pool must NOT relax the strict-extra rule for save safety."""
    from sidequest.game import CreatureCore, Inventory

    with pytest.raises(ValidationError):
        CreatureCore(
            name="Mira",
            description="A driver.",
            personality="Watchful.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            acquired_advancements=[],
            unexpected_field="boom",  # type: ignore[call-arg]
        )


def test_creature_core_round_trip_preserves_rig_pool() -> None:
    """``model_dump`` → ``model_validate`` preserves the bound pool.

    AC3: snapshot round-trip. CreatureCore is the carrier; if dump/validate
    drops rig_pool the save file silently loses vessel state on reload.
    """
    from sidequest.game import CreatureCore, Inventory, RigComposurePool

    pool = RigComposurePool(
        current=4,
        max=4,
        base_max=4,
        character_id="Mira",
        chassis_id="rig_tier_1_prospect",
    )
    core = CreatureCore(
        name="Mira",
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        acquired_advancements=[],
        rig_pool=pool,
    )

    revived = CreatureCore.model_validate(core.model_dump())

    assert revived.rig_pool is not None
    assert revived.rig_pool.current == 4
    assert revived.rig_pool.max == 4
    assert revived.rig_pool.character_id == "Mira"
    assert revived.rig_pool.chassis_id == "rig_tier_1_prospect"


def test_creature_core_json_round_trip_preserves_rig_pool() -> None:
    """JSON round-trip mirrors the dict round-trip (matches SQLite path)."""
    from sidequest.game import CreatureCore, Inventory, RigComposurePool

    pool = RigComposurePool(
        current=2,
        max=6,
        base_max=6,
        character_id="Mira",
        chassis_id="rig_tier_2_initiate",
    )
    core = CreatureCore(
        name="Mira",
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        acquired_advancements=[],
        rig_pool=pool,
    )

    revived = CreatureCore.model_validate_json(core.model_dump_json())

    assert revived.rig_pool is not None
    assert revived.rig_pool.current == 2
    assert revived.rig_pool.max == 6
    assert revived.rig_pool.chassis_id == "rig_tier_2_initiate"


def test_creature_core_round_trip_with_no_rig_pool_stays_none() -> None:
    """A non-rig character round-trips with ``rig_pool=None`` preserved."""
    from sidequest.game import CreatureCore, Inventory

    core = CreatureCore(
        name="Mira",
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        acquired_advancements=[],
    )

    revived = CreatureCore.model_validate(core.model_dump())
    assert revived.rig_pool is None


# ---------------------------------------------------------------------------
# find_vessel_item — pre-filter for the binder.
# ---------------------------------------------------------------------------


def test_find_vessel_item_returns_the_vessel_when_present() -> None:
    from sidequest.game import find_vessel_item

    items = [_non_vessel_item_dict(), _vessel_item_dict(), _non_vessel_item_dict("medkit")]

    result = find_vessel_item(items)

    assert result is not None
    assert result["id"] == "rig_tier_1_prospect"


def test_find_vessel_item_returns_none_when_no_vessel() -> None:
    from sidequest.game import find_vessel_item

    items = [_non_vessel_item_dict(), _non_vessel_item_dict("medkit")]
    assert find_vessel_item(items) is None


def test_find_vessel_item_returns_none_on_empty_inventory() -> None:
    from sidequest.game import find_vessel_item

    assert find_vessel_item([]) is None


def test_find_vessel_item_first_wins_when_multiple_vessels_present() -> None:
    """Story 53-2 assumption: one rig per character. If content drift puts
    two vessel items in inventory, the first one wins — deterministic, not
    silent. (Multi-rig scenarios are a separate story.)
    """
    from sidequest.game import find_vessel_item

    first = _vessel_item_dict(item_id="rig_tier_1_prospect")
    second = _vessel_item_dict(item_id="rig_tier_2_initiate", composure_max=6)

    result = find_vessel_item([first, second])

    assert result is not None
    assert result["id"] == "rig_tier_1_prospect"


# ---------------------------------------------------------------------------
# bind_rig_pool_from_inventory — the core binding helper.
# ---------------------------------------------------------------------------


def test_bind_rig_pool_attaches_to_core_when_vessel_present() -> None:
    """AC2: vessel item present → ``core.rig_pool`` is a bound pool."""
    from sidequest.game import bind_rig_pool_from_inventory

    core = _core_with_items([_vessel_item_dict(composure=4, composure_max=4)])

    pool = bind_rig_pool_from_inventory(core, character_id="Mira")

    assert pool is not None
    assert core.rig_pool is pool  # same instance — not a copy
    assert pool.character_id == "Mira"
    assert pool.chassis_id == "rig_tier_1_prospect"
    assert pool.current == 4
    assert pool.max == 4
    assert pool.base_max == 4


def test_bind_rig_pool_preserves_partial_composure() -> None:
    """A vessel item already damaged (composure < max) materializes
    with the damaged value — the materializer is NOT a healer."""
    from sidequest.game import bind_rig_pool_from_inventory

    core = _core_with_items([_vessel_item_dict(composure=1, composure_max=4)])

    pool = bind_rig_pool_from_inventory(core, character_id="Mira")

    assert pool is not None
    assert pool.current == 1
    assert pool.max == 4
    assert pool.base_max == 4


def test_bind_rig_pool_returns_none_when_no_vessel_item() -> None:
    """No vessel in inventory → returns None, leaves core.rig_pool as None."""
    from sidequest.game import bind_rig_pool_from_inventory

    core = _core_with_items([_non_vessel_item_dict(), _non_vessel_item_dict("medkit")])

    result = bind_rig_pool_from_inventory(core, character_id="Mira")

    assert result is None
    assert core.rig_pool is None


def test_bind_rig_pool_returns_none_on_empty_inventory() -> None:
    from sidequest.game import bind_rig_pool_from_inventory

    core = _core_with_items([])
    assert bind_rig_pool_from_inventory(core, character_id="Mira") is None
    assert core.rig_pool is None


def test_bind_rig_pool_is_idempotent_when_pool_already_present() -> None:
    """A second call on a core that already has rig_pool is a no-op.

    Snapshot reload path: when a save file is rehydrated, ``core.rig_pool``
    is already populated by pydantic. Re-running the materializer must
    NOT clobber the live (possibly-damaged) pool with a fresh full-composure
    one from the tags.
    """
    from sidequest.game import (
        RigComposurePool,
        bind_rig_pool_from_inventory,
    )

    existing = RigComposurePool(
        current=1,  # damaged
        max=4,
        base_max=4,
        character_id="Mira",
        chassis_id="rig_tier_1_prospect",
    )
    core = _core_with_items([_vessel_item_dict(composure=4, composure_max=4)])
    core.rig_pool = existing

    result = bind_rig_pool_from_inventory(core, character_id="Mira")

    # Returns the existing pool (or None — Dev's choice), but MUST NOT
    # replace it with a fresh full-composure pool.
    assert core.rig_pool is existing
    assert core.rig_pool.current == 1  # damaged value preserved
    # If the helper returns the existing pool on no-op, that's fine too.
    assert result is None or result is existing


def test_bind_rig_pool_raises_on_malformed_vessel_tags() -> None:
    """AC6: vessel item missing ``composure:N`` → loud failure.

    No silent skip, no default-to-zero. The binding helper surfaces the
    parser's ``InvalidVesselTagsError`` (or a wrapping subclass) so the
    chargen flow can fail and prompt for content fix.
    """
    from sidequest.game import (
        InvalidVesselTagsError,
        bind_rig_pool_from_inventory,
    )

    malformed = {
        "id": "rig_broken",
        "name": "Broken Rig",
        "category": "vessel",
        "tags": ["vessel", "rig", "composure_max:4"],  # no composure:N
    }
    core = _core_with_items([malformed])

    with pytest.raises(InvalidVesselTagsError):
        bind_rig_pool_from_inventory(core, character_id="Mira")

    # And the core stays clean — partial binding is worse than no binding.
    assert core.rig_pool is None


def test_bind_rig_pool_requires_non_blank_character_id() -> None:
    """Blank ``character_id`` would propagate into the pool's binding
    validator anyway, but rejecting at the binder gives a clearer error."""
    from sidequest.game import bind_rig_pool_from_inventory

    core = _core_with_items([_vessel_item_dict()])

    with pytest.raises((ValueError, ValidationError)):
        bind_rig_pool_from_inventory(core, character_id="")


# ---------------------------------------------------------------------------
# OTEL — AC4: rig_pool.created span fires when the binder instantiates the pool.
# ---------------------------------------------------------------------------


def test_bind_rig_pool_emits_rig_pool_created_span(monkeypatch) -> None:
    """AC4: binding a vessel item fires the existing ``rig_pool.created``
    span (emitted by :meth:`RigComposurePool.model_post_init`).

    The materializer does NOT need a new span — the model already emits
    on construction, and the binder constructs the model.
    """
    from sidequest.game import bind_rig_pool_from_inventory
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CREATED

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _core_with_items([_vessel_item_dict(composure=4, composure_max=4)])
    bind_rig_pool_from_inventory(core, character_id="Mira")

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_CREATED]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs["character_id"] == "Mira"
    assert attrs["chassis_id"] == "rig_tier_1_prospect"
    assert attrs["current"] == 4
    assert attrs["max"] == 4


def test_bind_rig_pool_does_not_emit_span_when_no_vessel(monkeypatch) -> None:
    """No vessel in inventory → no pool construction → no span.

    The GM panel uses span absence to verify a character genuinely has
    no rig (vs. silent binding failure). Phantom spans would defeat
    that distinction.
    """
    from sidequest.game import bind_rig_pool_from_inventory
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CREATED

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _core_with_items([_non_vessel_item_dict()])
    bind_rig_pool_from_inventory(core, character_id="Mira")

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_CREATED]
    assert matching == []


def test_bind_rig_pool_does_not_emit_span_on_idempotent_call(monkeypatch) -> None:
    """Re-binding a core that already has rig_pool must not re-fire ``created``.

    Otherwise every snapshot reload would emit a phantom creation event
    and pollute the GM panel.
    """
    from sidequest.game import (
        RigComposurePool,
        bind_rig_pool_from_inventory,
    )
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CREATED

    existing = RigComposurePool(
        current=4,
        max=4,
        base_max=4,
        character_id="Mira",
        chassis_id="rig_tier_1_prospect",
    )
    core = _core_with_items([_vessel_item_dict()])
    core.rig_pool = existing

    # Install exporter AFTER seeding so the seed's span is not captured.
    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    bind_rig_pool_from_inventory(core, character_id="Mira")

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_CREATED]
    assert matching == []
