"""RED (spec §2, plan task 4): _load_map_treatment loads worlds/<slug>/map.yaml.

Absent map.yaml -> None (d3-dag fallback, by design, spec §2). Present but
malformed -> MapTreatmentConfig.model_validate raises (No Silent Fallbacks —
the load fails loud rather than degrading to the dag default).
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sidequest.genre.loader import _load_map_treatment


def _write(p: Path, data: dict) -> None:
    p.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_absent_map_yaml_returns_none(tmp_path: Path) -> None:
    assert _load_map_treatment(tmp_path) is None


def test_present_raster_map_yaml_loads(tmp_path: Path) -> None:
    _write(
        tmp_path / "map.yaml",
        {
            "treatment": "raster",
            "image": "sheet.jpg",
            "provenance": {
                "source": "OS",
                "date": "1900",
                "archive": "NLS",
                "pd_basis": "Crown copyright expired",
            },
            "node_anchors": {"r1": [10, 20]},
        },
    )
    mt = _load_map_treatment(tmp_path)
    assert mt is not None
    assert mt.treatment == "raster"
    assert mt.node_anchors["r1"] == [10, 20]


# --- Rule-enforcement test (Argus): No Silent Fallbacks (AC task 4) ----------


def test_malformed_map_yaml_raises_no_silent_fallback(tmp_path: Path) -> None:
    """A present-but-malformed map.yaml must FAIL LOUD at load, never degrade
    silently to the dag fallback (that fallback is reserved for an ABSENT
    map.yaml). An unknown treatment kind is the canonical malformation."""
    _write(tmp_path / "map.yaml", {"treatment": "hologram"})
    with pytest.raises(ValidationError):
        _load_map_treatment(tmp_path)
