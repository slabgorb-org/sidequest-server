"""Story 114-14 (RED) — no WN-family pack carries a genre-tier bespoke item.

ADR-145 D3: bespoke gear is world-tier; a genre-tier ``mode=bespoke`` item is a
hard error for an SRD-bound pack. This story removes ALL declared-bespoke items
from the three offending packs' genre baselines:
  * mutant_wasteland — 12 (6 pre-war relics -> both worlds; 6 survival -> AWN-verbatim)
  * neon_dystopia    — 6  (-> franchise_nations world)
  * road_warrior     — 5  declared-bespoke (-> the_circuit world); its 25
                          UNPROVENANCED items stay (epic 120, verbatim-only sweep)

elemental_harmony + heavy_metal are already 100% verbatim (the target pattern) and
are asserted here as guards. Driven against the REAL packs so green proves the
production content complies, not a fixture.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

# Packs this story makes genre-clean (had declared bespoke).
_FIXED_PACKS = ["mutant_wasteland", "neon_dystopia", "road_warrior"]
# WN-family packs already clean — guard that they stay clean.
_ALREADY_CLEAN = ["elemental_harmony", "heavy_metal"]


def _load(slug: str):
    try:
        return load_genre_pack(find_pack_path(slug))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def _genre_bespoke_ids(pack) -> list[str]:
    assert pack.inventory is not None, "pack must ship a genre-tier inventory"
    return [
        item.id
        for item in pack.inventory.item_catalog
        if item.provenance is not None and item.provenance.mode == "bespoke"
    ]


@pytest.mark.parametrize("slug", _FIXED_PACKS)
def test_fixed_pack_has_no_genre_tier_bespoke(slug: str) -> None:
    """RED: today mutant_wasteland(12)/neon_dystopia(6)/road_warrior(5) each carry
    declared-bespoke genre items. After migration the genre baseline must carry
    ZERO ``mode=bespoke`` items (ADR-145 D3 — bespoke is world-tier)."""
    pack = _load(slug)
    bespoke = _genre_bespoke_ids(pack)
    assert not bespoke, (
        f"{slug} genre baseline must carry NO mode=bespoke items "
        f"(ADR-145 D3); relocate to the world tier or SRD-source verbatim. Found: {bespoke}"
    )


@pytest.mark.parametrize("slug", _ALREADY_CLEAN)
def test_already_clean_pack_stays_clean(slug: str) -> None:
    """Guard: the WWN packs that already model a 100%-verbatim genre baseline must
    not regress — the new validator must not require any change here."""
    pack = _load(slug)
    bespoke = _genre_bespoke_ids(pack)
    assert not bespoke, f"{slug} was already clean and must stay clean; found: {bespoke}"


def test_mutant_wasteland_survival_gear_is_awn_verbatim_at_genre() -> None:
    """RED: the 6 mutant_wasteland survival/tool items move OFF bespoke. The chosen
    resolution (handoff) is to AWN-source them verbatim AT the genre tier (avoids
    duplicating universal gear across the pack's two worlds). Any that remain at
    genre must be ``mode=verbatim, srd=awn, license=wn-free`` with an srd_ref; any
    with no AWN analog must instead be ABSENT from genre (moved to the worlds).
    Either way: none may be ``mode=bespoke`` at genre (covered above), and a survival
    id that stays at genre must be AWN-verbatim."""
    pack = _load("mutant_wasteland")
    assert pack.inventory is not None
    survival_ids = {
        "water_canteen",
        "rad_pills",
        "medkit_crude",
        "glow_rod",
        "tool_kit_basic",
        "geiger_clicker",
    }
    by_id = {i.id: i for i in pack.inventory.item_catalog}
    for sid in survival_ids:
        item = by_id.get(sid)
        if item is None:
            continue  # legitimately moved to the world tier (no clean AWN analog)
        prov = item.provenance
        assert prov is not None and prov.mode == "verbatim", (
            f"{sid} kept at genre must be AWN-verbatim (not bespoke/unprovenanced); "
            f"got {None if prov is None else prov.mode!r}"
        )
        assert prov.srd == "awn", f"{sid}: genre survival gear must be srd=awn"
        assert prov.license == "wn-free", f"{sid}: AWN verbatim license must be wn-free"
        assert prov.srd_ref and "awn" in prov.srd_ref.lower(), (
            f"{sid}: srd_ref must cite the AWN SRD, got {prov.srd_ref!r}"
        )
