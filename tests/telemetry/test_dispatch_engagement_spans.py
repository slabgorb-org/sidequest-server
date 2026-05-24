"""Span family tests for ``dispatch_engagement.*`` (Story 59-3) +
reflection-based retirement tests for the 59-1 reprompt-failed span family
(Story 59-3 AC6 — "one mechanism per problem").

Two concerns in one file because they're symmetric: 59-3 introduces a NEW
span family (``dispatch_engagement.{confrontation,magic_working,scenario_clue}.mismatch``)
and RETIRES the OLD reprompt-failed span family (the 59-1 self-report-
reprompt path the new router-driven watcher replaces). Keeping the
"adopted" and "retired" assertions side by side makes the bookkeeping
audit trail readable.

CLAUDE.md "No Source-Text Wiring Tests": retirement is proved by reflection
(``hasattr`` on the module / dict-membership in ``SPAN_ROUTES``), never by
``read_text() + regex``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# AC4: NEW span family — dispatch_engagement.{subsystem}.mismatch
# ---------------------------------------------------------------------------


def test_dispatch_engagement_confrontation_mismatch_span_constant_registered() -> None:
    """The new span name constant exists and is registered in SPAN_ROUTES so
    the GM panel can render it."""
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans import dispatch_engagement as mod

    assert hasattr(mod, "SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH")
    name = mod.SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH
    assert name == "dispatch_engagement.confrontation.mismatch", (
        f"span name must be 'dispatch_engagement.confrontation.mismatch' per "
        f"epic-59 AC4; got: {name!r}"
    )
    assert name in SPAN_ROUTES, (
        f"span {name} not registered in SPAN_ROUTES — GM panel will not render this span family"
    )


def test_dispatch_engagement_magic_working_mismatch_span_constant_registered() -> None:
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans import dispatch_engagement as mod

    assert hasattr(mod, "SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH")
    name = mod.SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH
    assert name == "dispatch_engagement.magic_working.mismatch"
    assert name in SPAN_ROUTES


def test_dispatch_engagement_scenario_clue_mismatch_span_constant_registered() -> None:
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans import dispatch_engagement as mod

    assert hasattr(mod, "SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH")
    name = mod.SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH
    assert name == "dispatch_engagement.scenario_clue.mismatch"
    assert name in SPAN_ROUTES


def test_dispatch_engagement_route_carries_subsystem_attribute() -> None:
    """The SPAN_ROUTES extract function must surface the dispatched
    subsystem name as a span attribute so the GM panel can filter by
    subsystem without parsing the span name string."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    name = "dispatch_engagement.confrontation.mismatch"
    assert name in SPAN_ROUTES, f"span {name} not routed"

    # Run the extract function against a fake span payload to verify
    # the subsystem field is propagated. The route extract is a pure
    # function over span.attributes — easy to exercise directly.
    class _FakeSpan:
        attributes = {"subsystem": "confrontation"}

    extracted = SPAN_ROUTES[name].extract(_FakeSpan())
    assert extracted.get("subsystem") == "confrontation"


# ---------------------------------------------------------------------------
# AC6: RETIREMENT — confrontation_intent_mismatch_reprompt_failed_*
# These tests ASSERT THE ABSENCE of the symbols. They will FAIL today
# (the symbols still exist on the branch baseline) and PASS once Dev
# removes them during GREEN, completing the "one mechanism" cleanup.
# ---------------------------------------------------------------------------


def test_reprompt_failed_span_context_manager_removed() -> None:
    """AC6: the 59-1-shipped ``confrontation_intent_mismatch_reprompt_failed_span``
    context manager is no longer importable.

    The watcher (59-3) replaces the self-report-reprompt failure path.
    Per project memory ``feedback_one_mechanism_per_problem``, leaving the
    old span context manager in place would let a future caller silently
    re-engage the deprecated flow."""
    from sidequest.telemetry.spans import confrontation_intent as mod

    assert not hasattr(mod, "confrontation_intent_mismatch_reprompt_failed_span"), (
        "AC6 retirement: the reprompt-failed span context manager must be "
        "removed; the watcher (sidequest.agents.dispatch_engagement_watcher) "
        "is the replacement signal."
    )


def test_reprompt_failed_span_constant_removed() -> None:
    """AC6: the span name constant is gone too."""
    from sidequest.telemetry.spans import confrontation_intent as mod

    assert not hasattr(mod, "SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED"), (
        "AC6 retirement: SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED must be removed."
    )


def test_reprompt_failed_span_route_removed() -> None:
    """AC6: the SPAN_ROUTES entry is removed so the GM panel stops trying
    to render a span name that nothing emits."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    assert "confrontation.intent_mismatch_reprompt_failed" not in SPAN_ROUTES, (
        "AC6 retirement: SPAN_ROUTES must not contain the deprecated reprompt-failed span name."
    )


def test_reprompt_failed_span_not_reexported_from_spans_package() -> None:
    """AC6: the package-root re-export is gone so old callers like
    ``from sidequest.telemetry.spans import confrontation_intent_mismatch_reprompt_failed_span``
    fail at import time, not silently no-op."""
    import sidequest.telemetry.spans as spans_pkg

    assert not hasattr(spans_pkg, "confrontation_intent_mismatch_reprompt_failed_span")
    assert not hasattr(spans_pkg, "SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED")


# ---------------------------------------------------------------------------
# AC6 follow-on: the reprompt loop's consumer state (RepromptRequest +
# NarrationApplyOutcome.reprompt_request) is also removed. Without
# removing the consumer, the producer side could be silently re-wired
# later. Reflection check via dataclass field interrogation — the CLAUDE.md
# sanctioned "tripwire" pattern (interrogate runtime types, not source
# strings).
# ---------------------------------------------------------------------------


def test_reprompt_request_field_removed_from_narration_apply_outcome() -> None:
    """AC6 consumer cleanup: ``NarrationApplyOutcome.reprompt_request`` is
    gone because the reprompt loop that consumed it is gone (one mechanism
    rule). Dataclass-field reflection is the sanctioned tripwire shape
    (per CLAUDE.md "No Source-Text Wiring Tests" exception)."""
    import dataclasses

    from sidequest.server.narration_apply import NarrationApplyOutcome

    field_names = {f.name for f in dataclasses.fields(NarrationApplyOutcome)}
    assert "reprompt_request" not in field_names, (
        f"AC6 consumer cleanup: NarrationApplyOutcome.reprompt_request must "
        f"be removed alongside the reprompt loop in _execute_narration_turn. "
        f"Current fields: {sorted(field_names)}"
    )


def test_reprompt_request_class_removed() -> None:
    """AC6 consumer cleanup: the ``RepromptRequest`` dataclass itself is
    removed — no callers, no purpose."""
    import sidequest.server.narration_apply as mod

    assert not hasattr(mod, "RepromptRequest"), (
        "AC6 consumer cleanup: RepromptRequest dataclass must be removed "
        "(no remaining callers after the reprompt loop is retired)."
    )
