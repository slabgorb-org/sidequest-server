"""Story 120-3 (RED) — the D3 genre-baseline validator goes verbatim-only.

ADR-145 D3 + epic 120: a Without Number pack's genre-tier item catalog IS the SRD
rulebook, so every genre item must be SRD-sourced — ``mode=verbatim`` (or, where the
mechanics were adapted, ``mode=derived``). 114-14 shipped only the NARROW rule (reject
``mode=bespoke``); it still let an UNPROVENANCED genre item (no ``provenance`` block at
all) load. This story UPGRADES ``_validate_genre_baseline_no_bespoke`` to the real D3
rule:

    For a WN-family pack (awn/cwn/wwn/swn) every genre-tier ``item_catalog`` entry must
    carry ``provenance.mode in {"verbatim", "derived"}``. REJECT both unprovenanced
    (``provenance is None``) AND ``mode=bespoke`` items, fail-loud naming every offender
    (No Silent Fallbacks). NATIVE-ruleset packs stay EXEMPT — their genre inventory is
    authored homebrew with no SRD to be verbatim from.

The content sweep that made this safe to enforce already landed: 120-1 (caverns_and_claudes)
and 120-2 (road_warrior) sourced their previously-unprovenanced genre items verbatim, so
no production WN pack carries unprovenanced/bespoke genre gear today (see
``tests/genre/test_114_14_srd_packs_genre_baseline_no_bespoke.py`` — the real-pack guard).

Driven through the REAL loader (``load_genre_pack``) against tmp copies of the SWN and
native fixture packs, so these are refactor-stable behavior tests AND the wiring test
(the upgraded rule is reachable from the production load path), not direct-call unit
tests of a private function.

RED today (fail until Dev tightens the validator):
  * ``test_wn_family_genre_unprovenanced_item_is_rejected``
  * ``test_wn_family_genre_unprovenanced_among_verbatim_still_rejected``
  * ``test_error_message_names_both_unprovenanced_and_bespoke_offenders``
GREEN-guard (the upgrade must NOT break these — they describe the rule's boundaries):
  * verbatim allowed, derived allowed, bespoke still rejected, native exempt for
    unprovenanced, world-tier unprovenanced allowed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.genre.loader import PackError, load_genre_pack
from tests._helpers.fixture_packs import fixture_pack_path

_SWN_FIXTURE = "swn_test_pack"  # ruleset: swn (WN-family); ships no genre item_catalog today
_NATIVE_FIXTURE = "test_genre"  # ruleset: native (default); lethality genre_key == dir name
_SWN_WORLD = "test_world"


def _base_item(item_id: str) -> dict:
    """A catalog item with the required presentation fields and NO provenance block."""
    return {
        "id": item_id,
        "name": item_id.replace("_", " ").title(),
        "description": "A catalog item used by the 120-3 verbatim-only validator test.",
        "category": "tool",
    }


def _unprovenanced_item(item_id: str) -> dict:
    """A legacy item carrying NO ``provenance`` block at all — the case 114-14 missed."""
    return _base_item(item_id)


def _bespoke_item(item_id: str) -> dict:
    return {**_base_item(item_id), "provenance": {"mode": "bespoke", "license": "na"}}


def _verbatim_item(item_id: str, srd: str = "swn") -> dict:
    return {
        **_base_item(item_id),
        "provenance": {
            "mode": "verbatim",
            "srd": srd,
            "srd_ref": f"{srd.upper()} SRD Equipment — 120-3 test",
            "license": "wn-free",
        },
    }


def _derived_item(item_id: str, srd: str = "swn") -> dict:
    """``mode=derived`` is SRD-adapted gear — legal at the genre tier (the rule is
    verbatim *or* derived, not verbatim-strict)."""
    return {
        **_base_item(item_id),
        "provenance": {
            "mode": "derived",
            "srd": srd,
            "srd_ref": f"{srd.upper()} SRD Equipment — adapted (120-3 test)",
            "license": "wn-free",
        },
    }


def _copy_fixture(slug: str, tmp_path: Path) -> Path:
    dest = tmp_path / slug
    shutil.copytree(fixture_pack_path(slug), dest)
    return dest


def _append_catalog_item(inv_path: Path, item: dict) -> None:
    """Append ``item`` to the ``item_catalog`` of an inventory.yaml (creating the
    file/key as needed), preserving existing content via a YAML round-trip."""
    data = {}
    if inv_path.exists():
        data = yaml.safe_load(inv_path.read_text(encoding="utf-8")) or {}
    data.setdefault("item_catalog", []).append(item)
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# --------------------------------------------------------------------------- RED


def test_wn_family_genre_unprovenanced_item_is_rejected(tmp_path: Path) -> None:
    """RED: a WN-family pack whose genre ``item_catalog`` carries an item with NO
    ``provenance`` block must FAIL to load, loud, naming the offending id. Today the
    114-14 validator rejects only ``mode=bespoke`` and lets the unprovenanced item
    load → ``pytest.raises`` fails (the RED state). Dev raises a ``PackError`` naming
    the id."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _unprovenanced_item("legacy_relic_120_3"))

    with pytest.raises(PackError) as exc_info:
        load_genre_pack(pack_dir)
    message = str(exc_info.value)
    assert "legacy_relic_120_3" in message, (
        "the upgraded D3 validator must fail loud and NAME the unprovenanced genre-tier "
        f"id (No Silent Fallbacks); error was: {message!r}"
    )


def test_wn_family_genre_unprovenanced_among_verbatim_still_rejected(tmp_path: Path) -> None:
    """RED: the rule is per-item, not all-or-nothing. A genre catalog that is mostly
    verbatim but carries ONE unprovenanced item must still fail, naming only the
    unprovenanced offender (the verbatim sibling is legal and must not be blamed)."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _verbatim_item("srd_blade_120_3"))
    _append_catalog_item(pack_dir / "inventory.yaml", _unprovenanced_item("orphan_gizmo_120_3"))

    with pytest.raises(PackError) as exc_info:
        load_genre_pack(pack_dir)
    message = str(exc_info.value)
    assert "orphan_gizmo_120_3" in message, (
        f"must name the unprovenanced offender; error was: {message!r}"
    )
    assert "srd_blade_120_3" not in message, (
        f"the legal verbatim item must NOT be reported as an offender; error was: {message!r}"
    )


def test_error_message_names_both_unprovenanced_and_bespoke_offenders(tmp_path: Path) -> None:
    """RED: when the genre catalog carries BOTH an unprovenanced item and a bespoke
    item, the loud error must name BOTH (No Silent Fallbacks — every offender, not
    just the first class found). Today's message reports only the bespoke id."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _unprovenanced_item("ghost_part_120_3"))
    _append_catalog_item(pack_dir / "inventory.yaml", _bespoke_item("invented_part_120_3"))

    with pytest.raises(PackError) as exc_info:
        load_genre_pack(pack_dir)
    message = str(exc_info.value)
    assert "ghost_part_120_3" in message, f"must name the unprovenanced id; got {message!r}"
    assert "invented_part_120_3" in message, f"must name the bespoke id; got {message!r}"


# ------------------------------------------------------------------------- GUARDS


def test_wn_family_genre_bespoke_still_rejected(tmp_path: Path) -> None:
    """Guard: the 114-14 narrow rule survives the upgrade — a ``mode=bespoke`` genre
    item is still rejected loud, naming the id. The verbatim-only rule SUBSUMES the
    no-bespoke rule; it must not accidentally relax it."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _bespoke_item("contraband_120_3"))

    with pytest.raises(PackError) as exc_info:
        load_genre_pack(pack_dir)
    assert "contraband_120_3" in str(exc_info.value)


def test_wn_family_genre_verbatim_is_allowed(tmp_path: Path) -> None:
    """Guard: an SRD-verbatim genre item is exactly what the rule exists to protect —
    it must load clean."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _verbatim_item("srd_sourced_120_3"))

    pack = load_genre_pack(pack_dir)
    assert pack.inventory is not None
    assert any(i.id == "srd_sourced_120_3" for i in pack.inventory.item_catalog)


def test_wn_family_genre_derived_is_allowed(tmp_path: Path) -> None:
    """Guard: ``mode=derived`` (SRD-adapted) is legal at the genre tier — the rule is
    'verbatim OR derived', NOT verbatim-strict. A too-eager upgrade that rejects
    derived would over-reach."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _derived_item("adapted_kit_120_3"))

    pack = load_genre_pack(pack_dir)
    assert pack.inventory is not None
    item = next((i for i in pack.inventory.item_catalog if i.id == "adapted_kit_120_3"), None)
    assert item is not None, "a mode=derived genre item must survive load"
    assert item.provenance is not None and item.provenance.mode == "derived"


def test_native_pack_genre_unprovenanced_is_exempt(tmp_path: Path) -> None:
    """Boundary guard (CRITICAL): a NATIVE-ruleset pack's genre inventory is authored
    homebrew with no SRD to be verbatim from — its items are legitimately unprovenanced.
    The verbatim-only rule is gated to the WN family and must NOT fire for native packs,
    or it breaks every native / homebrew pack's load (they carry unprovenanced gear by
    design)."""
    pack_dir = _copy_fixture(_NATIVE_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _unprovenanced_item("homebrew_charm_120_3"))

    pack = load_genre_pack(pack_dir)  # must NOT raise — native is exempt
    assert pack.rules.ruleset == "native"
    assert pack.inventory is not None
    assert any(i.id == "homebrew_charm_120_3" for i in pack.inventory.item_catalog)


def test_wn_family_world_tier_unprovenanced_is_allowed(tmp_path: Path) -> None:
    """Guard: the rule governs the GENRE tier only. A WN-family pack whose genre
    baseline is clean (a verbatim item) but whose WORLD ships an unprovenanced item
    must load clean — world inventory is campaign content, not the SRD baseline."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _verbatim_item("srd_sourced_120_3"))
    world_inv = pack_dir / "worlds" / _SWN_WORLD / "inventory.yaml"
    _append_catalog_item(world_inv, _unprovenanced_item("local_trinket_120_3"))

    pack = load_genre_pack(pack_dir)
    world = pack.worlds.get(_SWN_WORLD)
    assert world is not None and world.inventory is not None
    assert any(i.id == "local_trinket_120_3" for i in world.inventory.item_catalog), (
        "a world-tier unprovenanced item is legal and must survive load"
    )
