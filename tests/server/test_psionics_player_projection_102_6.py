"""Story 102-6 RED — player-facing Effort + System Strain legibility.

Sebastien/Jade design rubric (CLAUDE.md, context-story-102-6 guardrail #20):
mechanical resolution must be legible in PLAYER-FACING surfaces. A psychic's
Effort pool (committed / free / max) and System Strain (current / max) must reach
the party panel via ``PartyMember`` — today ``party_member_from_character``
projects HP, rig composure, and injuries, but NOT Effort or Strain.

This mirrors the story 53-5 rig_composure precedent
(``test_party_member_rig_composure.py``) exactly: nullable protocol fields
following the ``rig_composure_*`` pattern (``include_when_none`` so the UI can
distinguish "no psionics" from "0 Effort committed"), plus the views extraction.

SCOPE NOTE: the protocol model + the views.py population are server-side (in
scope: server,content). The UI rendering of these fields is a ui-repo change —
OUT of scope per the story header; flagged to SM as a Delivery Finding.
"""

from __future__ import annotations

import pytest

from sidequest.protocol.models import PartyMember
from sidequest.protocol.types import NonBlankString


def _base_kwargs() -> dict:
    return dict(
        player_id=NonBlankString("player-1"),
        name=NonBlankString("Keith"),
        character_name=NonBlankString("Sael"),
        current_hp=8,
        max_hp=10,
        statuses=[],
        level=2,
        portrait_url=None,
        current_location=None,
        sheet=None,
        inventory=None,
        class_reference_url=None,
        **{"class": NonBlankString("Psychic")},
    )


# ---------------------------------------------------------------------------
# Protocol model — Effort pool fields on PartyMember
# ---------------------------------------------------------------------------


class TestEffortProjectionFields:
    def test_party_member_accepts_effort_fields(self) -> None:
        pm = PartyMember(
            **_base_kwargs(),
            effort_available=2,
            effort_committed=1,
            effort_max=3,
        )
        assert pm.effort_available == 2
        assert pm.effort_committed == 1
        assert pm.effort_max == 3

    def test_party_member_effort_defaults_to_none_for_non_psychics(self) -> None:
        """A non-psychic projects None — distinct from a psychic at 0 free."""
        pm = PartyMember(**_base_kwargs())
        assert pm.effort_available is None
        assert pm.effort_max is None

    def test_party_member_zero_free_effort_is_distinct_from_none(self) -> None:
        pm = PartyMember(
            **_base_kwargs(),
            effort_available=0,
            effort_committed=3,
            effort_max=3,
        )
        assert pm.effort_available == 0
        assert pm.effort_committed == 3

    def test_effort_fields_serialize_on_wire(self) -> None:
        pm = PartyMember(**_base_kwargs(), effort_available=2, effort_committed=1, effort_max=3)
        d = pm.model_dump(by_alias=True)
        assert d["effort_available"] == 2
        assert d["effort_committed"] == 1
        assert d["effort_max"] == 3


# ---------------------------------------------------------------------------
# Protocol model — System Strain fields on PartyMember
# ---------------------------------------------------------------------------


class TestStrainProjectionFields:
    def test_party_member_accepts_strain_fields(self) -> None:
        pm = PartyMember(
            **_base_kwargs(),
            system_strain_current=2,
            system_strain_max=10,
        )
        assert pm.system_strain_current == 2
        assert pm.system_strain_max == 10

    def test_party_member_strain_defaults_to_none(self) -> None:
        pm = PartyMember(**_base_kwargs())
        assert pm.system_strain_current is None
        assert pm.system_strain_max is None

    def test_strain_fields_serialize_on_wire(self) -> None:
        pm = PartyMember(**_base_kwargs(), system_strain_current=3, system_strain_max=10)
        d = pm.model_dump(by_alias=True)
        assert d["system_strain_current"] == 3
        assert d["system_strain_max"] == 10


# ---------------------------------------------------------------------------
# Wiring: party_member_from_character projects the pools
#
# Mirrors the 53-5 rig_composure precedent: the heavy handler/_SessionData
# fixture is built by Dev in GREEN. The Delivery Finding records that the
# views.py population (not just the protocol fields) must be wired — otherwise
# the fields exist but never carry data (CLAUDE.md "Verify Wiring, Not Just
# Existence").
# ---------------------------------------------------------------------------


class TestEffortStrainExtraction:
    def test_party_member_from_character_projects_psychic_effort(self) -> None:
        pytest.skip(
            "Needs handler/_SessionData fixture for party_member_from_character "
            "with a psychic (seeded core.effort) — Dev wires the views.py "
            "extraction in GREEN (see Delivery Finding)."
        )

    def test_party_member_from_character_projects_system_strain(self) -> None:
        pytest.skip(
            "Needs handler/_SessionData fixture for party_member_from_character "
            "with a strain-bearing core — Dev wires the views.py extraction in "
            "GREEN (see Delivery Finding)."
        )

    def test_party_member_from_character_non_psychic_pools_are_none(self) -> None:
        pytest.skip(
            "Needs handler/_SessionData fixture — Dev wires the views.py "
            "extraction in GREEN (see Delivery Finding)."
        )
