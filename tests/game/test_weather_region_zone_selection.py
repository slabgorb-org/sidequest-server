"""RED (spec §2 A2, plan task 17): region-aware bootstrap zone selection.

``_select_zone_for_region`` binds weather to geography at session start: the
STARTING region's ``weather_zone`` wins over the genre hardcode. A declared but
unknown zone fails LOUD (SOUL.md / CLAUDE.md "No Silent Fallbacks") rather than
drifting to a default. Absent a region zone, it defers to the existing
``_select_zone_season`` genre-override / first-zone behavior.

RED today: ``_select_zone_for_region`` does not exist → ImportError.
"""

from __future__ import annotations

import pytest

from sidequest.game.weather import ClimateRulesFile

# Two zones; ``glen_floor`` is declared first so the genre-override AND the
# first-zone default both resolve to it — the fallback assertion is robust to
# either path in ``_select_zone_season``.
_RULES = ClimateRulesFile.model_validate(
    {
        "climate_zones": {
            "glen_floor": {
                "seasons": {
                    "autumn": {"temp_range": [5, 12], "conditions": ["smirr"], "weights": [1]}
                }
            },
            "highland_pass": {
                "seasons": {
                    "autumn": {"temp_range": [-2, 6], "conditions": ["blizzard"], "weights": [1]}
                }
            },
        }
    }
)


class _Region:
    def __init__(self, wz: str | None) -> None:
        self.weather_zone = wz


class _Cart:
    def __init__(self, start: str, regions: dict[str, _Region]) -> None:
        self.starting_region = start
        self.regions = regions


def test_region_weather_zone_wins_over_default() -> None:
    """The starting region's declared zone beats the genre default."""
    from sidequest.game.world_grounding_bootstrap import _select_zone_for_region

    cart = _Cart("castle_ross", {"castle_ross": _Region("highland_pass")})
    zone = _select_zone_for_region(_RULES, cart, genre_slug="tea_and_murder")
    assert zone == "highland_pass"


def test_no_region_zone_falls_back_to_genre_default() -> None:
    """No region zone → existing genre-override selection (glen_floor)."""
    from sidequest.game.world_grounding_bootstrap import _select_zone_for_region

    cart = _Cart("the_bridge", {"the_bridge": _Region(None)})
    zone = _select_zone_for_region(_RULES, cart, genre_slug="tea_and_murder")
    assert zone == "glen_floor"


def test_declared_but_invalid_region_zone_fails_loud() -> None:
    """A region zone that is not a real climate zone must RAISE, never fall back.

    This is the No Silent Fallbacks guardrail: a typo in cartography must
    surface loudly at bootstrap, not silently pick some default weather.
    """
    from sidequest.game.world_grounding_bootstrap import _select_zone_for_region

    cart = _Cart("castle_ross", {"castle_ross": _Region("stratosphere")})
    with pytest.raises(ValueError):
        _select_zone_for_region(_RULES, cart, genre_slug="tea_and_murder")


def test_none_cartography_falls_back_to_genre_default() -> None:
    """A world with no cartography at all defers cleanly to the genre default.

    ``load_world_grounding`` passes ``cartography=None`` for non-region worlds;
    the selector must not crash on the missing structure.
    """
    from sidequest.game.world_grounding_bootstrap import _select_zone_for_region

    zone = _select_zone_for_region(_RULES, None, genre_slug="tea_and_murder")
    assert zone == "glen_floor"
