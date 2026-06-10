"""Story 90-5 — direct unit coverage for the ``Bestiary`` / ``BestiaryEntry``
validators.

90-1 added the bestiary models with validators (non-empty ``entries``, no
duplicate ids, non-empty id/name, ``ge=1`` bounds on level/hp/armor_class) but
the only consumer is a *seam* test that uses ``model_construct`` — which BYPASSES
every validator. The Reviewer flagged this: the content-contract test parses raw
``yaml.safe_load`` dicts and never exercises ``Bestiary.model_validate``, so a
broken validator (e.g. a dropped dup-id check) ships green.

These are coverage locks: they pass on current ``develop`` (the validators exist
and work) and turn RED the moment a validator is weakened — pinning the content
boundary that keeps a malformed ``bestiary.yaml`` from reaching encountergen.

They also pin two *deliberate* 90-1 schema decisions:
* ``attack_bonus`` is INTENTIONALLY unbounded (negative bonuses are SRD-legal for
  weak creatures) — a bound here would be a regression, not a fix.
* The top-level shape is ``extra="forbid"`` (a stray key is an authoring typo),
  while per-entry is ``extra="allow"`` (SRD color: damage/move/morale/save).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry

_VALID_ENTRY = {
    "id": "rust_hound",
    "name": "Rust Hound",
    "level": 1,
    "hp": 6,
    "armor_class": 13,
    "attack_bonus": 2,
}


def _entry(**overrides: object) -> dict[str, object]:
    return {**_VALID_ENTRY, **overrides}


# ---------------------------------------------------------------------------
# BestiaryEntry — required fields, identity, ge=1 bounds
# ---------------------------------------------------------------------------


def test_entry_accepts_a_well_formed_block() -> None:
    entry = BestiaryEntry.model_validate(_VALID_ENTRY)
    assert entry.id == "rust_hound"
    assert entry.hp == 6


def test_entry_rejects_empty_id() -> None:
    with pytest.raises(ValidationError, match="id must not be empty"):
        BestiaryEntry.model_validate(_entry(id=""))


def test_entry_rejects_empty_name() -> None:
    with pytest.raises(ValidationError, match="name must not be empty"):
        BestiaryEntry.model_validate(_entry(name=""))


@pytest.mark.parametrize("field", ["level", "hp", "armor_class"])
def test_entry_rejects_below_one_on_bounded_fields(field: str) -> None:
    """level/hp/armor_class are ``Field(ge=1)`` — 0 (or negative) is invalid."""
    with pytest.raises(ValidationError):
        BestiaryEntry.model_validate(_entry(**{field: 0}))


def test_entry_allows_negative_attack_bonus() -> None:
    """Deliberate 90-1 decision: ``attack_bonus`` is UNBOUNDED — negative bonuses
    are SRD-legal (weak / unskilled creatures). A bound here would be a
    regression."""
    entry = BestiaryEntry.model_validate(_entry(attack_bonus=-1))
    assert entry.attack_bonus == -1


def test_entry_preserves_extra_srd_color() -> None:
    """Per-entry shape is ``extra="allow"`` — authors carry SRD color (damage,
    move, morale, save, ...) without a schema change, and it survives validation."""
    entry = BestiaryEntry.model_validate(_entry(morale=8, move="9m", save=15))
    dumped = entry.model_dump()
    assert dumped["morale"] == 8
    assert dumped["move"] == "9m"
    assert dumped["save"] == 15


# ---------------------------------------------------------------------------
# Bestiary — non-empty entries, unique ids, forbid stray top-level keys
# ---------------------------------------------------------------------------


def test_bestiary_accepts_a_well_formed_roster() -> None:
    bestiary = Bestiary.model_validate(
        {"entries": [_entry(id="a", name="A"), _entry(id="b", name="B")]}
    )
    assert [e.id for e in bestiary.entries] == ["a", "b"]


def test_bestiary_rejects_empty_entries() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        Bestiary.model_validate({"entries": []})


def test_bestiary_rejects_duplicate_entry_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Bestiary.model_validate(
            {"entries": [_entry(id="dup", name="One"), _entry(id="dup", name="Two")]}
        )


def test_bestiary_forbids_stray_top_level_keys() -> None:
    """Top-level shape is ``extra="forbid"`` — a stray key (e.g. a misspelled
    ``entires:``) is an authoring typo that must fail loud, not be silently
    dropped (No Silent Fallbacks)."""
    with pytest.raises(ValidationError):
        Bestiary.model_validate({"entries": [_VALID_ENTRY], "entires": []})
