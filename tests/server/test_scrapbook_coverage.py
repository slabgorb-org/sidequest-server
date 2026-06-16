"""Unit + span tests for the scrapbook-coverage gap detector (Story 45-10).

Regression evidence (Playtest 3, 2026-04-19): Orin's session covered 29
narrative rounds but only 10 had scrapbook entries — the other 19 were
invisible to the subsystem that injects "what happened in scene N" into
narrator recap and the GM-panel timeline. The cause was benign (save
predated scrapbook subsystem); the damage was real (silent invented
continuity). This file pins the detector that flags that gap loudly.

Design doc: `sprint/context/context-story-45-10.md`. Behavior: warn-only,
read-only — no backfill, no mutations. Two spans:

- ``scrapbook.coverage_evaluated``  — fires every resume (Sebastien's
  negative-confirmation requirement per CLAUDE.md OTEL principle).
- ``scrapbook.coverage_gap_detected`` — fires only when ``gap_count > 0``,
  carries ``gap_rounds``.

Plus a watcher event ``scrapbook_coverage_gap`` so the GM panel surfaces
the gap visibly.

These tests fail until:
1. ``sidequest/game/scrapbook_coverage.py`` exists with
   ``detect_scrapbook_coverage_gaps`` and ``ScrapbookCoverageReport``.
2. ``sidequest/telemetry/spans/scrapbook.py`` registers
   ``SPAN_SCRAPBOOK_COVERAGE_EVALUATED`` and
   ``SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED`` in ``SPAN_ROUTES``.
"""

from __future__ import annotations

import json as _json
import tempfile
from dataclasses import fields, is_dataclass
from typing import Any, get_type_hints

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def watcher_capture(monkeypatch):
    """Capture every ``_watcher_publish`` call into a list of dicts.

    The detector's gap-path publishes a ``scrapbook_coverage_gap`` event
    via the same ``_watcher_publish`` helper used by ``emit_scrapbook_entry``
    (``server/emitters.py:342``). Tests assert publish-or-no-publish per
    AC2 / AC1.
    """
    captured: list[dict[str, Any]] = []

    def _capture(field: str, payload: dict[str, Any], **kwargs: Any) -> None:
        captured.append({"field": field, "payload": payload, **kwargs})

    # Patch wherever the helper imports the watcher publish symbol.
    # The detector is required to use the same import surface as
    # ``emitters.py`` (single source of truth).
    monkeypatch.setattr(
        "sidequest.game.scrapbook_coverage._watcher_publish",
        _capture,
        raising=True,
    )
    return captured


@pytest.fixture
def populated_store(tmp_path):
    """Build a real on-disk SqliteStore with knobs for narrative + scrapbook
    coverage. The fixture returns a callable so each test can dial its own
    coverage shape.

    Closing the store between tests is the caller's responsibility.
    """
    from sidequest.game.persistence import SqliteStore
    from sidequest.protocol.messages import ScrapbookEntryPayload

    created: list[SqliteStore] = []

    def _make(*, narrative_rounds: int, scrapbook_rounds: int) -> SqliteStore:
        if scrapbook_rounds > narrative_rounds:
            raise ValueError(
                "Test fixture invariant: scrapbook_rounds cannot exceed "
                "narrative_rounds (scrapbook indexes into narrative_log)."
            )
        db_path = tmp_path / f"cov-{narrative_rounds}-{scrapbook_rounds}.db"
        store = SqliteStore.open(str(db_path))
        store.init_session("test_genre", "test_world")

        # Append narrative rounds 1..narrative_rounds. Each round carries
        # one entry — that's the round_number the scrapbook joins against.
        from sidequest.game.session import NarrativeEntry

        for r in range(1, narrative_rounds + 1):
            store.append_narrative(
                NarrativeEntry(
                    round=r,
                    author="narrator",
                    content=f"Round {r} narration text.",
                    tags=[],
                )
            )

        # Insert scrapbook rows for rounds 1..scrapbook_rounds. The scrapbook
        # row's ``turn_id`` mirrors the round number for fixture simplicity —
        # production uses ``max_narrative_round()`` to bound the range and
        # queries ``scrapbook_entries.turn_id`` directly (no join), per the
        # post-45-11 lockstep invariant (ADR-051).
        for r in range(1, scrapbook_rounds + 1):
            payload = ScrapbookEntryPayload(
                turn_id=r,
                scene_title=f"Scene {r}",
                scene_type="exploration",
                location=f"Location {r}",
                image_url=None,
                narrative_excerpt=f"Round {r} excerpt.",
                world_facts=[],
                npcs_present=[],
            )
            with store._conn:
                store._conn.execute(
                    "INSERT INTO scrapbook_entries "
                    "(turn_id, scene_title, scene_type, location, image_url, "
                    " narrative_excerpt, world_facts, npcs_present) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        payload.turn_id,
                        payload.scene_title,
                        payload.scene_type,
                        payload.location,
                        payload.image_url,
                        payload.narrative_excerpt,
                        _json.dumps(list(payload.world_facts)),
                        _json.dumps(
                            [
                                {"name": ref.name, "role": ref.role, "disposition": ref.disposition}
                                for ref in payload.npcs_present
                            ]
                        ),
                    ),
                )

        created.append(store)
        return store

    yield _make

    import contextlib

    for s in created:
        # Fixture cleanup must never raise — close() failures on already-
        # closed handles are benign here.
        with contextlib.suppress(Exception):
            s.close()


# Minimal snapshot fixture — the detector's signature accepts
# (store, snapshot, **ctx) but only reads ``genre_slug`` / ``world_slug``
# off the snapshot for span attribution. This keeps tests independent of
# GameSnapshot's full surface.
@pytest.fixture
def stub_snapshot():
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        location="Unknown",
    )


# ---------------------------------------------------------------------------
# Module + dataclass shape
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """Shape of the new module — fail before the implementer can do anything else."""

    def test_module_imports_cleanly(self) -> None:
        """The module must exist at the documented path and expose the
        helper. Asserting both keeps the import-and-symbol check in one
        place — a missing module fails the import; a renamed helper fails
        the hasattr."""
        import sidequest.game.scrapbook_coverage as mod

        assert hasattr(mod, "detect_scrapbook_coverage_gaps"), (
            "scrapbook_coverage module must expose detect_scrapbook_coverage_gaps "
            "at module top — a rename here forces a coordinated update to the "
            "wire site in connect.py."
        )

    def test_helper_function_exported(self) -> None:
        """``detect_scrapbook_coverage_gaps`` must be a top-level callable."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        assert callable(detect_scrapbook_coverage_gaps), (
            "detect_scrapbook_coverage_gaps must be importable and callable. "
            "If you renamed it, also update the connect.py wire and this test."
        )

    def test_report_dataclass_exported(self) -> None:
        """``ScrapbookCoverageReport`` must be a dataclass with the 5 fields
        the spans + watcher event consume.

        Fields locked by AC1/AC2/AC3:
        - max_round: int
        - covered_count: int
        - gap_count: int
        - gap_rounds: tuple[int, ...] | list[int]
        - coverage_ratio: float
        """
        from sidequest.game.scrapbook_coverage import ScrapbookCoverageReport

        assert is_dataclass(ScrapbookCoverageReport), (
            "ScrapbookCoverageReport must be a @dataclass for stable field "
            "introspection — the watcher event payload pulls from the report."
        )
        names = {f.name for f in fields(ScrapbookCoverageReport)}
        assert names == {
            "max_round",
            "covered_count",
            "gap_count",
            "gap_rounds",
            "coverage_ratio",
        }, (
            f"Report fields must match the AC contract. Got {sorted(names)}; "
            f"expected max_round, covered_count, gap_count, gap_rounds, "
            f"coverage_ratio."
        )

    def test_helper_has_type_annotations(self) -> None:
        """Public helper MUST have annotated parameters and return type
        (python.md rule #3 — type annotations at module boundaries)."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        hints = get_type_hints(detect_scrapbook_coverage_gaps)
        assert "return" in hints, (
            "Public boundary function missing return annotation (python.md #3)."
        )
        # Parameters: at minimum store + snapshot. Names checked here so a
        # rename forces a coordinated update to the wire site in connect.py.
        params = {k for k in hints if k != "return"}
        assert {"store", "snapshot"}.issubset(params), (
            f"Helper must accept (store, snapshot, ...). Got params {params}."
        )


# ---------------------------------------------------------------------------
# AC1 / AC3 — full coverage and empty store paths
# ---------------------------------------------------------------------------


class TestNoGapPaths:
    """AC1 (full coverage) and AC3 (fresh save, empty narrative)."""

    def test_empty_store_reports_zero_max_round(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """AC3: fresh save (no rounds, no entries) → max_round=0, gap_count=0,
        coverage_ratio=1.0 (defined, not NaN — context: 'better than NaN for
        downstream dashboarding')."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=0, scrapbook_rounds=0)
        report = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        assert report.max_round == 0
        assert report.covered_count == 0
        assert report.gap_count == 0
        assert tuple(report.gap_rounds) == ()
        assert report.coverage_ratio == pytest.approx(1.0), (
            "AC3 requires coverage_ratio==1.0 on empty (no rounds) — NaN/0 "
            "would break the GM-panel chart axis."
        )

    def test_full_coverage_reports_zero_gaps(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """AC1: 5 narrative rounds, 5 scrapbook rounds → gap_count=0,
        ratio=1.0, no gap span, no watcher event."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=5, scrapbook_rounds=5)
        report = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        assert report.max_round == 5
        assert report.covered_count == 5
        assert report.gap_count == 0
        assert tuple(report.gap_rounds) == ()
        assert report.coverage_ratio == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# AC2 — the Orin regression
# ---------------------------------------------------------------------------


class TestOrinRegression:
    """AC2: 29 rounds narrative + 10 rounds scrapbook (rounds 1-10) → gap of 19.

    The bug-evidence fixture from Playtest 3 becomes the failing test that
    drives the detector into existence.
    """

    def test_orin_fixture_yields_19_round_gap(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        report = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        assert report.max_round == 29
        assert report.covered_count == 10
        assert report.gap_count == 19
        # Rounds 11..29 inclusive — exact list. Order matters for the span
        # attribute payload (GM-panel renders the list verbatim).
        assert list(report.gap_rounds) == list(range(11, 30))
        # 10/29 ≈ 0.345
        assert report.coverage_ratio == pytest.approx(10 / 29, rel=1e-3)

    def test_orin_fixture_emits_evaluated_span(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """``scrapbook.coverage_evaluated`` must fire on the gap path with
        all attributes populated (gap_count=19, ratio≈0.345)."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        evaluated = _spans_named(otel_capture, "scrapbook.coverage_evaluated")
        assert len(evaluated) == 1, (
            "scrapbook.coverage_evaluated must fire exactly once per call. "
            "If it fires zero times, the helper isn't using the tracer; if "
            "it fires multiple times, you have a duplicate span site."
        )
        attrs = dict(evaluated[0].attributes or {})
        assert attrs.get("max_round") == 29
        assert attrs.get("covered_count") == 10
        assert attrs.get("gap_count") == 19
        # Span attribute coverage_ratio is a float; SDK may store as float.
        assert float(attrs.get("coverage_ratio") or 0) == pytest.approx(10 / 29, rel=1e-3)

    def test_orin_fixture_emits_gap_detected_span_with_gap_rounds(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """``scrapbook.coverage_gap_detected`` must fire with the full
        ``gap_rounds`` list as a span attribute."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        gap_spans = _spans_named(otel_capture, "scrapbook.coverage_gap_detected")
        assert len(gap_spans) == 1, (
            "Gap-detected span fires once when gap_count>0. Zero means the "
            "branch is dead; >1 means the helper is calling itself or "
            "duplicating the emit site."
        )
        attrs = dict(gap_spans[0].attributes or {})
        assert attrs.get("gap_count") == 19
        # OTEL stringifies sequences in attribute exporters; accept either
        # a tuple/list-shaped value (preferred — when the SDK preserves
        # sequence types) or a string repr (when an exporter coerces). In
        # both cases assert on the EXACT 11..29 range, not a fuzzy
        # substring — "11" and "29" appearing in a string would also match
        # "111", "291", "229", etc., which masks off-by-one regressions.
        gap_rounds_attr = attrs.get("gap_rounds")
        assert gap_rounds_attr is not None, (
            "gap_rounds attribute is the load-bearing payload — without it "
            "the GM panel can't render which rounds are missing."
        )
        expected = tuple(range(11, 30))
        if isinstance(gap_rounds_attr, (list, tuple)):
            assert tuple(gap_rounds_attr) == expected, (
                f"gap_rounds sequence must be exactly 11..29 inclusive. Got: {gap_rounds_attr!r}"
            )
        else:
            # SDK-serialised case: parse the string repr and compare the
            # parsed integer sequence against the expected tuple.
            import re

            parsed = tuple(int(m) for m in re.findall(r"-?\d+", str(gap_rounds_attr)))
            assert parsed == expected, (
                f"gap_rounds string repr must parse to 11..29 inclusive. "
                f"Got: {gap_rounds_attr!r} (parsed: {parsed})"
            )

    def test_orin_fixture_publishes_watcher_event(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """Gap path must publish ``scrapbook_coverage_gap`` watcher event
        with ``severity='warning'`` so the GM panel surfaces it visibly."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        gap_publishes = [c for c in watcher_capture if c["field"] == "scrapbook_coverage_gap"]
        assert len(gap_publishes) == 1, (
            "Gap path must publish exactly one scrapbook_coverage_gap event. "
            f"Got {len(gap_publishes)} (events: {watcher_capture!r})."
        )
        evt = gap_publishes[0]
        assert evt.get("severity") == "warning", (
            "GM panel uses severity to color the lane — gaps must be "
            "warning-level so they stand out from informational chatter."
        )
        assert evt.get("component") == "scrapbook"

        # Lock the watcher payload contract — the GM-panel renderer reads
        # these keys verbatim, so a payload-shape regression here would
        # quietly break the dashboard.
        payload = evt["payload"]
        assert set(payload.keys()) >= {
            "max_round",
            "covered_count",
            "gap_count",
            "coverage_ratio",
            "gap_rounds",
            "genre",
            "world",
            "slug",
        }, (
            f"Watcher payload missing required keys for GM-panel render. "
            f"Got: {sorted(payload.keys())}"
        )
        assert payload["max_round"] == 29
        assert payload["covered_count"] == 10
        assert payload["gap_count"] == 19
        assert payload["coverage_ratio"] == pytest.approx(10 / 29, rel=1e-3)
        assert payload["gap_rounds"] == list(range(11, 30))
        assert payload["genre"] == "test_genre"
        assert payload["world"] == "test_world"


# ---------------------------------------------------------------------------
# No-op paths — gap span and watcher event MUST NOT fire on full coverage
# ---------------------------------------------------------------------------


class TestNoOpSilence:
    """The negative cases that catch a half-fixed helper. AC1 mandates these."""

    def test_full_coverage_does_not_emit_gap_span(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=5, scrapbook_rounds=5)
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        gap_spans = _spans_named(otel_capture, "scrapbook.coverage_gap_detected")
        assert gap_spans == [], (
            f"Gap span must NOT fire on full coverage. Found {len(gap_spans)} "
            f"— a half-fix that always emits the gap span breaks the "
            f"GM-panel signal-to-noise ratio."
        )

    def test_full_coverage_does_not_publish_watcher_event(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=5, scrapbook_rounds=5)
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        gap_publishes = [c for c in watcher_capture if c["field"] == "scrapbook_coverage_gap"]
        assert gap_publishes == [], (
            "Watcher event MUST NOT publish on full coverage — Sebastien's "
            "GM panel would cry wolf and the alerting goes numb."
        )

    def test_empty_store_emits_evaluated_span_with_max_round_zero(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """AC3 explicit: even on a fresh save the evaluated span MUST fire so
        Sebastien gets negative-confirmation that scrapbook coverage was
        checked. This is the no-op path that a half-fix typically skips."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=0, scrapbook_rounds=0)
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        evaluated = _spans_named(otel_capture, "scrapbook.coverage_evaluated")
        assert len(evaluated) == 1, (
            "Evaluated span MUST fire once even on empty stores. Skipping "
            "this branch is the exact lie-detector blind-spot CLAUDE.md "
            "calls out."
        )
        attrs = dict(evaluated[0].attributes or {})
        assert attrs.get("max_round") == 0
        assert attrs.get("gap_count") == 0

        # The empty-store path must NOT emit the gap span or the watcher
        # event. A half-fix that fires both branches unconditionally would
        # cry wolf on every fresh save and the GM panel signal goes numb.
        assert _spans_named(otel_capture, "scrapbook.coverage_gap_detected") == [], (
            "Gap span must NOT fire on max_round==0 (fresh save). A noisy "
            "gap-on-empty would teach Sebastien to ignore the signal."
        )
        gap_publishes = [c for c in watcher_capture if c["field"] == "scrapbook_coverage_gap"]
        assert gap_publishes == [], (
            "Watcher event must NOT publish on max_round==0. Empty stores "
            "are not a gap — they are 'no coverage required yet'."
        )


# ---------------------------------------------------------------------------
# AC5 — read-only invariant
# ---------------------------------------------------------------------------


class TestReadOnlyInvariant:
    """AC5: detector must not mutate any DB state. Idempotent across resumes."""

    def test_helper_does_not_change_narrative_log_count(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        before = _row_count(store, "narrative_log")
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)
        after = _row_count(store, "narrative_log")

        assert before == after == 29, (
            f"Detector mutated narrative_log: {before} → {after}. AC5 requires read-only behavior."
        )

    def test_helper_does_not_change_scrapbook_count(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        before = _row_count(store, "scrapbook_entries")
        detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)
        after = _row_count(store, "scrapbook_entries")

        assert before == after == 10, (
            f"Detector mutated scrapbook_entries: {before} → {after}. AC5 "
            f"explicitly rejects backfill — warn-only is the chosen path."
        )

    def test_helper_idempotent_on_repeated_invocation(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """AC5 explicit: same store, same snapshot, called twice → identical
        report and identical span/watcher fan-out shape (each call: one
        evaluated span, one gap span, one watcher event)."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        store = populated_store(narrative_rounds=29, scrapbook_rounds=10)
        r1 = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)
        r2 = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        assert r1 == r2, "Same input must yield the same report."
        # 2 evaluated spans, 2 gap-detected spans, 2 watcher events
        assert len(_spans_named(otel_capture, "scrapbook.coverage_evaluated")) == 2
        assert len(_spans_named(otel_capture, "scrapbook.coverage_gap_detected")) == 2
        assert len([c for c in watcher_capture if c["field"] == "scrapbook_coverage_gap"]) == 2


# ---------------------------------------------------------------------------
# Span routing registration (rule-coverage test for the OTEL discipline)
# ---------------------------------------------------------------------------


class TestSpanRouting:
    """The new spans must be in ``SPAN_ROUTES`` so the GM-panel watcher
    feed picks them up (CLAUDE.md OTEL principle — 'every backend fix that
    touches a subsystem MUST add OTEL watcher events so the GM panel can
    verify the fix is working')."""

    def test_evaluated_span_constant_exported(self) -> None:
        from sidequest.telemetry.spans import SPAN_SCRAPBOOK_COVERAGE_EVALUATED

        assert SPAN_SCRAPBOOK_COVERAGE_EVALUATED == "scrapbook.coverage_evaluated", (
            "Span constant must equal the documented name; the GM panel "
            "filters on this exact string."
        )

    def test_gap_detected_span_constant_exported(self) -> None:
        from sidequest.telemetry.spans import SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED

        assert SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED == "scrapbook.coverage_gap_detected"

    def test_evaluated_span_registered_in_routes(self) -> None:
        """SPAN_ROUTES entry required for the watcher hub to pick this
        span up. Without the route, the span fires into a void and the
        GM panel never sees it (silent failure mode)."""
        from sidequest.telemetry.spans import (
            SPAN_ROUTES,
            SPAN_SCRAPBOOK_COVERAGE_EVALUATED,
        )

        assert SPAN_SCRAPBOOK_COVERAGE_EVALUATED in SPAN_ROUTES, (
            "Span constant declared but not routed — defeats the whole "
            "point of the lie-detector. Add a SPAN_ROUTES[name]=SpanRoute(...) "
            "entry alongside the constant in spans/scrapbook.py."
        )
        route = SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_EVALUATED]
        assert route.component == "scrapbook"

    def test_gap_detected_span_registered_in_routes(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_ROUTES,
            SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED,
        )

        assert SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED in SPAN_ROUTES
        route = SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED]
        assert route.component == "scrapbook"

    def test_evaluated_route_extract_returns_expected_dict_shape(self) -> None:
        """``SpanRoute.extract`` is the load-bearing call site — the watcher
        hub invokes it on every closed span to produce the typed event
        payload. Tests only asserting ``route.component`` leave the
        extractor untested, so a regression in the dict shape (renamed
        field, missing key) would silently break the GM panel feed.
        Construct a fake span with known attributes and assert the dict
        matches the documented contract."""
        from sidequest.telemetry.spans import (
            SPAN_ROUTES,
            SPAN_SCRAPBOOK_COVERAGE_EVALUATED,
        )

        route = SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_EVALUATED]
        fake_span = _FakeSpan(
            name=SPAN_SCRAPBOOK_COVERAGE_EVALUATED,
            attributes={
                "max_round": 29,
                "covered_count": 10,
                "gap_count": 19,
                "coverage_ratio": 10 / 29,
                "genre": "test_genre",
                "world": "test_world",
                "slug": "orin-1",
            },
        )

        out = route.extract(fake_span)

        assert out == {
            "field": "scrapbook",
            "op": "coverage_evaluated",
            "max_round": 29,
            "covered_count": 10,
            "gap_count": 19,
            "coverage_ratio": pytest.approx(10 / 29),
            "genre": "test_genre",
            "world": "test_world",
            "slug": "orin-1",
        }

    def test_evaluated_route_extract_handles_missing_attributes(self) -> None:
        """Missing-attribute path: extractor must fall back to documented
        defaults (zero counts, ratio 1.0, empty strings). The watcher hub
        cannot tolerate a KeyError or ``None`` leaking into the dashboard
        payload — the panel renders these fields verbatim."""
        from sidequest.telemetry.spans import (
            SPAN_ROUTES,
            SPAN_SCRAPBOOK_COVERAGE_EVALUATED,
        )

        route = SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_EVALUATED]
        fake_span = _FakeSpan(name=SPAN_SCRAPBOOK_COVERAGE_EVALUATED, attributes={})

        out = route.extract(fake_span)

        assert out == {
            "field": "scrapbook",
            "op": "coverage_evaluated",
            "max_round": 0,
            "covered_count": 0,
            "gap_count": 0,
            "coverage_ratio": 1.0,
            "genre": "",
            "world": "",
            "slug": "",
        }

    def test_gap_detected_route_extract_returns_expected_dict_shape(self) -> None:
        """Gap-detected extractor mirrors the evaluated extractor plus
        ``gap_rounds`` — the load-bearing payload that the GM panel
        renders verbatim. Pin the dict shape so a renamed field breaks
        the test before it breaks the dashboard."""
        from sidequest.telemetry.spans import (
            SPAN_ROUTES,
            SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED,
        )

        route = SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED]
        fake_span = _FakeSpan(
            name=SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED,
            attributes={
                "max_round": 29,
                "covered_count": 10,
                "gap_count": 19,
                "coverage_ratio": 10 / 29,
                "gap_rounds": tuple(range(11, 30)),
                "genre": "test_genre",
                "world": "test_world",
                "slug": "orin-1",
            },
        )

        out = route.extract(fake_span)

        assert out == {
            "field": "scrapbook",
            "op": "coverage_gap_detected",
            "max_round": 29,
            "covered_count": 10,
            "gap_count": 19,
            "coverage_ratio": pytest.approx(10 / 29),
            "gap_rounds": tuple(range(11, 30)),
            "genre": "test_genre",
            "world": "test_world",
            "slug": "orin-1",
        }


# ---------------------------------------------------------------------------
# AC6 / AC7 — gap-pattern edge cases
# ---------------------------------------------------------------------------


class TestGapPatternEdgeCases:
    """Edge-case shapes that contiguous-range tests don't exercise."""

    def test_non_contiguous_gap_pattern(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """Rounds 1-5 and 8-10 covered → gap_rounds == [6, 7]. The Orin
        regression is a single contiguous tail-gap, but real-world saves
        could have any pattern — pin the set-difference math against a
        non-contiguous shape so an off-by-one in ``expected - covered``
        surfaces here."""
        from sidequest.game.persistence import SqliteStore
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps
        from sidequest.game.session import NarrativeEntry
        from sidequest.protocol.messages import ScrapbookEntryPayload

        # Hand-built fixture — populated_store only supports contiguous
        # 1..N scrapbook coverage.
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/non-contig.db"
            store = SqliteStore.open(db_path)
            store.init_session("test_genre", "test_world")
            for r in range(1, 11):
                store.append_narrative(
                    NarrativeEntry(round=r, author="narrator", content=f"R{r}", tags=[])
                )
            covered_rounds = list(range(1, 6)) + list(range(8, 11))
            with store._conn:
                for r in covered_rounds:
                    payload = ScrapbookEntryPayload(
                        turn_id=r,
                        scene_title=f"Scene {r}",
                        scene_type="exploration",
                        location=f"Location {r}",
                        image_url=None,
                        narrative_excerpt=f"R{r}",
                        world_facts=[],
                        npcs_present=[],
                    )
                    store._conn.execute(
                        "INSERT INTO scrapbook_entries "
                        "(turn_id, scene_title, scene_type, location, image_url, "
                        " narrative_excerpt, world_facts, npcs_present) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            payload.turn_id,
                            payload.scene_title,
                            payload.scene_type,
                            payload.location,
                            payload.image_url,
                            payload.narrative_excerpt,
                            _json.dumps([]),
                            _json.dumps([]),
                        ),
                    )

            try:
                report = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)
            finally:
                store.close()

        assert report.max_round == 10
        assert report.covered_count == 8
        assert report.gap_count == 2
        assert tuple(report.gap_rounds) == (6, 7), (
            "Non-contiguous coverage must yield only the missing rounds, "
            "in ascending order. A bug in expected-covered set math would "
            "either include 6/7 plus extras or miss them entirely."
        )

    def test_out_of_range_scrapbook_rows_excluded(
        self, populated_store, stub_snapshot, otel_capture, watcher_capture
    ) -> None:
        """Rows with ``turn_id <= 0`` or ``turn_id > max_round`` are noise
        (test-fixture artifacts, pre-lockstep stragglers). The detector's
        WHERE clause must filter them out so they never inflate
        ``covered_count`` or pollute ``gap_rounds``."""
        from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps

        # 5 narrative rounds, 5 valid scrapbook rows (1..5 — full coverage).
        store = populated_store(narrative_rounds=5, scrapbook_rounds=5)
        # Inject noise rows: turn_id=0 (pre-lockstep) and turn_id=10 (past
        # max_round).
        with store._conn:
            for noise_turn_id in (0, 10):
                store._conn.execute(
                    "INSERT INTO scrapbook_entries "
                    "(turn_id, scene_title, scene_type, location, image_url, "
                    " narrative_excerpt, world_facts, npcs_present) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        noise_turn_id,
                        "Noise",
                        "exploration",
                        "Nowhere",
                        None,
                        "out-of-range",
                        _json.dumps([]),
                        _json.dumps([]),
                    ),
                )

        report = detect_scrapbook_coverage_gaps(store=store, snapshot=stub_snapshot)

        assert report.max_round == 5
        assert report.covered_count == 5, (
            "Out-of-range rows (turn_id=0, turn_id=max_round+5) must NOT "
            "inflate covered_count. The WHERE filter is the lie-detector "
            "for fixture-leak regressions."
        )
        assert report.gap_count == 0
        assert tuple(report.gap_rounds) == ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSpan:
    """Structural stand-in for opentelemetry.sdk.trace.ReadableSpan,
    matching ``_SpanLike`` in ``telemetry/spans/_core.py``. Used by
    ``TestSpanRouting`` to drive ``SpanRoute.extract`` without spinning up
    a real tracer."""

    def __init__(self, *, name: str, attributes: dict[str, Any] | None) -> None:
        self.name = name
        self.attributes = attributes


def _spans_named(exporter, name: str) -> list:
    """Return the list of finished spans whose name matches exactly."""
    return [s for s in exporter.get_finished_spans() if s.name == name]


_ROW_COUNT_TABLE_WHITELIST = frozenset({"narrative_log", "scrapbook_entries"})


def _row_count(store, table: str) -> int:
    """Read row count of a table directly through the store's connection.

    Accepts only the two tables this test module probes — the f-string
    interpolation is non-exploitable from test inputs but the whitelist
    pins the surface so a future caller cannot pass an attacker-controlled
    name into the query (python.md rule #11).
    """
    assert table in _ROW_COUNT_TABLE_WHITELIST, (
        f"_row_count only accepts {sorted(_ROW_COUNT_TABLE_WHITELIST)}; got {table!r}"
    )
    row = store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0
