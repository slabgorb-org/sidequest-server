"""Unit + wire tests for discovered_regions canonical-slug dedup
(Story 45-17).

Regression evidence: Playtest 3 Felix's save logged the same room
twice — narrator emitted "The Crew Quarters — Freighter Unpaid Debt"
on one turn and "the crew quarters of a beat-up freighter…" on
another, and the dedup compared raw strings so both landed in
``discovered_regions``. 45-16 added the rejection layer; 45-17
extends it with slug-based dedup so case- / punctuation-only variants
collapse to one entry.

Two layers of coverage:

1. **Unit** — ``canonicalize_region_name`` produces a stable slug
   and matches a curated set of equivalence classes (the Felix
   surface variants, accent folding, em-dash handling).
2. **Wire** — production write paths (`narration_apply.location_update`
   and `session.apply_world_patch.*`) collapse surface variants and
   emit ``region.entry_canonicalized_dedup`` so the GM panel can see
   the merge fire (CLAUDE.md OTEL principle).
"""

from __future__ import annotations

import pytest

from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Layer 1 — Unit: canonicalize_region_name
# ---------------------------------------------------------------------------


class TestCanonicalize:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("The Crew Quarters", "the-crew-quarters"),
            ("the crew quarters", "the-crew-quarters"),
            ("The  Crew   Quarters", "the-crew-quarters"),
            ("the crew quarters!", "the-crew-quarters"),
            (
                "The Crew Quarters — Freighter Unpaid Debt",
                "the-crew-quarters-freighter-unpaid-debt",
            ),
            ("Felix's Workshop", "felix-s-workshop"),
            ("Tood's Dome", "tood-s-dome"),
            ("Bridge", "bridge"),
            ("Scavenger Pit", "scavenger-pit"),
        ],
    )
    def test_slug_matches_expected(self, name: str, expected: str) -> None:
        from sidequest.game.region_validation import canonicalize_region_name

        assert canonicalize_region_name(name) == expected

    def test_slug_is_idempotent(self) -> None:
        """Slugging an already-slugged value returns the same value."""
        from sidequest.game.region_validation import canonicalize_region_name

        once = canonicalize_region_name("The Crew Quarters")
        twice = canonicalize_region_name(once)
        assert once == twice == "the-crew-quarters"

    def test_slug_folds_accents(self) -> None:
        from sidequest.game.region_validation import canonicalize_region_name

        assert canonicalize_region_name("Tood's Dôme") == "tood-s-dome"
        # Same as the unaccented form — accent variants collapse.
        assert canonicalize_region_name("Tood's Dôme") == canonicalize_region_name("Tood's Dome")

    def test_slug_collapses_em_dashes_and_punctuation(self) -> None:
        from sidequest.game.region_validation import canonicalize_region_name

        # Em-dash, em-dash with spaces, hyphen — all the same slug.
        a = canonicalize_region_name("Crew Quarters — Freighter")
        b = canonicalize_region_name("Crew Quarters - Freighter")
        c = canonicalize_region_name("Crew Quarters—Freighter")
        assert a == b == c == "crew-quarters-freighter"

    def test_blank_returns_empty_slug(self) -> None:
        from sidequest.game.region_validation import canonicalize_region_name

        assert canonicalize_region_name("") == ""
        assert canonicalize_region_name("   ") == ""

    def test_felix_playtest_variants_collapse(self) -> None:
        """The exact two surface forms from Felix's Playtest 3 save —
        these were the duplicate-region bug evidence."""
        from sidequest.game.region_validation import canonicalize_region_name

        # The two forms aren't *identical* prose (the narrator phrased
        # them differently), so a pure slug doesn't collapse them.
        # AC3 requires the canonical-dedup to catch case/punctuation
        # variants of the SAME prose; semantic dedup (these two are
        # different prose for the same room) is out-of-scope for 45-17.
        a_slug = canonicalize_region_name("The Crew Quarters")
        b_slug = canonicalize_region_name("the crew quarters")
        c_slug = canonicalize_region_name("THE  CREW  QUARTERS!")
        assert a_slug == b_slug == c_slug == "the-crew-quarters"


# ---------------------------------------------------------------------------
# Layer 2 — Span registration
# ---------------------------------------------------------------------------


class TestSpanRegistration:
    def test_span_constant_defined(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_REGION_ENTRY_CANONICALIZED_DEDUP,
        )

        assert SPAN_REGION_ENTRY_CANONICALIZED_DEDUP == "region.entry_canonicalized_dedup"

    def test_span_routed_to_state_transition_event(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_REGION_ENTRY_CANONICALIZED_DEDUP,
            SPAN_ROUTES,
        )

        route = SPAN_ROUTES.get(SPAN_REGION_ENTRY_CANONICALIZED_DEDUP)
        assert route is not None
        assert route.event_type == "state_transition"
        assert route.component == "region_state"

    def test_span_extract_carries_audit_fields(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_REGION_ENTRY_CANONICALIZED_DEDUP,
            SPAN_ROUTES,
        )

        route = SPAN_ROUTES[SPAN_REGION_ENTRY_CANONICALIZED_DEDUP]

        class FakeSpan:
            attributes = {
                "entry": "the crew quarters",
                "canonical_slug": "the-crew-quarters",
                "existing_surface_form": "The Crew Quarters",
                "caller_path": "narration_apply.location_update",
                "dedup_count": 1,
            }

        extracted = route.extract(FakeSpan())
        assert extracted == {
            "field": "discovered_regions",
            "op": "canonicalized_dedup",
            "entry": "the crew quarters",
            "canonical_slug": "the-crew-quarters",
            "existing_surface_form": "The Crew Quarters",
            "caller_path": "narration_apply.location_update",
            "dedup_count": 1,
        }


# ---------------------------------------------------------------------------
# Layer 3 — Wire tests on production write paths
# ---------------------------------------------------------------------------


def _make_minimal_snapshot():
    from sidequest.game.session import GameSnapshot

    return GameSnapshot()


def _make_narration_result(*, narration: str, location: str | None):
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(narration=narration, location=location)


class TestNarrationApplyDedupWiring:
    """``_apply_narration_result_to_snapshot`` collapses surface
    variants emitted across turns into one ``discovered_regions``
    entry."""

    def test_first_form_wins(self) -> None:
        from sidequest.server.narration_apply import (
            _apply_narration_result_to_snapshot,
        )

        snap = _make_minimal_snapshot()

        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(narration="…", location="The Crew Quarters"),
            player_name="Felix",
            room=room_for(snap),
        )
        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(narration="…", location="the crew quarters"),
            player_name="Felix",
            room=room_for(snap),
        )

        assert snap.discovered_regions == ["The Crew Quarters"], (
            f"second-emitted variant must dedup against first; got {snap.discovered_regions}"
        )

    def test_dedup_emits_span(self, otel_capture) -> None:
        from sidequest.server.narration_apply import (
            _apply_narration_result_to_snapshot,
        )

        snap = _make_minimal_snapshot()

        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(narration="…", location="The Crew Quarters"),
            player_name="Felix",
            room=room_for(snap),
        )
        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(narration="…", location="THE  CREW  QUARTERS!"),
            player_name="Felix",
            room=room_for(snap),
        )

        dedup_spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "region.entry_canonicalized_dedup"
        ]
        assert len(dedup_spans) == 1, (
            "second variant must emit dedup span so Sebastien sees the merge fire"
        )
        attrs = dict(dedup_spans[0].attributes)
        assert attrs["entry"] == "THE  CREW  QUARTERS!"
        assert attrs["existing_surface_form"] == "The Crew Quarters"
        assert attrs["canonical_slug"] == "the-crew-quarters"
        assert attrs["caller_path"] == "narration_apply.location_update"

    def test_exact_match_does_not_emit_dedup_span(self, otel_capture) -> None:
        """Exact-string repeat is the no-op case — already-handled
        before 45-17. Don't fire a dedup span on the no-surface-variant
        path; the GM panel would mistake it for a real dedup event."""
        from sidequest.server.narration_apply import (
            _apply_narration_result_to_snapshot,
        )

        snap = _make_minimal_snapshot()
        for _ in range(2):
            _apply_narration_result_to_snapshot(
                snap,
                _make_narration_result(narration="…", location="The Crew Quarters"),
                player_name="Felix",
                room=room_for(snap),
            )

        dedup_spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "region.entry_canonicalized_dedup"
        ]
        assert dedup_spans == []
        assert snap.discovered_regions == ["The Crew Quarters"]

    def test_distinct_rooms_both_appended(self) -> None:
        from sidequest.server.narration_apply import (
            _apply_narration_result_to_snapshot,
        )

        snap = _make_minimal_snapshot()
        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(narration="…", location="The Crew Quarters"),
            player_name="Felix",
            room=room_for(snap),
        )
        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(narration="…", location="The Bridge"),
            player_name="Felix",
            room=room_for(snap),
        )

        assert snap.discovered_regions == ["The Crew Quarters", "The Bridge"]


class TestSessionPatchDedupWiring:
    """``GameSnapshot.apply_world_patch`` dedups both the
    wholesale-replace path (``patch.discovered_regions``) and the
    incremental-discover path (``patch.discover_regions``)."""

    def test_discover_regions_patch_dedups_against_existing(
        self,
        otel_capture,
    ) -> None:
        from sidequest.game.session import GameSnapshot, WorldStatePatch

        snap = GameSnapshot()
        snap.discovered_regions = ["The Crew Quarters"]
        patch = WorldStatePatch(discover_regions=["the crew quarters", "Engine Room"])

        snap.apply_world_patch(patch)

        assert snap.discovered_regions == ["The Crew Quarters", "Engine Room"]
        dedup_spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "region.entry_canonicalized_dedup"
        ]
        assert len(dedup_spans) == 1
        attrs = dict(dedup_spans[0].attributes)
        assert attrs["caller_path"] == "session.apply_patch.discover_regions"
        assert attrs["entry"] == "the crew quarters"
        assert attrs["existing_surface_form"] == "The Crew Quarters"

    def test_discovered_regions_set_path_collapses_internal_dups(
        self,
        otel_capture,
    ) -> None:
        """A wholesale-replace patch carrying its own internal dups
        (e.g., narrator emitted both forms in one patch) must
        collapse to one entry per slug."""
        from sidequest.game.session import GameSnapshot, WorldStatePatch

        snap = GameSnapshot()
        patch = WorldStatePatch(
            discovered_regions=[
                "The Crew Quarters",
                "the crew quarters",
                "Engine Room",
            ]
        )

        snap.apply_world_patch(patch)

        assert snap.discovered_regions == ["The Crew Quarters", "Engine Room"]
        dedup_spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "region.entry_canonicalized_dedup"
        ]
        assert len(dedup_spans) == 1
        attrs = dict(dedup_spans[0].attributes)
        assert attrs["caller_path"] == "session.apply_patch.discovered_regions_set"


class TestValidationStillFiresFirst:
    """AC4 ordering: validate-then-canonicalize. A bracketed entry
    must be rejected (45-16 guard) before canonicalization is
    attempted, so the rejection span fires and the dedup span does
    not."""

    def test_bracketed_rejected_not_canonicalized(self, otel_capture) -> None:
        from sidequest.server.narration_apply import (
            _apply_narration_result_to_snapshot,
        )

        snap = _make_minimal_snapshot()
        _apply_narration_result_to_snapshot(
            snap,
            _make_narration_result(
                narration="…",
                location="(aside — narrator brief)",
            ),
            player_name="Felix",
            room=room_for(snap),
        )

        spans_by_name = {s.name for s in otel_capture.get_finished_spans()}
        assert "region.entry_rejected" in spans_by_name
        assert "region.entry_canonicalized_dedup" not in spans_by_name
        assert "(aside — narrator brief)" not in snap.discovered_regions
