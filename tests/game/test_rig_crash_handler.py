"""Rig crash handler — Composure→0 fires injury tag + Edge hit + dismount.

Story 53-3, Epic 53 (Road Warrior). The handler is the consequence layer
that subscribes to ``RigComposurePool``'s downward zero-crossing
detection. When a driver's rig wrecks:

  1. Driver Edge loses 1 (apply_delta(-1)).
  2. An "injury" status is appended to the character (severity Wound —
     persists across the scene per ADR-080 + road_warrior injury_system).
  3. A "dismounted" status is appended (severity Scar — content rules
     describe recovery as "a story arc, not a shopping trip").
  4. An OTEL ``rig_pool.crash_event`` span fires with rig + character
     identifiers plus optional ``location`` / ``attacker`` attributes
     (per ADR-031 + rules.yaml ``rig_composure_spec``).

Wiring story: ``apply_rig_damage`` is the production-facing seam that
combines ``RigComposurePool.apply_delta(-N)`` with the crash handler so
downstream callers (combat resolver, dogfight subsystem) get one entry
point. Damage that does NOT cross zero leaves the character mounted;
damage that crosses zero triggers all four consequences. Repeated damage
to an already-wrecked rig is idempotent — no double-crash, no double
injury, no second Edge hit.

These tests are RED until Dev implements ``sidequest/game/rig_crash.py``,
registers the ``SPAN_RIG_POOL_CRASH_EVENT`` constant in
``sidequest/telemetry/spans/rig.py``, and exports both symbols through
``sidequest.game.__init__``.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _fresh_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Install an in-memory span exporter for rig.* spans.

    Matches the pattern used by ``tests/telemetry/test_rig_spans.py``
    and ``tests/game/test_rig_pool_binding.py``.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _mounted_core(
    *,
    name: str = "Mira",
    composure: int = 4,
    composure_max: int = 4,
    edge_current: int = 5,
    edge_max: int = 5,
    chassis_id: str = "rig_tier_1_prospect",
):
    """Build a CreatureCore with a healthy rig pool and Edge pool.

    Local import so test collection passes even when the handler module
    has not yet been implemented (we WANT RED at call time, not at
    collection time).
    """
    from sidequest.game import CreatureCore, HpPool, Inventory, RigComposurePool

    pool = RigComposurePool(
        current=composure,
        max=composure_max,
        base_max=composure_max,
        character_id=name,
        chassis_id=chassis_id,
    )
    return CreatureCore(
        name=name,
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(
            current=edge_current,
            max=edge_max,
            base_max=edge_max,
        ),
        acquired_advancements=[],
        rig_pool=pool,
    )


# ---------------------------------------------------------------------------
# Import wiring — the handler + seam + span constant are reachable.
# ---------------------------------------------------------------------------


def test_handle_rig_crash_importable() -> None:
    """The crash handler is part of the public game-package API."""
    from sidequest.game import handle_rig_crash  # noqa: F401


def test_apply_rig_damage_importable() -> None:
    """The damage-routing seam is public so combat/dogfight code can call it
    without reimplementing the pool delta + crash branch."""
    from sidequest.game import apply_rig_damage  # noqa: F401


def test_rig_crash_result_importable() -> None:
    """``RigCrashResult`` is the typed return shape from the handler;
    downstream callers may want to inspect ``edge_after`` / ``chassis_id``
    for follow-on narration or telemetry."""
    from sidequest.game import RigCrashResult  # noqa: F401


def test_span_rig_pool_crash_event_constant_exposed() -> None:
    """The crash-event span constant is part of the telemetry catalog
    (mirrors ``SPAN_RIG_POOL_CREATED`` / ``..._DELTA`` / ``..._ZERO_CROSSING``)."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    assert SPAN_RIG_POOL_CRASH_EVENT == "rig_pool.crash_event"


def test_span_rig_pool_crash_event_is_routed_state_transition() -> None:
    """Story 53-4 (ADR-031): ``rig_pool.crash_event`` is routed to a typed
    ``state_transition`` event with ``component="rig"`` so the GM panel's
    Subsystems tab surfaces every crash deterministically — not just as
    an opaque ``agent_span_close`` on the firehose.

    Before 53-4 this lived in ``FLAT_ONLY_SPANS`` (the 53-3 ship state).
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_RIG_POOL_CRASH_EVENT,
        SPAN_ROUTES,
    )

    assert SPAN_RIG_POOL_CRASH_EVENT in SPAN_ROUTES, (
        f"{SPAN_RIG_POOL_CRASH_EVENT!r} must be registered in SPAN_ROUTES"
    )
    assert SPAN_RIG_POOL_CRASH_EVENT not in FLAT_ONLY_SPANS, (
        f"{SPAN_RIG_POOL_CRASH_EVENT!r} must not be in FLAT_ONLY_SPANS now that it is routed"
    )
    route = SPAN_ROUTES[SPAN_RIG_POOL_CRASH_EVENT]
    assert route.event_type == "state_transition", (
        f"route.event_type should be 'state_transition', got {route.event_type!r}"
    )
    assert route.component == "rig", f"route.component should be 'rig', got {route.component!r}"


# ---------------------------------------------------------------------------
# handle_rig_crash — direct calls on a destroyed rig.
# ---------------------------------------------------------------------------


def test_handle_rig_crash_applies_minus_one_edge_to_driver() -> None:
    """AC: ``apply -1 Edge ... to the driver`` (road_warrior rules.yaml)."""
    from sidequest.game import handle_rig_crash

    core = _mounted_core(composure=0, composure_max=4, edge_current=5, edge_max=5)

    handle_rig_crash(core)

    assert core.hp.current == 4


def test_handle_rig_crash_appends_injury_status_with_wound_severity() -> None:
    """AC: ``+ 1 injury tag to the driver``. Per road_warrior injury_system,
    injuries persist across the scene — Wound severity (clears at session
    end / rest), not Scratch (clears at scene end).
    """
    from sidequest.game import handle_rig_crash
    from sidequest.game.status import StatusSeverity

    core = _mounted_core(composure=0)

    handle_rig_crash(core)

    injury_statuses = [s for s in core.statuses if "injur" in s.text.lower()]
    assert len(injury_statuses) == 1
    assert injury_statuses[0].severity == StatusSeverity.Wound


def test_handle_rig_crash_appends_dismounted_status() -> None:
    """AC: ``write the dismounted status``. Per dismounted_rules: recovery
    is "a story arc, not a shopping trip" — Scar severity (persists until
    milestone), not Wound (clears at session end).
    """
    from sidequest.game import handle_rig_crash
    from sidequest.game.status import StatusSeverity

    core = _mounted_core(composure=0)

    handle_rig_crash(core)

    dismounted = [s for s in core.statuses if s.text == "dismounted"]
    assert len(dismounted) == 1
    assert dismounted[0].severity == StatusSeverity.Scar


def test_handle_rig_crash_returns_typed_result() -> None:
    """``RigCrashResult`` carries enough context for the caller (combat
    resolver, narrator integration) to follow up without re-reading the
    pool — at minimum the chassis_id, character_id, and the resulting
    driver Edge."""
    from sidequest.game import handle_rig_crash

    core = _mounted_core(
        name="Mira",
        composure=0,
        edge_current=5,
        chassis_id="rig_tier_2_initiate",
    )

    result = handle_rig_crash(core)

    assert result is not None
    assert result.chassis_id == "rig_tier_2_initiate"
    assert result.character_id == "Mira"
    assert result.edge_after == 4


def test_handle_rig_crash_is_noop_when_rig_pool_is_none() -> None:
    """A character with no rig in inventory has no rig_pool — the handler
    must NOT silently mutate Edge or append statuses on a foot soldier."""
    from sidequest.game import CreatureCore, HpPool, Inventory, handle_rig_crash

    core = CreatureCore(
        name="Mira",
        description="A foot soldier.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=5, max=5, base_max=5),
        acquired_advancements=[],
    )

    result = handle_rig_crash(core)

    assert result is None
    assert core.hp.current == 5
    assert core.statuses == []


def test_handle_rig_crash_is_noop_when_rig_still_has_composure() -> None:
    """A scratched-but-mounted rig does NOT trigger the crash. The handler
    keys off ``rig_pool.is_destroyed()`` so callers can pass in an
    arbitrary core without first checking the pool state themselves."""
    from sidequest.game import handle_rig_crash

    core = _mounted_core(composure=2, composure_max=4)

    result = handle_rig_crash(core)

    assert result is None
    assert core.hp.current == 5
    assert core.statuses == []


def test_handle_rig_crash_is_idempotent_when_already_dismounted() -> None:
    """A second call on a wrecked + dismounted character does NOT re-apply
    Edge damage or duplicate statuses. The handler detects the prior
    dismount via the status list (presence of "dismounted") rather than
    re-firing on every call.
    """
    from sidequest.game import handle_rig_crash

    core = _mounted_core(composure=0, edge_current=5)

    first = handle_rig_crash(core)
    assert first is not None
    edge_after_first = core.hp.current
    status_count_after_first = len(core.statuses)

    second = handle_rig_crash(core)

    assert second is None
    assert core.hp.current == edge_after_first
    assert len(core.statuses) == status_count_after_first


def test_handle_rig_crash_does_not_drop_existing_statuses() -> None:
    """The handler appends to ``core.statuses`` rather than replacing it.
    A driver already carrying a Burned scar must keep that scar after the
    crash adds injury + dismounted."""
    from sidequest.game import handle_rig_crash
    from sidequest.game.status import Status, StatusSeverity

    core = _mounted_core(composure=0)
    prior = Status(text="Burned", severity=StatusSeverity.Scar)
    core.statuses.append(prior)

    handle_rig_crash(core)

    assert prior in core.statuses
    # Plus the two crash statuses appended.
    assert any(s.text == "dismounted" for s in core.statuses)
    assert any("injur" in s.text.lower() for s in core.statuses)


# ---------------------------------------------------------------------------
# OTEL — rig_pool.crash_event span emission.
# ---------------------------------------------------------------------------


def test_handle_rig_crash_emits_crash_event_span(monkeypatch) -> None:
    """AC (rules.yaml rig_composure_spec): handler MUST emit OTEL span
    ``rig_pool.crash_event`` with ``{rig_slug, location, attacker}`` per
    ADR-031. ``rig_slug`` maps to ``chassis_id`` (the existing convention
    in rig_pool.* span attrs)."""
    from sidequest.game import handle_rig_crash
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _mounted_core(name="Mira", composure=0, chassis_id="rig_tier_1_prospect")
    handle_rig_crash(core, location="dust_canyon", attacker="raider_chief")

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs["character_id"] == "Mira"
    assert attrs["chassis_id"] == "rig_tier_1_prospect"
    assert attrs["location"] == "dust_canyon"
    assert attrs["attacker"] == "raider_chief"


def test_handle_rig_crash_span_captures_consequence_outcomes(monkeypatch) -> None:
    """Story 53-4 / ADR-031: the crash span must capture what was DECIDED,
    not just the inputs.

    Pre-53-4, the span carried ``character_id, chassis_id, location,
    attacker`` — all inputs. The three consequences (Edge -1, injury
    status appended, dismount status appended) were mutations on
    ``core`` but invisible on the span, so the GM dashboard could only
    see "a crash happened" without knowing the outcome.

    Per the Layer-2 Agent Telemetry contract in ADR-031 ("structured
    spans at every point where the system makes or executes a decision"
    with fields "what was decided"), the span attrs MUST now also carry:

    - ``edge_delta`` — the signed Edge change applied (``DRIVER_HP_HIT``)
    - ``edge_after`` — the driver's Edge after the hit lands
    - ``injury_status_text`` — the appended injury status text
    - ``dismounted_status_text`` — the appended dismount status text

    Sebastien's lie detector: these four attributes are what tell the
    operator that the consequences fired AS A MATTER OF FACT, separate
    from the narrator's prose about the crash.
    """
    from sidequest.game import handle_rig_crash
    from sidequest.game.rig_crash import (
        DISMOUNTED_STATUS_TEXT,
        DRIVER_HP_HIT,
        INJURY_STATUS_TEXT,
    )
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    # Edge starts at 5; after DRIVER_HP_HIT (currently -1) it lands at 4.
    core = _mounted_core(
        name="Mira",
        composure=0,
        edge_current=5,
        edge_max=5,
        chassis_id="rig_tier_1_prospect",
    )
    handle_rig_crash(core, location="dust_canyon", attacker="raider_chief")

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert len(matching) == 1, "exactly one crash_event span per crash"
    attrs = matching[0].attributes

    assert attrs["hp_delta"] == DRIVER_HP_HIT, (
        f"hp_delta must equal DRIVER_HP_HIT ({DRIVER_HP_HIT}), got {attrs.get('hp_delta')!r}"
    )
    assert attrs["hp_after"] == 5 + DRIVER_HP_HIT, (
        f"hp_after must reflect the post-crash HP value, got {attrs.get('hp_after')!r}"
    )
    assert attrs["injury_status_text"] == INJURY_STATUS_TEXT, (
        f"injury_status_text must match the appended status, got {attrs.get('injury_status_text')!r}"
    )
    assert attrs["dismounted_status_text"] == DISMOUNTED_STATUS_TEXT, (
        f"dismounted_status_text must match the appended status, got {attrs.get('dismounted_status_text')!r}"
    )


def test_handle_rig_crash_span_consequences_floor_edge_at_zero(monkeypatch) -> None:
    """Sebastien's edge case: a driver whose Edge is already 0 still
    triggers the crash sequence, but ``edge_after`` must NOT go negative
    — Edge pools floor at 0. The span must report ``edge_after=0`` and
    ``edge_delta`` equal to the *realized* delta (i.e., 0 — no Edge was
    actually subtracted because the floor swallowed it), not the
    requested ``DRIVER_HP_HIT``.

    Pinning this avoids a future "phantom hit" where the dashboard shows
    edge_delta=-1 against a character who had 0 Edge to lose.
    """
    from sidequest.game import handle_rig_crash
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _mounted_core(name="Mira", composure=0, edge_current=0, edge_max=5)
    handle_rig_crash(core)

    matching = [s for s in exporter.get_finished_spans() if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert len(matching) == 1
    attrs = matching[0].attributes
    assert attrs["hp_after"] == 0
    # Realized delta: the floor swallowed the requested -1, so the span
    # reports what actually happened to the Edge pool, not what was asked.
    assert attrs["hp_delta"] == 0, (
        "hp_delta on the span is the realized delta (after flooring), "
        f"not the requested DRIVER_HP_HIT — got {attrs.get('hp_delta')!r}"
    )


def test_handle_rig_crash_span_handles_none_location_and_attacker(monkeypatch) -> None:
    """Optional context attrs must be present even when callers do not
    supply them — OTEL attrs cannot be None, so the handler coerces to
    empty string per the magic.py / rig.py None-coercion precedent."""
    from sidequest.game import handle_rig_crash
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _mounted_core(composure=0)
    handle_rig_crash(core)

    matching = [s for s in exporter.get_finished_spans() if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert len(matching) == 1
    assert matching[0].attributes.get("location", "") == ""
    assert matching[0].attributes.get("attacker", "") == ""


def test_handle_rig_crash_does_not_emit_span_on_idempotent_call(monkeypatch) -> None:
    """A second call on a dismounted character must NOT pollute the GM
    panel with a phantom crash event — otherwise every snapshot reload
    of a wrecked rig would generate noise."""
    from sidequest.game import handle_rig_crash
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    core = _mounted_core(composure=0)
    handle_rig_crash(core)

    # Install exporter AFTER first call so only the second emission would
    # show up (mirrors test_bind_rig_pool_does_not_emit_span_on_idempotent_call).
    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    handle_rig_crash(core)

    matching = [s for s in exporter.get_finished_spans() if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert matching == []


def test_handle_rig_crash_does_not_emit_span_when_rig_not_destroyed(monkeypatch) -> None:
    """No crash → no crash_event span. The GM panel uses span absence to
    confirm a damage event was non-fatal."""
    from sidequest.game import handle_rig_crash
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _mounted_core(composure=3, composure_max=4)
    handle_rig_crash(core)

    matching = [s for s in exporter.get_finished_spans() if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert matching == []


# ---------------------------------------------------------------------------
# apply_rig_damage — production seam combining pool delta + crash handler.
# ---------------------------------------------------------------------------


def test_apply_rig_damage_sublethal_damage_does_not_fire_crash() -> None:
    """Non-fatal damage: rig takes a hit, character stays mounted, no
    crash consequences. ``RigDamageResult.crash`` is None."""
    from sidequest.game import apply_rig_damage

    core = _mounted_core(composure=4, composure_max=4, edge_current=5)

    result = apply_rig_damage(core, 2)

    assert result is not None
    assert result.crash is None
    assert core.rig_pool is not None
    assert core.rig_pool.current == 2
    assert core.hp.current == 5  # unchanged
    assert core.statuses == []


def test_apply_rig_damage_lethal_damage_fires_crash() -> None:
    """Damage that crosses zero fires the crash handler: Edge -1, injury
    status, dismounted status, crash result populated."""
    from sidequest.game import apply_rig_damage

    core = _mounted_core(composure=2, composure_max=4, edge_current=5)

    result = apply_rig_damage(core, 5, location="dust_canyon", attacker="raider_chief")

    assert result is not None
    assert result.crash is not None
    assert result.crash.character_id == "Mira"
    assert result.crash.chassis_id == "rig_tier_1_prospect"
    assert core.rig_pool is not None
    assert core.rig_pool.current == 0
    assert core.hp.current == 4
    assert any(s.text == "dismounted" for s in core.statuses)
    assert any("injur" in s.text.lower() for s in core.statuses)


def test_apply_rig_damage_exact_overkill_fires_crash() -> None:
    """Damage equal to remaining composure also fires the crash — the
    zero-crossing detection in RigComposurePool is ``new_current == 0``."""
    from sidequest.game import apply_rig_damage

    core = _mounted_core(composure=2, composure_max=4)

    result = apply_rig_damage(core, 2)

    assert result is not None
    assert result.crash is not None
    assert core.rig_pool is not None
    assert core.rig_pool.current == 0


def test_apply_rig_damage_to_already_wrecked_rig_does_not_re_crash() -> None:
    """A character already dismounted takes further "rig damage" with no
    practical effect — the rig is already at 0, so the zero-crossing
    detector does not re-fire, and the crash handler's idempotency guard
    holds. Edge does NOT take a second -1."""
    from sidequest.game import apply_rig_damage

    core = _mounted_core(composure=0, edge_current=5)
    # Pre-seed dismounted (as if a prior crash already ran).
    from sidequest.game.status import Status, StatusSeverity

    core.statuses.append(Status(text="dismounted", severity=StatusSeverity.Scar))

    result = apply_rig_damage(core, 3)

    assert result is not None
    assert result.crash is None
    assert core.hp.current == 5  # NO additional Edge hit
    # Status list unchanged (still just the one dismounted entry).
    assert sum(1 for s in core.statuses if s.text == "dismounted") == 1


def test_apply_rig_damage_returns_none_when_no_rig_pool() -> None:
    """A character without a rig has no pool to damage. The seam returns
    None rather than silently routing damage to Edge — the caller chose
    ``apply_rig_damage`` explicitly, so failing to find a rig is a no-op
    signal, not a fallback. ``apply_damage`` is the right tool for raw
    Edge damage."""
    from sidequest.game import CreatureCore, HpPool, Inventory, apply_rig_damage

    core = CreatureCore(
        name="Mira",
        description="A foot soldier.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=5, max=5, base_max=5),
        acquired_advancements=[],
    )

    result = apply_rig_damage(core, 3)

    assert result is None
    assert core.hp.current == 5  # unchanged


def test_apply_rig_damage_rejects_negative_amount() -> None:
    """Damage amount is positive — negative would be healing, which has
    a separate API. Fail loud (no silent fallback to abs())."""
    from sidequest.game import apply_rig_damage

    core = _mounted_core()

    with pytest.raises(ValueError):
        apply_rig_damage(core, -1)


def test_apply_rig_damage_zero_amount_is_noop_with_no_crash() -> None:
    """A zero-damage call is a deliberate no-op — Edge / statuses /
    composure all unchanged, and no crash span fires. (The pool's
    apply_delta(0) will still fire its rig_pool.delta span, but the crash
    branch must NOT activate.)"""
    from sidequest.game import apply_rig_damage

    core = _mounted_core(composure=4, edge_current=5)

    result = apply_rig_damage(core, 0)

    assert result is not None
    assert result.crash is None
    assert core.rig_pool is not None
    assert core.rig_pool.current == 4
    assert core.hp.current == 5


def test_apply_rig_damage_fires_crash_event_span_on_lethal_hit(monkeypatch) -> None:
    """End-to-end wiring: the damage seam → pool delta → crash handler →
    crash_event span. This is the integration test that proves the three
    layers are wired (CLAUDE.md "Every Test Suite Needs a Wiring Test")."""
    from sidequest.game import apply_rig_damage
    from sidequest.telemetry import spans as _spans
    from sidequest.telemetry.spans import (
        SPAN_RIG_POOL_CRASH_EVENT,
        SPAN_RIG_POOL_ZERO_CROSSING,
    )

    provider, exporter = _fresh_provider()
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    core = _mounted_core(composure=1)
    apply_rig_damage(core, 5, location="dust_canyon", attacker="raider_chief")

    finished = exporter.get_finished_spans()
    # Pool emits zero_crossing AND the handler emits crash_event.
    zero_crossings = [s for s in finished if s.name == SPAN_RIG_POOL_ZERO_CROSSING]
    crash_events = [s for s in finished if s.name == SPAN_RIG_POOL_CRASH_EVENT]
    assert len(zero_crossings) == 1
    assert len(crash_events) == 1
    crash_attrs = crash_events[0].attributes
    assert crash_attrs["character_id"] == "Mira"
    assert crash_attrs["location"] == "dust_canyon"
    assert crash_attrs["attacker"] == "raider_chief"


# ---------------------------------------------------------------------------
# Python lang-review rules (gates/lang-review/python.md).
# ---------------------------------------------------------------------------


def test_rig_crash_module_exports_all_public_symbols() -> None:
    """lang-review #10: public modules declare ``__all__`` for clear API.
    The rig_crash module is a fresh public module — must declare its
    surface explicitly."""
    import sidequest.game.rig_crash as rig_crash_module

    assert hasattr(rig_crash_module, "__all__")
    exported = set(rig_crash_module.__all__)
    # Must export the production-facing surface; helper internals may stay private.
    assert "handle_rig_crash" in exported
    assert "apply_rig_damage" in exported
    assert "RigCrashResult" in exported


def test_handle_rig_crash_has_type_annotations() -> None:
    """lang-review #3: public functions at module boundaries MUST have
    type annotations. ``handle_rig_crash`` is public — verify the
    signature carries them."""
    import inspect

    from sidequest.game import handle_rig_crash

    sig = inspect.signature(handle_rig_crash)
    for name, param in sig.parameters.items():
        assert param.annotation is not inspect.Parameter.empty, (
            f"parameter {name!r} on handle_rig_crash is missing a type annotation"
        )
    assert sig.return_annotation is not inspect.Signature.empty


def test_apply_rig_damage_has_type_annotations() -> None:
    """lang-review #3: same check for the damage seam."""
    import inspect

    from sidequest.game import apply_rig_damage

    sig = inspect.signature(apply_rig_damage)
    for name, param in sig.parameters.items():
        assert param.annotation is not inspect.Parameter.empty, (
            f"parameter {name!r} on apply_rig_damage is missing a type annotation"
        )
    assert sig.return_annotation is not inspect.Signature.empty
