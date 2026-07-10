"""RED (spec §2 A2, plan task 16): ``Region.weather_zone`` typed field.

Binds a cartography region to a climate zone in the world's ``weather.yaml``.
``Region`` already has ``extra="allow"``, so an authored ``weather_zone`` key
round-trips into ``__pydantic_extra__`` TODAY — meaning a naive
``r.weather_zone == "..."`` assertion PASSES before the field exists and is a
false-RED. The load-bearing RED assertions here interrogate the *declared*
schema (``model_fields``) and the *default* (omitted → ``None``, which raises
``AttributeError`` today because the key is absent from both fields and the
extras bag). Both flip only once task 16 adds the explicit typed field.
"""

from __future__ import annotations

from sidequest.genre.models.world import Region


def test_region_declares_weather_zone_as_typed_field() -> None:
    """The distinguisher between an extras-bag entry and a real field.

    ``model_fields`` lists declared fields only — an ``extra="allow"`` bag
    entry never appears there. False today; True once task 16 declares
    ``weather_zone: str | None = None`` on ``Region``.
    """
    assert "weather_zone" in Region.model_fields


def test_region_weather_zone_defaults_none() -> None:
    """Omitted ``weather_zone`` must resolve to ``None``, not raise.

    Today the attribute is absent from both declared fields and the extras
    bag, so access raises ``AttributeError`` (genuine RED). After task 16 the
    declared default makes it ``None``.
    """
    r = Region.model_validate({"name": "The Bridge", "summary": "s", "description": "d"})
    assert r.weather_zone is None


def test_region_accepts_weather_zone() -> None:
    """An authored ``weather_zone`` round-trips to the typed attribute.

    (This alone passes today via the extras bag — it is the AC companion to
    the schema assertion above, not the RED driver.)
    """
    r = Region.model_validate(
        {
            "name": "Castle Ross",
            "summary": "s",
            "description": "d",
            "weather_zone": "highland_pass",
        }
    )
    assert r.weather_zone == "highland_pass"
