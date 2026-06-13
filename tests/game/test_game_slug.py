from datetime import date

import pytest

from sidequest.game.game_slug import InvalidSlugError, generate_slug, parse_slug
from sidequest.game.persistence import GameMode

# ---------------------------------------------------------------------------
# Unique-slug contract (2026-06-13).
#
# The deterministic ``<date>-<world>[-mp]`` slug was replaced by a unique slug
# ``<date>-<world>[-mp]-<token>``. Rationale (operator call, sq-playtest
# 2026-06-13): the deterministic slug's only value was the human "when did this
# start" date signal — and its collision-resume behavior silently inherited a
# prior run's durable seat roster on a reused per-day MP slug, deadlocking the
# turn barrier on a phantom seat (the Kael deadlock). Real co-play joins by
# sharing the exact ``/play/<slug>`` link, not by re-deriving a deterministic
# slug, so uniqueness costs nothing and removes the inheritance class entirely.
#
# The date prefix and ``-mp`` mode marker are preserved (the date is the kept
# signal); only the trailing ``-<token>`` is new. ``token=`` is injectable so
# the generation is deterministic under test.
# ---------------------------------------------------------------------------

_TOKEN = "a1b2c3d4"


def test_generate_slug_keeps_date_world_and_appends_token():
    assert (
        generate_slug(world_slug="moldharrow-keep", today=date(2026, 4, 22), token=_TOKEN)
        == "2026-04-22-moldharrow-keep-a1b2c3d4"
    )


def test_generate_slug_appends_mp_before_token_for_multiplayer():
    assert (
        generate_slug(
            world_slug="mawdeep",
            today=date(2026, 4, 24),
            mode=GameMode.MULTIPLAYER,
            token=_TOKEN,
        )
        == "2026-04-24-mawdeep-mp-a1b2c3d4"
    )


def test_generate_slug_rejects_empty_world():
    with pytest.raises(ValueError):
        generate_slug(world_slug="", today=date(2026, 4, 22))


def test_generate_slug_mints_unique_token_when_unspecified():
    """Two calls with identical (world, day, mode) must NOT collide — the whole
    point of the change. Without an injected token, each call mints a fresh one."""
    today = date(2026, 4, 24)
    a = generate_slug(world_slug="mawdeep", today=today, mode=GameMode.MULTIPLAYER)
    b = generate_slug(world_slug="mawdeep", today=today, mode=GameMode.MULTIPLAYER)
    assert a != b, "same world+day+mode must mint distinct slugs (no deterministic resume)"
    # Both still carry the readable date + world + mode prefix.
    assert a.startswith("2026-04-24-mawdeep-mp-")
    assert b.startswith("2026-04-24-mawdeep-mp-")


def test_generated_token_is_lowercase_hex():
    slug = generate_slug(world_slug="mawdeep", today=date(2026, 4, 24))
    token = slug.rsplit("-", 1)[-1]
    assert token and all(c in "0123456789abcdef" for c in token), token
    assert len(token) >= 6


def test_solo_and_multiplayer_slugs_do_not_collide():
    """Same world + same day in different modes must produce distinct slugs —
    the ``-mp`` marker is preserved so mode is never silently downgraded."""
    today = date(2026, 4, 24)
    solo = generate_slug(world_slug="mawdeep", today=today, mode=GameMode.SOLO, token=_TOKEN)
    mp = generate_slug(world_slug="mawdeep", today=today, mode=GameMode.MULTIPLAYER, token=_TOKEN)
    assert solo != mp
    assert solo == "2026-04-24-mawdeep-a1b2c3d4"
    assert mp == "2026-04-24-mawdeep-mp-a1b2c3d4"


# ---------------------------------------------------------------------------
# parse_slug — extracts the kept signal (date, world, mode) from BOTH the new
# tokened slugs and legacy deterministic slugs (durable saves predate the
# change and must still parse).
# ---------------------------------------------------------------------------


def test_parse_slug_new_tokened_solo():
    parsed = parse_slug("2026-04-22-moldharrow-keep-a1b2c3d4")
    assert parsed.date == date(2026, 4, 22)
    assert parsed.world_slug == "moldharrow-keep"
    assert parsed.mode == GameMode.SOLO


def test_parse_slug_new_tokened_multiplayer_with_dashed_world():
    parsed = parse_slug("2026-04-24-the-iron-city-mp-a1b2c3d4")
    assert parsed.world_slug == "the-iron-city"
    assert parsed.mode == GameMode.MULTIPLAYER


def test_parse_slug_legacy_deterministic_solo_still_parses():
    parsed = parse_slug("2026-04-22-moldharrow-keep")
    assert parsed.date == date(2026, 4, 22)
    assert parsed.world_slug == "moldharrow-keep"
    assert parsed.mode == GameMode.SOLO


def test_parse_slug_legacy_deterministic_multiplayer_still_parses():
    parsed = parse_slug("2026-04-24-mawdeep-mp")
    assert parsed.world_slug == "mawdeep"
    assert parsed.mode == GameMode.MULTIPLAYER


def test_parse_slug_rejects_missing_date():
    with pytest.raises(InvalidSlugError):
        parse_slug("moldharrow-keep")


def test_parse_slug_rejects_malformed_date():
    with pytest.raises(InvalidSlugError):
        parse_slug("2026-13-40-moldharrow")


def test_generate_then_parse_roundtrips_the_signal():
    slug = generate_slug(world_slug="the-iron-city", today=date(2026, 12, 1), token=_TOKEN)
    parsed = parse_slug(slug)
    assert parsed.date == date(2026, 12, 1)
    assert parsed.world_slug == "the-iron-city"
    assert parsed.mode == GameMode.SOLO
