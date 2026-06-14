"""RED-phase wiring tests for CWN cyberware ``system_strain`` in the inventory
resolve/merge production path (story 114-5).

Two contracts, both flowing through the REAL production code in
``sidequest.server.dispatch.inventory_resolve`` (no source-text grepping — this is
the codebase's blessed fixture-driven-behavior wiring shape, CLAUDE.md "No
Source-Text Wiring Tests"):

1. **system_strain is a LOCKED mechanical field (ADR-145 D3/D4).** A cyberware
   item's strain cost is part of the SRD's balanced envelope, so a world MUST NOT
   re-stat it on a ``mode=verbatim`` baseline item. The merge must raise
   ``VerbatimFieldLockError`` — which requires Dev to add ``system_strain`` to both
   ``_MECHANICAL_FIELDS`` and ``_FIELD_DEFAULTS`` in ``inventory_resolve``. A
   presentation-only reskin (name change, strain untouched) must still succeed and
   inherit the baseline strain.

2. **The CWN baseline cyberware actually LOADS through ``resolve_inventory``** and
   the resolution fires its ``state_transition`` OTEL span (the GM-panel lie
   detector) — proving a neon_dystopia / road_warrior session reads CWN cyberware
   from the resolver, not from improvisation.

RED today: constructing a ``CatalogItem`` with ``system_strain`` raises
``ValidationError`` (the field does not exist yet); once it exists, the lock test
stays RED until ``system_strain`` joins ``_MECHANICAL_FIELDS``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from sidequest.genre.models.inventory import (
    CatalogItem,
    InventoryConfig,
    ItemProvenance,
)
from sidequest.genre.models.pack import GenrePack, World
from sidequest.server.dispatch.inventory_resolve import (
    VerbatimFieldLockError,
    merge_inventory_catalog,
    resolve_inventory,
)


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    from sidequest.telemetry import watcher_hub

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    yield captured


def _cwn_cyberware(
    item_id: str = "cwn_wired_reflexes", *, strain: int = 2, **overrides
) -> CatalogItem:
    """A verbatim CWN cyberware baseline item carrying a typed system_strain."""
    base: dict[str, object] = dict(
        id=item_id,
        name="Wired Reflexes",
        description="Reflex-boosting cyberware.",
        category="cyberware",
        system_strain=strain,
        tech_level=4,
        value=5000,
        provenance=ItemProvenance(
            mode="verbatim",
            srd="cwn",
            srd_ref="CWN SRD §3.0.5 Cyberware",
            license="wn-free",
            extracted_by="cwn_equip_extract@114-5",
        ),
    )
    base.update(overrides)
    return CatalogItem(**base)  # type: ignore[arg-type]


def _make_pack(
    *,
    genre_inventory: InventoryConfig | None,
    worlds: dict[str, InventoryConfig | None],
) -> GenrePack:
    world_objs: dict[str, World] = {}
    for slug, inv in worlds.items():
        world_objs[slug] = cast(World, World.model_construct(inventory=inv))
    return cast(
        GenrePack,
        GenrePack.model_construct(inventory=genre_inventory, worlds=world_objs),
    )


# ---------------------------------------------------------------------------
# system_strain is a locked mechanical field (ADR-145 D3/D4)
# ---------------------------------------------------------------------------


def test_world_may_not_restat_verbatim_cyberware_strain() -> None:
    """A world override that changes a verbatim cyberware's system_strain is the
    ADR-143 forbidden re-tune — the merge must fail loud (VerbatimFieldLockError),
    not silently accept the world's cheaper strain cost."""
    baseline = _cwn_cyberware(strain=2)
    # World keeps the same id but tries to halve the strain cost.
    world_override = _cwn_cyberware(strain=1, provenance=None)
    with pytest.raises(VerbatimFieldLockError):
        merge_inventory_catalog([baseline], [world_override])


def test_world_may_reskin_cyberware_name_keeping_strain() -> None:
    """Presentation reskin is allowed: a world may rename the cyberware while the
    baseline's system_strain is inherited and preserved (ADR-145 D1/D3)."""
    baseline = _cwn_cyberware(strain=2)
    # World authors ONLY a new name; strain left at the model default (not authored).
    world_reskin = CatalogItem(
        id="cwn_wired_reflexes",
        name="Sandevistan",  # world flavor
        description="Reflex-boosting cyberware.",
        category="cyberware",
    )
    merged, _counts = merge_inventory_catalog([baseline], [world_reskin])
    item = next(it for it in merged if it.id == "cwn_wired_reflexes")
    assert item.name == "Sandevistan"  # presentation overridden
    assert item.system_strain == 2  # mechanics inherited from baseline, not dropped
    assert item.provenance is not None and item.provenance.mode == "verbatim"


# ---------------------------------------------------------------------------
# Wiring: the CWN baseline cyberware loads through resolve_inventory + OTEL
# ---------------------------------------------------------------------------


def test_resolve_inventory_loads_cwn_cyberware_baseline(
    captured_watcher_events: list[dict[str, Any]],
) -> None:
    """A genre-tier CWN baseline cyberware item resolves through the production
    resolve_inventory seam carrying its typed system_strain + verbatim provenance,
    and the resolution fires its state_transition OTEL span (wiring proof)."""
    genre_inv = InventoryConfig(item_catalog=[_cwn_cyberware(strain=2)])
    pack = _make_pack(genre_inventory=genre_inv, worlds={"franchise_nations": None})

    resolved = resolve_inventory(pack, "franchise_nations")
    assert resolved is not None
    cyber = next(it for it in resolved.item_catalog if it.id == "cwn_wired_reflexes")
    assert cyber.system_strain == 2
    assert cyber.provenance is not None
    assert cyber.provenance.srd == "cwn"
    assert cyber.provenance.mode == "verbatim"

    # OTEL: the resolver fired a state_transition span (the GM-panel lie detector
    # proving the catalog was loaded by the resolver, not improvised).
    spans = [e for e in captured_watcher_events if e["event_type"] == "state_transition"]
    assert spans, "resolve_inventory must emit a state_transition span"
    fields = spans[-1]["fields"]
    assert fields["catalog_count"] >= 1
    assert fields["has_config"] is True
