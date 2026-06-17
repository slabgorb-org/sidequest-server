"""Story 22-4 — span-route static checks for the seed trope subsystem.

The story moves ``SPAN_SEED_DRAWN`` and ``SPAN_SEED_EXPIRED`` out of
``FLAT_ONLY_SPANS`` and into ``SPAN_ROUTES`` so the GM panel's typed
Subsystems feed surfaces seed lifecycle events as ``state_transition``
events alongside the trope-engine spans (45-27 pattern). Two new
constants are added: ``SPAN_SEED_FIRED`` (routed — per-seed surfacing
during narrator context build) and ``SPAN_SEED_PROMOTED`` (flat-only
scaffold, no call site until 22-5).

Without these route entries the watcher's ``on_end`` hook falls back to
firehose-only emission and the dashboard's Seed tab stays dark — the
GM panel cannot verify that the seed engine is engaged vs. the narrator
improvising.

Sibling of ``test_45_27_trope_span_routing.py`` — same shape, same
discipline. Static checks at unit-test speed; runtime emission is
covered by ``test_seed_fired_emission.py`` and the existing
``test_seed_expiry.py`` span assertions.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# AC1 — New span constants exist and are importable
# ---------------------------------------------------------------------------


def test_span_seed_fired_constant_is_defined() -> None:
    """``SPAN_SEED_FIRED`` must be importable from the spans package.
    Dev adds it to ``sidequest/telemetry/spans/seed.py`` and re-exports
    via ``__init__.py`` (star-import from ``.seed``).
    """
    from sidequest.telemetry.spans import SPAN_SEED_FIRED  # noqa: F811

    assert isinstance(SPAN_SEED_FIRED, str), (
        f"SPAN_SEED_FIRED must be a string constant; got {type(SPAN_SEED_FIRED)}"
    )
    assert SPAN_SEED_FIRED, "SPAN_SEED_FIRED must not be empty"


def test_span_seed_promoted_constant_is_defined() -> None:
    """``SPAN_SEED_PROMOTED`` must be importable — scaffolded now, wired
    in a future story (22-5 or later). The routing completeness test
    enforces that it appears in either ``SPAN_ROUTES`` or
    ``FLAT_ONLY_SPANS``; this test ensures the constant itself exists.
    """
    from sidequest.telemetry.spans import SPAN_SEED_PROMOTED  # noqa: F811

    assert isinstance(SPAN_SEED_PROMOTED, str)
    assert SPAN_SEED_PROMOTED, "SPAN_SEED_PROMOTED must not be empty"


# ---------------------------------------------------------------------------
# AC2 — GM Panel Routing: seed spans migrate from FLAT_ONLY to SPAN_ROUTES
# ---------------------------------------------------------------------------


def test_seed_drawn_migrated_from_flat_to_routed() -> None:
    """``seed.drawn`` was in ``FLAT_ONLY_SPANS`` (22-3 baseline). 22-4
    promotes it to ``SPAN_ROUTES`` for the GM panel's Seed subsystem tab.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_ROUTES,
        SPAN_SEED_DRAWN,
    )

    assert SPAN_SEED_DRAWN in SPAN_ROUTES, (
        f"{SPAN_SEED_DRAWN!r} not in SPAN_ROUTES — the GM panel's Seed "
        f"tab cannot surface draw events. Pre-22-4 it lived in "
        f"FLAT_ONLY_SPANS; move it to SPAN_ROUTES with a SpanRoute."
    )
    assert SPAN_SEED_DRAWN not in FLAT_ONLY_SPANS, (
        f"{SPAN_SEED_DRAWN!r} still in FLAT_ONLY_SPANS — duplicate "
        f"registration (both routed and flat) produces inconsistent "
        f"watcher behavior. Remove it from FLAT_ONLY_SPANS."
    )


def test_seed_expired_migrated_from_flat_to_routed() -> None:
    """``seed.expired`` was in ``FLAT_ONLY_SPANS`` (22-3 baseline). 22-4
    promotes it to ``SPAN_ROUTES`` for the GM panel's Seed subsystem tab.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_ROUTES,
        SPAN_SEED_EXPIRED,
    )

    assert SPAN_SEED_EXPIRED in SPAN_ROUTES, (
        f"{SPAN_SEED_EXPIRED!r} not in SPAN_ROUTES — the GM panel cannot "
        f"surface expiry events on the Seed tab."
    )
    assert SPAN_SEED_EXPIRED not in FLAT_ONLY_SPANS, (
        f"{SPAN_SEED_EXPIRED!r} still in FLAT_ONLY_SPANS — remove it "
        f"when adding the route (no dual membership)."
    )


def test_seed_fired_is_routed() -> None:
    """``SPAN_SEED_FIRED`` — new in 22-4 — fires per active seed during
    narrator context build. Must be routed (not flat-only) so the GM
    panel's Seed tab sees every surfacing event.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_ROUTES,
        SPAN_SEED_FIRED,
    )

    assert SPAN_SEED_FIRED in SPAN_ROUTES, (
        f"{SPAN_SEED_FIRED!r} not in SPAN_ROUTES — the GM panel cannot "
        f"verify that seeds are being surfaced to the narrator. Without "
        f"this route, seed injection is invisible."
    )
    assert SPAN_SEED_FIRED not in FLAT_ONLY_SPANS, (
        f"{SPAN_SEED_FIRED!r} must not be in FLAT_ONLY_SPANS — it needs "
        f"typed routing for the Seed subsystem tab."
    )


def test_seed_promoted_is_flat_only_pending_wiring() -> None:
    """``SPAN_SEED_PROMOTED`` is scaffolded now but has no call site
    until a future story (22-5 or later). It belongs in
    ``FLAT_ONLY_SPANS`` to satisfy the routing completeness lint without
    creating a dead route entry.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_ROUTES,
        SPAN_SEED_PROMOTED,
    )

    assert SPAN_SEED_PROMOTED in FLAT_ONLY_SPANS, (
        f"{SPAN_SEED_PROMOTED!r} not in FLAT_ONLY_SPANS — it has no "
        f"call site yet, so it should be flat-only. The routing "
        f"completeness test will fail if it's neither routed nor flat."
    )
    assert SPAN_SEED_PROMOTED not in SPAN_ROUTES, (
        f"{SPAN_SEED_PROMOTED!r} is in SPAN_ROUTES but has no call "
        f"site — creating a dead route entry is worse than flat-only. "
        f"Route it when a future story wires the call site."
    )


# ---------------------------------------------------------------------------
# AC2 — Route metadata: component and event_type consistency
# ---------------------------------------------------------------------------


def test_routed_seed_spans_share_seeds_component() -> None:
    """All three routed seed spans must use ``component='seeds'`` so the
    GM panel groups them under one Subsystems-tab predicate — sibling
    of the trope engine's ``component='tropes'`` discipline.
    """
    from sidequest.telemetry.spans import (
        SPAN_ROUTES,
        SPAN_SEED_DRAWN,
        SPAN_SEED_EXPIRED,
        SPAN_SEED_FIRED,
    )

    for span_name in (SPAN_SEED_DRAWN, SPAN_SEED_EXPIRED, SPAN_SEED_FIRED):
        route = SPAN_ROUTES.get(span_name)
        assert route is not None, f"{span_name!r} missing from SPAN_ROUTES"
        assert route.component == "seeds", (
            f"{span_name!r} route has component={route.component!r}, "
            f"expected 'seeds'. All seed spans must share the same "
            f"component for GM panel tab grouping."
        )


def test_routed_seed_spans_use_state_transition_event_type() -> None:
    """Seed lifecycle events are state transitions (draw, fire, expire)
    — same event_type as trope spans. The dashboard's typed Subsystems
    feed filters on this.
    """
    from sidequest.telemetry.spans import (
        SPAN_ROUTES,
        SPAN_SEED_DRAWN,
        SPAN_SEED_EXPIRED,
        SPAN_SEED_FIRED,
    )

    for span_name in (SPAN_SEED_DRAWN, SPAN_SEED_EXPIRED, SPAN_SEED_FIRED):
        route = SPAN_ROUTES.get(span_name)
        assert route is not None, f"{span_name!r} missing from SPAN_ROUTES"
        assert route.event_type == "state_transition", (
            f"{span_name!r} route has event_type={route.event_type!r}, expected 'state_transition'."
        )


# ---------------------------------------------------------------------------
# AC2 — Extractor field contracts: each route surfaces the right attrs
# ---------------------------------------------------------------------------


def test_seed_drawn_extractor_surfaces_expected_fields() -> None:
    """The ``seed.drawn`` route extractor must surface ``seed_id``,
    ``session_id``, and ``activated_at_turn`` — the three attributes
    the call site in ``ensure_initial_draw`` stamps on the span (22-3).
    The GM panel's Seed tab renders these as "seed X dealt at turn Y".
    """
    from sidequest.telemetry.spans import SPAN_ROUTES, SPAN_SEED_DRAWN

    route = SPAN_ROUTES[SPAN_SEED_DRAWN]

    class _FakeSpan:
        name = SPAN_SEED_DRAWN
        attributes = {
            "seed_id": "sealed_letter",
            "session_id": "session-alpha",
            "activated_at_turn": 0,
        }

    fields = route.extract(_FakeSpan())  # type: ignore[arg-type]
    assert fields.get("seed_id") == "sealed_letter", (
        f"Extractor must surface seed_id; got fields={fields}"
    )
    assert fields.get("session_id") == "session-alpha", (
        f"Extractor must surface session_id; got fields={fields}"
    )
    assert fields.get("activated_at_turn") == 0, (
        f"Extractor must surface activated_at_turn; got fields={fields}"
    )


def test_seed_expired_extractor_surfaces_expected_fields() -> None:
    """The ``seed.expired`` route extractor must surface ``seed_id``
    and ``expired_at_turn`` — the two attributes the call site in
    ``tick_seeds`` stamps on the span (22-3). The GM panel renders
    these as "seed X expired at turn Y".
    """
    from sidequest.telemetry.spans import SPAN_ROUTES, SPAN_SEED_EXPIRED

    route = SPAN_ROUTES[SPAN_SEED_EXPIRED]

    class _FakeSpan:
        name = SPAN_SEED_EXPIRED
        attributes = {
            "seed_id": "uneasy_innkeeper",
            "expired_at_turn": 12,
        }

    fields = route.extract(_FakeSpan())  # type: ignore[arg-type]
    assert fields.get("seed_id") == "uneasy_innkeeper", (
        f"Extractor must surface seed_id; got fields={fields}"
    )
    assert fields.get("expired_at_turn") == 12, (
        f"Extractor must surface expired_at_turn; got fields={fields}"
    )


def test_seed_fired_extractor_surfaces_seed_id() -> None:
    """The ``seed.fired`` route extractor must at minimum surface
    ``seed_id`` — the GM panel needs to know *which* seed was surfaced
    to the narrator. Additional attributes (turn number, active count)
    are at Dev's discretion.
    """
    from sidequest.telemetry.spans import SPAN_ROUTES, SPAN_SEED_FIRED

    route = SPAN_ROUTES[SPAN_SEED_FIRED]

    class _FakeSpan:
        name = SPAN_SEED_FIRED
        attributes = {
            "seed_id": "missing_portrait",
        }

    fields = route.extract(_FakeSpan())  # type: ignore[arg-type]
    assert fields.get("seed_id") == "missing_portrait", (
        f"Extractor must surface seed_id — the panel needs to attribute "
        f"each fired event to a specific seed; got fields={fields}"
    )


def test_seed_drawn_extractor_field_is_active_seeds() -> None:
    """``field='active_seeds'`` namespaces the typed event to the
    snapshot field the panel renders. Mirrors the trope engine's
    ``field='active_tropes'`` discipline.
    """
    from sidequest.telemetry.spans import SPAN_ROUTES, SPAN_SEED_DRAWN

    route = SPAN_ROUTES[SPAN_SEED_DRAWN]

    class _FakeSpan:
        name = SPAN_SEED_DRAWN
        attributes = {"seed_id": "x", "session_id": "s", "activated_at_turn": 0}

    fields = route.extract(_FakeSpan())  # type: ignore[arg-type]
    assert fields.get("field") == "active_seeds", (
        f"field={fields.get('field')!r}, expected 'active_seeds' for panel-side filtering."
    )
