from __future__ import annotations

from sidequest.genre.models.world import CartographyConfig
from sidequest.server.reference_projection import build_lore_map_section


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
