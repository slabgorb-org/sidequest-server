from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.models.world import CartographyConfig
from sidequest.server.reference_projection import build_lore_map_section, build_lore_projection


def _cart() -> CartographyConfig:
    return CartographyConfig.model_validate(
        {
            "starting_region": "harbor",
            "regions": {
                "harbor": {
                    "name": "The Harbor",
                    "summary": "A busy harbor district.",
                    "description": "Ships come and go.",
                    "adjacent": ["market", "ghost-isle"],  # ghost-isle is dangling
                    "entities": [
                        {
                            "id": "old-sten",
                            "label": "Old Sten",
                            "tier": "real_object",
                            "binding": {"kind": "npc", "ref": "old-sten"},
                        },
                        {
                            "id": "a-crate",
                            "label": "A Crate",
                            "tier": "flavor_only",
                        },
                    ],
                },
                "market": {
                    "name": "Night Market",
                    "summary": "A night market.",
                    "description": "Stalls and lanterns.",
                    "adjacent": ["harbor"],
                },
            },
        }
    )


def test_map_section_emits_topology_not_coordinates():
    section = build_lore_map_section(
        _cart(), pack="p", world="w", portrait_on_r2_slugs=frozenset({"old_sten"})
    )
    assert section["id"] == "map"
    assert section["label"] == "Map"
    assert section["starting_region"] == "harbor"
    assert section["edges"] == [["harbor", "market"]]
    assert section["dangling"] == [["harbor", "ghost-isle"]]
    blob = repr(section)
    assert '"x"' not in blob and "'x'" not in blob
    regions = {r["id"]: r for r in section["regions"]}
    assert set(regions["harbor"].keys()) == {"id", "name", "adjacent", "pins"}
    pins = regions["harbor"]["pins"]
    assert len(pins) == 1
    assert pins[0]["slug"] == "old_sten"
    assert pins[0]["label"] == "Old Sten"
    assert pins[0]["portrait_url"] is not None
    assert regions["market"]["pins"] == []


def test_map_pin_portrait_url_null_when_not_on_r2():
    section = build_lore_map_section(_cart(), pack="p", world="w", portrait_on_r2_slugs=frozenset())
    harbor = next(r for r in section["regions"] if r["id"] == "harbor")
    assert harbor["pins"][0]["portrait_url"] is None


def test_lore_projection_includes_map_when_cartography_present(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    (world_dir / "cartography.yaml").write_text(
        "starting_region: harbor\n"
        "regions:\n"
        "  harbor: {name: The Harbor, summary: Salt docks., description: Fog and hulls., adjacent: [market]}\n"
        "  market: {name: Night Market, summary: Lit stalls., description: Spice and smoke., adjacent: [harbor]}\n",
        encoding="utf-8",
    )
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    assert doc["schema_version"] == 1
    assert doc["pack"] == "p"
    assert doc["world"] == "w"
    assert [s["id"] for s in doc["sections"]] == ["map"]


def test_lore_projection_omits_map_when_no_cartography(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    assert doc["sections"] == []


def test_lore_projection_raises_on_malformed_cartography(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    (world_dir / "cartography.yaml").write_text("regions: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError):
        build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
