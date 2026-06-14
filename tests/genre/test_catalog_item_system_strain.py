"""RED-phase tests for the cyberware ``system_strain`` schema delta (story 114-5).

ADR-145 D4 names ``system_strain`` as the Without-Number item-schema category
extra "for cyberware". This story makes it a **typed, validated** field on
``CatalogItem`` — replacing the status quo the 2026-06-14 inventory audit found,
where a cyberware item's System Strain cost lived only in a free-form ``lore``
string ("Costs System Strain. Worth it when the bullet stops.") and the item
carried nothing but a cosmetic ``cyberware`` tag.

Contract:
* ``CatalogItem`` grows ``system_strain: float | None = None``.
* ``None``-defaulted, so every existing non-cyberware item validates unchanged.
* It is a real ``float``, not free-form prose — ``"permanent"`` is rejected.
* It holds FRACTIONAL strain verbatim — CWN prices common chrome at 0.25/0.5, so
  ``0.25`` must round-trip (an int would truncate it to a "free" implant; Keith's
  ruling, 2026-06-14).
* A strain cost is non-negative — a negative value is rejected (``ge=0``).
* The strict ``extra="forbid"`` config is NOT relaxed by the delta.

These tests are RED until 114-5 adds the field: today ``CatalogItem`` is
``extra="forbid"`` with no ``system_strain``, so constructing one raises
``ValidationError`` (the intended red signal).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.inventory import CatalogItem, ItemProvenance


def _cyberware(**overrides: object) -> CatalogItem:
    base: dict[str, object] = dict(
        id="cwn_wired_reflexes",
        name="Wired Reflexes",
        description="Reflex-boosting cyberware.",
        category="cyberware",
        system_strain=2,
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


def test_catalog_item_accepts_typed_system_strain() -> None:
    """system_strain is a first-class typed float on CatalogItem (ADR-145 D4)."""
    item = _cyberware()
    assert item.system_strain == 2
    assert isinstance(item.system_strain, float)


def test_catalog_item_holds_fractional_system_strain_verbatim() -> None:
    """CWN prices its most common cyberware at fractional strain (Cybereyes = 0.25);
    the field must hold it verbatim, not truncate to 0 (Keith's float ruling)."""
    item = _cyberware(system_strain=0.25)
    assert item.system_strain == 0.25
    assert isinstance(item.system_strain, float)


def test_system_strain_defaults_none_for_non_cyberware() -> None:
    """Existing non-cyberware items validate unchanged: system_strain defaults to None."""
    sword = CatalogItem(
        id="cwn_mono_katana",
        name="Mono Katana",
        description="A vibro-edged blade.",
        category="melee_weapon",
    )
    assert sword.system_strain is None


def test_system_strain_rejects_free_form_prose() -> None:
    """The defect this story closes: system_strain must be a real int, NOT prose.
    A string like 'permanent' (the kind of value cyberware cost used to hide in)
    must be rejected, not silently accepted."""
    with pytest.raises(ValidationError):
        _cyberware(system_strain="permanent")


def test_system_strain_rejects_negative() -> None:
    """A System Strain cost is a non-negative count — negative is a content error
    and must fail loud (ge=0), never silently install 'free' cyberware."""
    with pytest.raises(ValidationError):
        _cyberware(system_strain=-1)


def test_system_strain_delta_keeps_extra_forbid() -> None:
    """The schema delta must NOT relax CatalogItem's strict extra='forbid'."""
    with pytest.raises(ValidationError):
        CatalogItem(
            id="x",
            name="x",
            description="x",
            category="cyberware",
            not_a_real_field=1,  # type: ignore[call-arg]
        )
