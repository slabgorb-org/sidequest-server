"""Story 59-2 AC-2: DispatchPackage.degraded + degraded_reason REMOVED.

ADR-113 (Intent Router — Mechanical-Engagement Spine) retires the
"degraded → narrator-only" path on the SDK backend. The 2026-04-23
LocalDM design's ``degraded`` sentinel field is gone from the protocol;
producer failure surfaces as a typed exception, not as a degraded
DispatchPackage.

Memory rule ``feedback_no_fallbacks_hard`` + CLAUDE.md "No Silent
Fallbacks": fail loud, never return a degraded shape that callers can
quietly accept.

Per CLAUDE.md "No Source-Text Wiring Tests" the assertions here use
pydantic's constructor behavior (extra='forbid' on ProtocolBase rejects
unknown keys at validation time) and ``model_fields`` introspection,
NOT source-text grep. These survive refactors of the dispatch module
internals as long as the contract holds.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.dispatch import DispatchPackage


# ---------------------------------------------------------------------------
# AC-2: ``degraded`` and ``degraded_reason`` fields removed.
# ---------------------------------------------------------------------------


def test_dispatch_package_has_no_degraded_field() -> None:
    """``DispatchPackage.degraded`` is no longer a model field.

    Pydantic v2 exposes ``model_fields`` at the class level — interrogating
    it tells us the actual declared schema without grepping source.
    """
    assert "degraded" not in DispatchPackage.model_fields, (
        "DispatchPackage.degraded must be removed (ADR-113 / "
        f"feedback_no_fallbacks_hard); still declared. "
        f"model_fields={sorted(DispatchPackage.model_fields)}"
    )


def test_dispatch_package_has_no_degraded_reason_field() -> None:
    """``DispatchPackage.degraded_reason`` is no longer a model field."""
    assert "degraded_reason" not in DispatchPackage.model_fields, (
        "DispatchPackage.degraded_reason must be removed (ADR-113); "
        f"still declared. model_fields={sorted(DispatchPackage.model_fields)}"
    )


def test_dispatch_package_constructor_rejects_degraded_kwarg() -> None:
    """Constructing ``DispatchPackage(degraded=True, ...)`` raises
    because ``ProtocolBase`` sets ``extra='forbid'`` — unknown fields
    are not silently dropped; they fail validation.

    This is the behavioral half of the contract: callers cannot
    accidentally pass the old kwarg and get a silently-stripped result.
    """
    with pytest.raises(ValidationError):
        DispatchPackage(
            turn_id="t",
            per_player=[],
            cross_player=[],
            confidence_global=1.0,
            degraded=True,  # type: ignore[call-arg]
            degraded_reason="anything",  # type: ignore[call-arg]
        )


def test_dispatch_package_constructor_rejects_degraded_reason_only_kwarg() -> None:
    """Even the standalone ``degraded_reason=...`` kwarg is rejected —
    no halfway-stripped acceptance."""
    with pytest.raises(ValidationError):
        DispatchPackage(
            turn_id="t",
            per_player=[],
            cross_player=[],
            confidence_global=1.0,
            degraded_reason="this should not pass",  # type: ignore[call-arg]
        )


def test_dispatch_package_model_validate_rejects_legacy_degraded_payload() -> None:
    """A legacy serialized payload that still carries ``degraded`` is
    rejected by ``model_validate`` — proves we are not silently dropping
    the field on the wire path.

    This is the spec-§5 "no parallel period" discipline: there is no
    grace window during which old-shape payloads quietly succeed.
    """
    legacy_payload = {
        "turn_id": "t",
        "per_player": [],
        "cross_player": [],
        "confidence_global": 1.0,
        "degraded": False,
        "degraded_reason": None,
    }
    with pytest.raises(ValidationError):
        DispatchPackage.model_validate(legacy_payload)


def test_dispatch_package_no_degraded_validator_attached() -> None:
    """The ``_degraded_requires_reason`` validator is gone.

    Pydantic v2 stores validators on ``__pydantic_validator__`` /
    ``model_config``; the simplest behavioral interrogation is: no
    method by that name remains on the class. (We are not asserting
    the absence of a private name to be cute — we are asserting the
    validator was REMOVED, not commented out or renamed-with-the-same-
    intent. If a different validator enforces equivalent semantics on
    a removed field, this test will still pass because the field is
    gone.)
    """
    # No leftover validator method by that legacy name.
    assert not hasattr(DispatchPackage, "_degraded_requires_reason"), (
        "_degraded_requires_reason validator must be removed alongside "
        "the degraded field (ADR-113)"
    )


