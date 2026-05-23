"""Tests for ``KnownFact.confidence`` Literal type promotion (story 50-17, J-4 per ADR-100).

These tests pin the contract that ``KnownFact.confidence`` is a closed
``Literal["Certain", "Suspected", "Rumored", "Discovered"]`` rather than the
legacy ``str = "confirmed"`` default. The literal mirrors the existing canonical
set in ``sidequest.server.dispatch.scenario_accusation._SUPPORTED_CONFIDENCES``
and ``sidequest.game.accusation.AccusationItem.confidence``.

Coverage:
- AC1: accepts each of the four canonical values
- AC2: rejects unknown / legacy / mis-cased values (pydantic ValidationError)
- AC3: JSON round-trip preserves the literal value verbatim
- AC4: default value is one of the four canonical values (not ``"confirmed"``)
- AC5: the production path ``KnownFact.model_validate(<dict>)`` — used by
  ``session.apply_patch`` for narrator-emitted ``DiscoveredFact.fact`` dicts —
  also rejects unknown values. This is the type-safety boundary that turns
  silent string drift into a loud validation error.
- Wiring: field annotation actually narrows to ``Literal[...]`` rather than
  ``str`` (proves the type-system pin, not just runtime validation).
"""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from sidequest.game.character import KnownFact

CANONICAL_CONFIDENCES = frozenset({"Certain", "Suspected", "Rumored", "Discovered"})


# ---------------------------------------------------------------------------
# AC1 — accepts canonical values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", sorted(CANONICAL_CONFIDENCES))
def test_known_fact_confidence_accepts_canonical_value(value: str) -> None:
    """Each of the four canonical confidence values constructs successfully."""
    kf = KnownFact(content="The Warden is in the mines", confidence=value)
    assert kf.confidence == value


# ---------------------------------------------------------------------------
# AC2 — rejects non-canonical values
# ---------------------------------------------------------------------------


def test_known_fact_confidence_rejects_legacy_confirmed() -> None:
    """The legacy ``"confirmed"`` string is no longer a valid confidence.

    Migration intent: the previous ``str = "confirmed"`` default is being
    replaced by one of the four canonical values. Pre-existing call sites
    that hand-rolled ``"confirmed"`` must be migrated by Dev as part of
    GREEN. Failing loudly here is the whole point of the Literal — it
    surfaces those call sites instead of silently accepting drift.
    """
    with pytest.raises(ValidationError):
        KnownFact(content="x", confidence="confirmed")


@pytest.mark.parametrize(
    "value",
    [
        "high",
        "likely",
        "maybe",
        "unknown",
        "True",
        "0.8",
        "certainly",
    ],
)
def test_known_fact_confidence_rejects_arbitrary_string(value: str) -> None:
    """Arbitrary natural-language confidence labels are rejected.

    Closes the door on narrator-improvised values like ``"high"`` or
    ``"likely"`` slipping through ``WorldStatePatch.discovered_facts``.
    """
    with pytest.raises(ValidationError):
        KnownFact(content="x", confidence=value)


def test_known_fact_confidence_rejects_empty_string() -> None:
    """Empty-string confidence is rejected (closes the boundary)."""
    with pytest.raises(ValidationError):
        KnownFact(content="x", confidence="")


@pytest.mark.parametrize(
    "value",
    [
        "certain",
        "suspected",
        "rumored",
        "discovered",
        "CERTAIN",
        "Certain ",
        " Certain",
    ],
)
def test_known_fact_confidence_rejects_mis_cased_or_padded(value: str) -> None:
    """Literal matching is case- and whitespace-sensitive.

    Rationale: the UI's ``Confidence`` type at ``GameStateProvider.tsx:32``
    uses exactly ``'Certain' | 'Suspected' | 'Rumored'`` capitalisation;
    accepting lowercase here would re-introduce wire-format drift.
    """
    with pytest.raises(ValidationError):
        KnownFact(content="x", confidence=value)


def test_known_fact_confidence_rejects_none() -> None:
    """``None`` is not a member of the literal — explicit rejection."""
    with pytest.raises(ValidationError):
        KnownFact(content="x", confidence=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC3 — JSON round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", sorted(CANONICAL_CONFIDENCES))
def test_known_fact_confidence_json_roundtrip(value: str) -> None:
    """Serialize → parse preserves the confidence literal verbatim.

    The narrator emits ``DiscoveredFact.fact`` as JSON inside
    ``WorldStatePatch``; the session then re-validates via
    ``KnownFact.model_validate``. A serializer that mangled the value
    (e.g. lower-cased it) would break that pipeline.
    """
    original = KnownFact(content="x", confidence=value)
    json_str = original.model_dump_json()
    restored = KnownFact.model_validate_json(json_str)
    assert restored.confidence == value


# ---------------------------------------------------------------------------
# AC4 — default value is canonical
# ---------------------------------------------------------------------------


def test_known_fact_default_confidence_is_canonical() -> None:
    """Constructing without ``confidence=`` yields a canonical value.

    The Literal default is ``"Certain"`` (the closest semantic match for the
    legacy ``"confirmed"`` high-confidence default; chosen during the 50-17
    GREEN phase). This test pins two invariants together:

    1. The default is one of the four canonical values (closed-set check).
    2. The default is **not** the legacy ``"confirmed"`` string — regression
       guard against reverting the migration.
    """
    kf = KnownFact(content="x")
    assert kf.confidence in CANONICAL_CONFIDENCES
    assert kf.confidence != "confirmed"


# ---------------------------------------------------------------------------
# AC5 — model_validate from dict (the production path)
# ---------------------------------------------------------------------------


def test_known_fact_model_validate_accepts_canonical_dict() -> None:
    """The ``session.apply_patch`` production path validates successfully.

    Mirrors ``sidequest/game/session.py:1164``:
        ``KnownFact.model_validate(df.fact)``
    where ``df.fact`` is a ``dict`` carried in
    ``WorldStatePatch.discovered_facts``.
    """
    kf = KnownFact.model_validate(
        {
            "content": "The Warden is in the mines",
            "confidence": "Discovered",
            "source": "ScenarioClue",
            "learned_turn": 7,
        }
    )
    assert kf.confidence == "Discovered"
    assert kf.source == "ScenarioClue"
    assert kf.learned_turn == 7


def test_known_fact_model_validate_rejects_legacy_confirmed_dict() -> None:
    """A narrator-emitted dict with the legacy value is rejected at the boundary.

    This is the load-bearing safety property: without it, the narrator could
    keep silently emitting ``"confirmed"`` and the server would keep silently
    accepting it. With the Literal in place, this path raises immediately,
    which is exactly the OTEL-lie-detector posture required by ADR-100.
    """
    with pytest.raises(ValidationError):
        KnownFact.model_validate({"content": "x", "confidence": "confirmed"})


def test_known_fact_model_validate_rejects_arbitrary_confidence_dict() -> None:
    """Narrator-improvised confidence values fail boundary validation."""
    with pytest.raises(ValidationError):
        KnownFact.model_validate({"content": "x", "confidence": "very-high"})


# ---------------------------------------------------------------------------
# Wiring — the field annotation is actually a Literal, not str
# ---------------------------------------------------------------------------


def test_known_fact_confidence_field_annotation_is_literal() -> None:
    """Confirm the annotation tightens at the type-system level, not only at runtime.

    A passing runtime validation test is not enough — if the annotation
    were still ``str`` with a runtime ``field_validator``, pyright/mypy
    callers would lose the narrowing benefit and call-site type errors
    would slip through. Pinning the annotation closes that loophole.

    Rule coverage (python.md #3 type-annotation gaps at boundaries):
    ``KnownFact`` is consumed at module boundaries (``session.apply_patch``,
    ``JournalEntry`` derivation, scenario clue intake) and so must carry
    its narrowing in the annotation, not behind a runtime check.
    """
    annotation = KnownFact.model_fields["confidence"].annotation
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    assert origin is typing.Literal, (
        f"KnownFact.confidence annotation origin is {origin!r}, "
        f"expected typing.Literal. Annotation: {annotation!r}"
    )
    assert set(args) == CANONICAL_CONFIDENCES, (
        f"KnownFact.confidence Literal members are {set(args)!r}, "
        f"expected {set(CANONICAL_CONFIDENCES)!r}"
    )


# ---------------------------------------------------------------------------
# Sanity — extra=forbid is preserved (unrelated field defence still holds)
# ---------------------------------------------------------------------------


def test_known_fact_still_forbids_extra_fields_after_enum_promotion() -> None:
    """The promotion does not weaken ``model_config = {"extra": "forbid"}``.

    Regression guard: a careless re-statement of ``model_config`` could
    drop the ``forbid`` setting, opening the door to silent passthrough
    of unknown narrator-emitted keys.
    """
    with pytest.raises(ValidationError):
        KnownFact.model_validate(
            {
                "content": "x",
                "confidence": "Certain",
                "unexpected_field": "should be rejected",
            }
        )
