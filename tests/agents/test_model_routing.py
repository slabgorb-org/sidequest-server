"""Tests for model routing — call-type → model id."""

from __future__ import annotations

import pytest

from sidequest.agents.model_routing import (
    CallType,
    UnknownCallType,
    resolve_model,
)


def test_narration_defaults_to_sonnet() -> None:
    assert resolve_model(CallType.NARRATION) == "claude-sonnet-4-6"


def test_narration_important_defaults_to_opus() -> None:
    assert resolve_model(CallType.NARRATION_IMPORTANT) == "claude-opus-4-8"


def test_classification_defaults_to_haiku() -> None:
    assert resolve_model(CallType.CLASSIFICATION) == "claude-haiku-4-5-20251001"


def test_scratch_defaults_to_haiku() -> None:
    assert resolve_model(CallType.SCRATCH) == "claude-haiku-4-5-20251001"


def test_per_pack_override_takes_precedence() -> None:
    pack_overrides = {CallType.NARRATION: "claude-opus-4-8"}
    assert resolve_model(CallType.NARRATION, pack_overrides=pack_overrides) == "claude-opus-4-8"


def test_partial_override_falls_back_to_default() -> None:
    pack_overrides = {CallType.NARRATION: "claude-opus-4-8"}
    assert (
        resolve_model(CallType.CLASSIFICATION, pack_overrides=pack_overrides)
        == "claude-haiku-4-5-20251001"
    )


def test_unknown_call_type_raises() -> None:
    with pytest.raises(UnknownCallType):
        resolve_model("not-a-call-type")  # type: ignore[arg-type]
