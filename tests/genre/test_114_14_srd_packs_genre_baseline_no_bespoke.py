"""Story 114-14 — no WN-family pack carries a genre-tier bespoke item.
RETUNED by 120-3 — the genre baseline is now verbatim-only (see below).

ADR-145 D3: bespoke gear is world-tier; a genre-tier ``mode=bespoke`` item is a
hard error for an SRD-bound pack. 114-14 removed ALL declared-bespoke items from
the three offending packs' genre baselines:
  * mutant_wasteland — 12 (6 pre-war relics -> both worlds; 6 survival -> AWN-verbatim)
  * neon_dystopia    — 6  (-> franchise_nations world)
  * road_warrior     — 5  declared-bespoke (-> the_circuit world); its 25
                          UNPROVENANCED items were swept verbatim later by 120-2.

elemental_harmony + heavy_metal are already 100% verbatim (the target pattern) and
are asserted here as guards. Driven against the REAL packs so green proves the
production content complies, not a fixture.

120-3 retune: the D3 validator upgraded from 'no genre-tier bespoke' to the full
ADR-145 D3 rule — 'WN-family genre baseline must be mode:verbatim (or derived)'.
``test_wn_family_pack_genre_baseline_is_verbatim_only`` below asserts that stricter
rule against EVERY WN-family pack (awn/cwn/wwn/swn): zero unprovenanced AND zero
bespoke genre items. It is GREEN now (120-1/120-2 finished the sweep) and is the
real-content regression guard that the tightened validator never breaks a
production load. The no-bespoke tests above remain valid as the subset they cover.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

# Packs this story makes genre-clean (had declared bespoke).
_FIXED_PACKS = ["mutant_wasteland", "neon_dystopia", "road_warrior"]
# WN-family packs already clean — guard that they stay clean.
_ALREADY_CLEAN = ["elemental_harmony", "heavy_metal"]
# All live WN-family (awn/cwn/wwn/swn) packs — 120-3 verbatim-only regression guard.
# A WN pack added later is enforced at LOAD time by the validator regardless; this
# parametrize is the canary that flags a regression in shipping content.
_WN_FAMILY_PACKS = [
    "caverns_and_claudes",  # wwn
    "elemental_harmony",  # wwn
    "heavy_metal",  # wwn
    "mutant_wasteland",  # awn
    "neon_dystopia",  # cwn
    "road_warrior",  # cwn
    "space_opera",  # swn
]


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


def _genre_non_verbatim_offenders(pack) -> list[str]:
    """120-3: every genre item must be ``mode in {verbatim, derived}``. Returns a
    labelled offender list for each item that is unprovenanced or carries any other
    mode (today: bespoke), so a failure message names exactly what regressed."""
    assert pack.inventory is not None, "pack must ship a genre-tier inventory"
    offenders: list[str] = []
    for item in pack.inventory.item_catalog:
        prov = item.provenance
        if prov is None:
            offenders.append(f"{item.id}: unprovenanced")
        elif prov.mode not in ("verbatim", "derived"):
            offenders.append(f"{item.id}: mode={prov.mode!r}")
    return offenders


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


@pytest.mark.parametrize("slug", _WN_FAMILY_PACKS)
def test_wn_family_pack_genre_baseline_is_verbatim_only(slug: str) -> None:
    """120-3 (retune): the upgraded D3 rule — every WN-family pack's genre item_catalog
    must be ``mode in {verbatim, derived}``: zero unprovenanced, zero bespoke. GREEN now
    (120-1 caverns + 120-2 road_warrior finished the unprovenanced sweep), and the
    real-content regression guard that the tightened ``_validate_genre_baseline_no_bespoke``
    never breaks a production WN pack's load. Driven through the REAL loader — if the
    pack carried an offender the loader would already PackError, so this also proves the
    pack loads at all."""
    pack = _load(slug)
    offenders = _genre_non_verbatim_offenders(pack)
    assert not offenders, (
        f"{slug} genre baseline must be verbatim/derived only (ADR-145 D3, 120-3): "
        f"every genre item is SRD-sourced or moved to the world tier. "
        f"{len(offenders)} offender(s): {offenders}"
    )


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
