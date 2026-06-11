"""Tests for genre pack models."""

from sidequest.genre.models.pack import PortraitManifestEntry, picker_portrait_slug


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


def test_picker_portrait_slug_explicit_id_wins():
    """An explicit ``id`` is the slug, used verbatim (generator parity:
    ``char.get("id") or _slugify_name(name)``)."""
    e = PortraitManifestEntry.model_validate(
        {
            "name": "Hegemony Officer",
            "type": "player_picker",
            "id": "picker_hegemonic_officer_f01",
        }
    )
    assert picker_portrait_slug(e) == "picker_hegemonic_officer_f01"


def test_picker_portrait_slug_name_fallback_is_slugified():
    """Entries without an ``id`` slugify the name EXACTLY like the render
    script's ``scripts/generate_portrait_images._slugify_name`` — lowercase,
    whitespace runs → ``_``, drop punctuation except ``_``/``-``. coyote_star's
    10 pickers ship id-less with space-separated names; a raw-name fallback
    would 404 against the generator's on-disk ``<slug>.png``."""
    e = PortraitManifestEntry.model_validate(
        {"name": "picker hegemonic officer f01", "type": "player_picker"}
    )
    assert picker_portrait_slug(e) == "picker_hegemonic_officer_f01"


def test_picker_portrait_slug_name_fallback_transform_details():
    """Pin the full transform: strip, lower, collapse whitespace runs,
    strip punctuation (keeping ``_`` and ``-``)."""
    e = PortraitManifestEntry.model_validate(
        {"name": "  Picker  O'Malley-Smith,  the 3rd  ", "type": "player_picker"}
    )
    assert picker_portrait_slug(e) == "picker_omalley-smith_the_3rd"
