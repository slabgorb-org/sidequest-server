"""Story 106-1 (RED) — content half: caverns_and_claudes armor declares WWN-SRD AC.

The server can only DERIVE ``core.armor_class`` from content if the content carries
the value. This data-contract test loads the REAL pack through the production loader
and asserts the genre-tier light-armor entry declares the WWN-SRD leather value (13).

Story 120-1 (ADR-145): the caverns genre baseline is now 100% WWN-SRD-verbatim, so
the legacy bare ``leather_armor`` id was sourced verbatim as ``wwn_linothorax`` — the
WWN AC-13 light armor that fills the old leather slot. The assertion follows the rename;
the data contract (armor declares its WWN AC so chargen can derive it) is unchanged.

Per the standing ruling (2026-06-13, ``.pennyfarthing/sidecars/gm-decisions.md``):
WWN-bound mechanical values come from the Worlds Without Number SRD — leather = AC 13.

Skips gracefully when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import PackNotFound, find_pack_path

WWN_LEATHER_AC = 13
# Story 120-1 verbatim sweep: legacy ``leather_armor`` (AC 13) → WWN ``wwn_linothorax``
# (the SRD's AC-13 light armor). Slug stability is id stability; the slot is the same.
WWN_LEATHER_ID = "wwn_linothorax"


def _load_caverns_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        return None
    return load_genre_pack(path)


def _catalog_item(pack, item_id: str):
    assert pack.inventory is not None, "caverns_and_claudes must declare a genre-tier inventory"
    return next((c for c in pack.inventory.item_catalog if c.id == item_id), None)


def test_leather_armor_declares_wwn_srd_armor_class():
    """AC2 (content): the WWN AC-13 light armor (wwn_linothorax, the verbatim
    successor to the legacy leather_armor slot) declares armor_class == 13 (WWN
    SRD), sourced from content so the engine derivation has a value to read."""
    pack = _load_caverns_pack()
    if pack is None:
        pytest.skip("caverns_and_claudes pack not on disk")

    leather = _catalog_item(pack, WWN_LEATHER_ID)
    assert leather is not None, (
        f"{WWN_LEATHER_ID} (WWN AC-13 light armor) must exist in the caverns inventory catalog"
    )
    assert leather.armor_class == WWN_LEATHER_AC, (
        f"{WWN_LEATHER_ID} must declare armor_class: 13 (WWN SRD) so chargen can derive "
        "core.armor_class from content rather than a hardcoded engine constant"
    )
