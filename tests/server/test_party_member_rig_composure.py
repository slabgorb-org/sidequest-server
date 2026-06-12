"""Story 53-5: PartyMember rig_composure + injury_tags protocol fields.

Tests the protocol contract for surfacing rig pool state and crash
injury tags in PARTY_STATUS messages. RED phase — all tests fail
until the protocol model and views extraction are implemented.
"""

from __future__ import annotations

import pytest

from sidequest.protocol.models import PartyMember
from sidequest.protocol.types import NonBlankString

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_kwargs() -> dict:
    """Minimal valid PartyMember kwargs — no rig pool, no injuries."""
    return dict(
        player_id=NonBlankString("player-1"),
        name=NonBlankString("Keith"),
        character_name=NonBlankString("Grizzled Drifter"),
        current_hp=4,
        max_hp=5,
        statuses=[],
        level=3,
        portrait_url=None,
        current_location=None,
        sheet=None,
        inventory=None,
        class_reference_url=None,
        **{"class": NonBlankString("Road Warrior")},
    )


# ---------------------------------------------------------------------------
# AC-1: Protocol model — rig_composure fields on PartyMember
# ---------------------------------------------------------------------------


class TestRigComposureProtocolFields:
    """PartyMember must carry nullable rig_composure_current /
    rig_composure_max fields for the UI to render the rig bar."""

    def test_party_member_accepts_rig_composure_fields(self) -> None:
        """PartyMember with explicit rig_composure values round-trips."""
        pm = PartyMember(
            **_base_kwargs(),
            rig_composure_current=8,
            rig_composure_max=12,
        )
        assert pm.rig_composure_current == 8
        assert pm.rig_composure_max == 12

    def test_party_member_rig_composure_defaults_to_none(self) -> None:
        """When a character has no rig, composure fields default to None."""
        pm = PartyMember(**_base_kwargs())
        assert pm.rig_composure_current is None
        assert pm.rig_composure_max is None

    def test_party_member_rig_composure_zero_is_valid(self) -> None:
        """Composure at zero (wrecked rig) is distinct from None (no rig)."""
        pm = PartyMember(
            **_base_kwargs(),
            rig_composure_current=0,
            rig_composure_max=10,
        )
        assert pm.rig_composure_current == 0
        assert pm.rig_composure_max == 10

    def test_party_member_serializes_rig_composure_in_dict(self) -> None:
        """model_dump includes the rig_composure fields for wire serialization."""
        pm = PartyMember(
            **_base_kwargs(),
            rig_composure_current=6,
            rig_composure_max=10,
        )
        d = pm.model_dump(by_alias=True)
        assert d["rig_composure_current"] == 6
        assert d["rig_composure_max"] == 10

    def test_party_member_serializes_none_rig_composure(self) -> None:
        """model_dump includes None composure for characters without rigs."""
        pm = PartyMember(**_base_kwargs())
        d = pm.model_dump(by_alias=True)
        assert d["rig_composure_current"] is None
        assert d["rig_composure_max"] is None


# ---------------------------------------------------------------------------
# AC-1: Protocol model — injury_tags field on PartyMember
# ---------------------------------------------------------------------------


class TestInjuryTagsProtocolField:
    """PartyMember must carry an injury_tags list for crash-related
    status display in the UI."""

    def test_party_member_accepts_injury_tags(self) -> None:
        """PartyMember with explicit injury tags round-trips."""
        pm = PartyMember(
            **_base_kwargs(),
            injury_tags=["injury", "dismounted"],
        )
        assert pm.injury_tags == ["injury", "dismounted"]

    def test_party_member_injury_tags_defaults_to_empty(self) -> None:
        """When no injuries, defaults to empty list (not None)."""
        pm = PartyMember(**_base_kwargs())
        assert pm.injury_tags == []

    def test_party_member_serializes_injury_tags(self) -> None:
        """model_dump includes injury_tags for wire."""
        pm = PartyMember(
            **_base_kwargs(),
            injury_tags=["injury"],
        )
        d = pm.model_dump(by_alias=True)
        assert d["injury_tags"] == ["injury"]


# ---------------------------------------------------------------------------
# Wiring: party_member_from_character extracts rig pool
# ---------------------------------------------------------------------------


class TestPartyMemberRigPoolExtraction:
    """party_member_from_character must project rig_pool and injury
    statuses into the new PartyMember fields."""

    def test_party_member_from_character_has_rig_fields(self) -> None:
        """Verify the PartyMember built from a rig-equipped character
        carries rig_composure_current and rig_composure_max.

        This is a behavioral wiring test — it drives the real function
        with a fixture and asserts the output shape, not source text.
        The dev must build the fixture infrastructure to make this pass.
        """
        pytest.skip(
            "Needs fixture infrastructure for party_member_from_character "
            "with a rig-pool-equipped character — Dev implements in GREEN"
        )

    def test_party_member_from_character_no_rig_is_none(self) -> None:
        """PartyMember from a character without rig_pool has None composure."""
        pytest.skip("Needs fixture infrastructure — Dev implements in GREEN")

    def test_party_member_from_character_crash_injuries_in_tags(self) -> None:
        """PartyMember from a crashed character carries injury statuses
        in injury_tags."""
        pytest.skip("Needs fixture infrastructure — Dev implements in GREEN")
