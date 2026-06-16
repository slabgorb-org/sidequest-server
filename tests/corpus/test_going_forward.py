from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.corpus.going_forward import (
    DISPATCH_PACKAGE_KIND,
    NARRATOR_DIRECTIVE_USED_KIND,
    VERDICT_OVERRIDE_KIND,
    DispatchPackageEvent,
    NarratorDirectiveUsedEvent,
    VerdictOverrideEvent,
)


def test_reserved_kinds_are_distinct_strings() -> None:
    kinds = {DISPATCH_PACKAGE_KIND, NARRATOR_DIRECTIVE_USED_KIND, VERDICT_OVERRIDE_KIND}
    assert len(kinds) == 3
    assert all(isinstance(k, str) and k for k in kinds)


def test_reserved_kinds_are_screaming_snake_case() -> None:
    """Matches the NARRATION / CHAPTER_MARKER convention used elsewhere."""
    for k in (DISPATCH_PACKAGE_KIND, NARRATOR_DIRECTIVE_USED_KIND, VERDICT_OVERRIDE_KIND):
        assert k == k.upper()
        assert " " not in k


def test_dispatch_package_event_roundtrips() -> None:
    evt = DispatchPackageEvent(
        decomposer_session_id="abc",
        dispatched_at="2026-04-24T00:00:00Z",
        raw_package_json="{}",
    )
    as_json = evt.model_dump_json()
    again = DispatchPackageEvent.model_validate_json(as_json)
    assert again.decomposer_session_id == "abc"


def test_narrator_directive_event_roundtrips() -> None:
    evt = NarratorDirectiveUsedEvent(
        directive_kind="must_narrate", directive_text="A beat of slapstick pain."
    )
    again = NarratorDirectiveUsedEvent.model_validate_json(evt.model_dump_json())
    assert again.directive_kind == "must_narrate"


def test_verdict_override_event_roundtrips_with_null_previous() -> None:
    evt = VerdictOverrideEvent(
        entity="alice", previous_verdict=None, new_verdict="humiliated", labeler="keith"
    )
    again = VerdictOverrideEvent.model_validate_json(evt.model_dump_json())
    assert again.previous_verdict is None


def test_dispatch_package_event_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        DispatchPackageEvent(
            decomposer_session_id="abc",
            dispatched_at="2026-04-24T00:00:00Z",
            raw_package_json="{}",
            nonsense="reject",  # type: ignore[call-arg]
        )
