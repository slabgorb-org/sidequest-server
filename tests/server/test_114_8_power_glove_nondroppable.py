"""Story 114-8 — power_glove survives the non-droppable genre↔world merge.

ADR-145 lists 114-8 as: "Fix the power_glove broken-ref regression via the D3
non-droppable baseline." The original audit defect: seaboard_of_saints shipped its
own `inventory.yaml`, which (under the old epic-94 wholesale-replace seam) dropped
the genre catalog entirely — including the chargen artifact `power_glove` — so a
character who picked it at chargen fell through to a weapon fallback.

That regression is structurally closed by two merged stories:
  * 114-2 added `power_glove` to the seaboard world catalog, and
  * 114-11 replaced wholesale-replace with the ADR-145 D3 non-droppable by-id
    merge in `inventory_resolve.resolve_inventory`.

This is a REGRESSION GUARD (it passes today) and the pack-specific wiring test for
the non-droppable merge: it pins that `power_glove` is present in the resolved
seaboard catalog regardless of which tier authors it, so a future edit to either
catalog or to the merge cannot silently resurrect the drop. Driven against the real
pack so it guards production content, not a synthetic fixture.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_PACK_SLUG = "mutant_wasteland"
_WORLD_SLUG = "seaboard_of_saints"


def _load_pack():
    try:
        return load_genre_pack(find_pack_path(_PACK_SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def test_power_glove_present_in_resolved_seaboard_catalog():
    """The resolved (genre baseline ∪ world) catalog for seaboard_of_saints must
    contain `power_glove` — the chargen artifact that the wholesale-replace seam
    used to drop. Non-droppable merge (ADR-145 D3) guarantees it survives."""
    pack = _load_pack()
    resolved = resolve_inventory(pack, _WORLD_SLUG)
    assert resolved is not None, "seaboard_of_saints must resolve an inventory"
    ids = {item.id for item in resolved.item_catalog}
    assert "power_glove" in ids, (
        "power_glove must survive into the resolved seaboard catalog "
        "(ADR-145 D3 non-droppable merge); a world inventory must not drop it"
    )


def test_resolved_seaboard_catalog_keeps_genre_baseline_breadth():
    """Sanity on the non-droppable invariant: shipping a world inventory.yaml must
    not shrink the catalog below the genre baseline. The resolved seaboard catalog
    must be at least as large as the genre-only catalog (world adds/overrides, it
    does not replace-and-drop)."""
    pack = _load_pack()
    assert pack.inventory is not None
    genre_ids = {item.id for item in pack.inventory.item_catalog}

    resolved = resolve_inventory(pack, _WORLD_SLUG)
    assert resolved is not None
    resolved_ids = {item.id for item in resolved.item_catalog}

    missing = genre_ids - resolved_ids
    assert not missing, (
        "no genre baseline id may vanish from the resolved world catalog "
        f"(non-droppable merge); dropped: {sorted(missing)}"
    )
