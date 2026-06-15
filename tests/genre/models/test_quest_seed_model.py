"""RED tests — Story 117-3 (ADR-146) — the ``QuestSeed`` authoring schema.

A new ``QuestSeed`` pydantic sub-model in
``sidequest.genre.models.narrative`` is declared as a typed field
``quest_seed`` on ``OpeningTone`` (``narrative.py``). Because ``OpeningTone``
is ``extra="forbid"`` the seed is a *typed field on the model*, not a
free-form passthrough — a typo in a sub-field fails loud at world load.

Contract under test (TEA-defined for Dev), straight from ADR-146 §1:

* ``QuestSeed`` carries ``quest_id`` / ``title`` / ``objective`` (required, map
  1:1 onto ``QuestEntry`` + ``RecordQuestArgs``), plus ``stakes=""`` /
  ``anchor: str|None=None`` / ``giver=""`` (optional, defaulted).
* ``QuestSeed`` itself is ``extra="forbid"`` — a typo'd sub-field fails loud.
* ``OpeningTone.quest_seed`` is an optional typed field (``None`` by default —
  authoring a seed is optional per ADR-146 "Neutral"), and parses the worked
  ``perseus_cloud`` YAML shape from the ADR.
* Because ``OpeningTone`` is ``extra="forbid"``, a *misspelled* ``quest_seed``
  key (e.g. ``quest_seeed``) is rejected at the tone level too.

These tests import ``QuestSeed`` which does NOT exist yet — they FAIL at
collection (ImportError) until Dev (117-3 GREEN) adds the sub-model and the
``OpeningTone.quest_seed`` field. That is the intended RED.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.narrative import OpeningTone, QuestSeed

# The worked exemplar from ADR-146 §1 (perseus_cloud floor-boss hook).
_FLOOR_BOSS_SEED = {
    "quest_id": "floor_boss_missing_person",
    "title": "The Floor-Boss's Missing Person",
    "objective": (
        "Find out who the Conglomerate floor-boss has lost in the under-levels "
        "of New Kowloon, and decide whether to bring them back."
    ),
    "stakes": "a corporate favour owed — or a corporate enemy made — in a sector you can't leave",
    "giver": "the Conglomerate floor-boss",
}


# --- QuestSeed required fields + defaults (ADR-146 §1) -----------------------


def test_quest_seed_parses_full_shape() -> None:
    """The full authored shape parses, and every field round-trips."""
    seed = QuestSeed(**_FLOOR_BOSS_SEED)
    assert seed.quest_id == "floor_boss_missing_person"
    assert seed.title == "The Floor-Boss's Missing Person"
    assert seed.objective.startswith("Find out who")
    assert seed.stakes.startswith("a corporate favour")
    assert seed.giver == "the Conglomerate floor-boss"
    assert seed.anchor is None  # optional, omitted in the exemplar


def test_quest_seed_minimal_shape_defaults() -> None:
    """Only quest_id/title/objective are required; the rest default cleanly.

    ``stakes`` and ``giver`` default to ``""`` and ``anchor`` to ``None`` —
    these defaults map onto ``QuestEntry`` (which the minting handler builds)
    without the author having to spell them out.
    """
    seed = QuestSeed(
        quest_id="q_min",
        title="A Small Job",
        objective="Do the small job.",
    )
    assert seed.stakes == ""
    assert seed.giver == ""
    assert seed.anchor is None


def test_quest_seed_accepts_anchor() -> None:
    seed = QuestSeed(
        quest_id="q_anchored",
        title="Anchored Quest",
        objective="Reach the beacon.",
        anchor="under_levels_beacon",
    )
    assert seed.anchor == "under_levels_beacon"


@pytest.mark.parametrize("missing", ["quest_id", "title", "objective"])
def test_quest_seed_requires_core_fields(missing: str) -> None:
    """quest_id/title/objective are required — omitting any one fails loud."""
    payload = {
        "quest_id": "q1",
        "title": "T",
        "objective": "O",
    }
    del payload[missing]
    with pytest.raises(ValidationError):
        QuestSeed(**payload)


# --- extra="forbid" — a typo in a sub-field fails loud (ADR-146 §1) ----------


def test_quest_seed_rejects_unknown_field() -> None:
    """QuestSeed is ``extra='forbid'`` — a misspelled sub-field is a content
    bug caught at world load, not silently dropped (the whole point of making
    the hook a typed field rather than a free-form passthrough)."""
    with pytest.raises(ValidationError):
        QuestSeed(
            quest_id="q1",
            title="T",
            objective="O",
            giverr="oops typo'd giver",  # extra key
        )


# --- OpeningTone.quest_seed typed field (ADR-146 §1) -------------------------


def test_opening_tone_quest_seed_defaults_none() -> None:
    """Authoring a seed is optional: a tone with no quest_seed has it None and
    behaves exactly as today."""
    tone = OpeningTone(register="noir", complication="a floor-boss is watching")
    assert tone.quest_seed is None


def test_opening_tone_parses_nested_quest_seed() -> None:
    """The ADR's worked YAML shape parses: tone.quest_seed coerces into a typed
    QuestSeed instance, not a bare dict."""
    tone = OpeningTone.model_validate(
        {
            "register": "noir",
            "complication": "a Conglomerate floor-boss two tiers up has been watching you",
            "quest_seed": _FLOOR_BOSS_SEED,
        }
    )
    assert isinstance(tone.quest_seed, QuestSeed)
    assert tone.quest_seed.quest_id == "floor_boss_missing_person"


def test_opening_tone_rejects_misspelled_quest_seed_key() -> None:
    """OpeningTone is ``extra='forbid'`` (narrative.py:114). A misspelled
    ``quest_seed`` key (``quest_seeed``) must be rejected at the tone level so
    a typo never silently drops the entire authored hook."""
    with pytest.raises(ValidationError):
        OpeningTone.model_validate(
            {
                "register": "noir",
                "quest_seeed": _FLOOR_BOSS_SEED,  # typo: three e's
            }
        )


def test_opening_tone_propagates_nested_seed_typo() -> None:
    """A typo *inside* the nested seed (QuestSeed extra='forbid') also fails at
    the tone level — the typed nesting validates all the way down."""
    bad = dict(_FLOOR_BOSS_SEED)
    bad["givr"] = bad.pop("giver")  # typo'd sub-field
    with pytest.raises(ValidationError):
        OpeningTone.model_validate({"quest_seed": bad})
