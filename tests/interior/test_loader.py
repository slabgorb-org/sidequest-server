"""Cross-validation tests for stations on a chassis class."""

import pytest

from sidequest.genre.models.chassis import (
    ChassisClass,
    InteriorRoomSpec,
    StationSpec,
)
from sidequest.interior.loader import (
    InteriorLoaderError,
    validate_chassis_stations,
)


def _minimal_chassis(rooms, stations):
    return ChassisClass(
        id="test",
        display_name="Test",
        **{"class": "freighter"},
        provenance="voidborn_built",
        scale_band="vehicular",
        crew_model="flexible_roles",
        interior_rooms=rooms,
        stations=stations,
    )


def test_validate_passes_when_all_rooms_resolve():
    chassis = _minimal_chassis(
        rooms=[InteriorRoomSpec(id="cockpit", display_name="Cockpit")],
        stations=[
            StationSpec(id="helm", display_name="Helm", room="cockpit"),
        ],
    )
    validate_chassis_stations(chassis)  # no raise


def test_validate_passes_with_no_stations():
    chassis = _minimal_chassis(
        rooms=[InteriorRoomSpec(id="cockpit", display_name="Cockpit")],
        stations=[],
    )
    validate_chassis_stations(chassis)


def test_validate_raises_loud_on_unknown_room():
    chassis = _minimal_chassis(
        rooms=[InteriorRoomSpec(id="cockpit", display_name="Cockpit")],
        stations=[
            StationSpec(id="helm", display_name="Helm", room="bridge"),
        ],
    )
    with pytest.raises(InteriorLoaderError) as exc:
        validate_chassis_stations(chassis)
    msg = str(exc.value)
    assert "helm" in msg
    assert "bridge" in msg
    assert "cockpit" in msg  # valid rooms listed


@pytest.mark.integration
def test_voidborn_freighter_loads_clean_through_genre_loader():
    """Wiring test: real Kestrel chassis YAML passes validation at load time."""
    from pathlib import Path

    from sidequest.genre.loader import load_genre_pack

    repo_root = Path(__file__).resolve().parents[2]
    space_opera = repo_root.parent / "sidequest-content" / "genre_packs" / "space_opera"
    if not space_opera.exists():
        pytest.skip("space_opera content pack not present")

    pack = load_genre_pack(space_opera)
    assert pack.chassis_classes is not None
    voidborn = next(c for c in pack.chassis_classes.classes if c.id == "voidborn_freighter")
    assert len(voidborn.stations) == 4
    station_ids = {s.id for s in voidborn.stations}
    assert station_ids == {"command", "helm", "weapons", "engineering_controls"}
    # Validation runs as part of pack loading; if it raised, this test would
    # never have reached this assertion.
    validate_chassis_stations(voidborn)
