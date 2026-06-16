from datetime import date

import pytest

from sidequest.game.game_slug import InvalidSlugError, generate_slug, parse_slug
from sidequest.game.persistence import GameMode


def test_generate_slug_uses_date_and_world():
    assert (
        generate_slug(world_slug="moldharrow-keep", today=date(2026, 4, 22))
        == "2026-04-22-moldharrow-keep"
    )


def test_generate_slug_rejects_empty_world():
    with pytest.raises(ValueError):
        generate_slug(world_slug="", today=date(2026, 4, 22))


def test_parse_slug_roundtrip():
    parsed = parse_slug("2026-04-22-moldharrow-keep")
    assert parsed.date == date(2026, 4, 22)
    assert parsed.world_slug == "moldharrow-keep"
    assert parsed.mode == GameMode.SOLO


def test_parse_slug_world_with_dashes():
    parsed = parse_slug("2026-12-01-the-iron-city")
    assert parsed.world_slug == "the-iron-city"
    assert parsed.mode == GameMode.SOLO


def test_parse_slug_rejects_missing_date():
    with pytest.raises(InvalidSlugError):
        parse_slug("moldharrow-keep")


def test_parse_slug_rejects_malformed_date():
    with pytest.raises(InvalidSlugError):
        parse_slug("2026-13-40-moldharrow")


def test_generate_slug_appends_mp_for_multiplayer():
    assert (
        generate_slug(world_slug="mawdeep", today=date(2026, 4, 24), mode=GameMode.MULTIPLAYER)
        == "2026-04-24-mawdeep-mp"
    )


def test_generate_slug_solo_unchanged_when_mode_explicit():
    assert (
        generate_slug(world_slug="mawdeep", today=date(2026, 4, 24), mode=GameMode.SOLO)
        == "2026-04-24-mawdeep"
    )


def test_solo_and_multiplayer_slugs_do_not_collide():
    """Same world + same day in different modes must produce distinct slugs.

    Without this, multiplayer is silently downgraded to solo on collision.
    """
    today = date(2026, 4, 24)
    solo = generate_slug(world_slug="mawdeep", today=today, mode=GameMode.SOLO)
    mp = generate_slug(world_slug="mawdeep", today=today, mode=GameMode.MULTIPLAYER)
    assert solo != mp


def test_parse_slug_extracts_multiplayer_mode():
    parsed = parse_slug("2026-04-24-mawdeep-mp")
    assert parsed.world_slug == "mawdeep"
    assert parsed.mode == GameMode.MULTIPLAYER


def test_parse_slug_multiplayer_with_dashed_world():
    parsed = parse_slug("2026-04-24-the-iron-city-mp")
    assert parsed.world_slug == "the-iron-city"
    assert parsed.mode == GameMode.MULTIPLAYER
