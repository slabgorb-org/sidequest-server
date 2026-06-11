"""Tests for genre pack models."""

from sidequest.genre.models.pack import PortraitManifestEntry


def test_portrait_manifest_entry_picker_fields():
    """player_picker entries carry id/culture/archetype/sex/backdrop_poi."""
    e = PortraitManifestEntry.model_validate(
        {
            "name": "Hegemony Officer",
            "type": "player_picker",
            "id": "picker_hegemonic_officer_f01",
            "culture": "hegemonic",
            "archetype": "ruler",
            "sex": "female",
            "backdrop_poi": "customs_concourse",
            "appearance": "stern, silver-templed",
        }
    )
    assert e.character_type == "player_picker"
    assert e.id == "picker_hegemonic_officer_f01"
    assert e.culture == "hegemonic"
    assert e.archetype == "ruler"
    assert e.sex == "female"
    assert e.backdrop_poi == "customs_concourse"


def test_portrait_manifest_entry_defaults_blank():
    """Existing NPC entries without picker fields still parse, fields blank."""
    e = PortraitManifestEntry.model_validate({"name": "Rux", "type": "npc_major"})
    assert e.id == ""
    assert e.culture == ""
    assert e.archetype == ""
    assert e.sex == ""
    assert e.backdrop_poi == ""
