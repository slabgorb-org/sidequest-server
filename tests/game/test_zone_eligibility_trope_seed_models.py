"""RED — Story 157-4, AC-3.1: the ``factions`` content tag on tropes and seeds.

Seams 3 & 4 of the faction/zone-scoped eligibility design
(``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``,
ADR-059 amendment) gate trope activation and seed-deck draws through the
``zone_eligibility`` predicate. That predicate reads the content item's
``factions`` tag — which does not exist on :class:`TropeDefinition` or
:class:`SeedTrope` yet.

157-2 added the identical additive field to ``BestiaryEntry`` / ``ManualEncounter``
/ ``ManualNpc`` (see ``tests/game/test_zone_eligibility.py``); this file pins the
same contract for the trope/seed models so the Seam 3 & 4 gates have something to
read. The contract is deliberately identical:

- additive + default-empty (``Field(default_factory=list)``), so existing
  trope/seed YAML with no ``factions:`` keeps parsing (both models are
  ``extra="forbid"`` — the field must EXIST or a ``factions:`` key fails to load);
- round-trips a provided value;
- per-instance default (lang-review #2 — never a shared mutable singleton).

RED until 157-4's GREEN adds ``factions`` to both models.
"""

from __future__ import annotations

import pytest

from sidequest.genre.models.tropes import SeedTrope, TropeDefinition

LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"


# ---------------------------------------------------------------------------
# TropeDefinition.factions
# ---------------------------------------------------------------------------


def test_trope_definition_factions_defaults_empty() -> None:
    """Additive field: an existing trope (no ``factions:``) keeps parsing — the
    default is an empty list, NOT None."""
    assert TropeDefinition(name="A Brewing Storm").factions == []


def test_trope_definition_factions_round_trips() -> None:
    assert TropeDefinition(name="A Brewing Storm", factions=[HOUYHNHNM]).factions == [HOUYHNHNM]


def test_trope_definition_factions_not_shared_between_instances() -> None:
    """lang-review #2 (mutable default): the default-empty list must be
    per-instance (``Field(default_factory=list)``), never a shared singleton."""
    a = TropeDefinition(name="A")
    b = TropeDefinition(name="B")
    a.factions.append(LILLIPUT)
    assert b.factions == [], "factions default leaked across TropeDefinition instances"


# ---------------------------------------------------------------------------
# SeedTrope.factions
# ---------------------------------------------------------------------------


def test_seed_trope_factions_defaults_empty() -> None:
    assert SeedTrope(id="s1", name="Seed 1").factions == []


def test_seed_trope_factions_round_trips() -> None:
    seed = SeedTrope(id="s1", name="Seed 1", factions=[LILLIPUT])
    assert seed.factions == [LILLIPUT]


def test_seed_trope_factions_not_shared_between_instances() -> None:
    a = SeedTrope(id="a", name="A")
    b = SeedTrope(id="b", name="B")
    a.factions.append(HOUYHNHNM)
    assert b.factions == [], "factions default leaked across SeedTrope instances"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
