"""Story 114-14 (RED) — the D3 genre-baseline "no bespoke" loader validator.

ADR-145 D3: bespoke gear is a WORLD-tier privilege; a genre-tier baseline item with
``provenance.mode == "bespoke"`` is a hard error for a pack that BINDS an SRD
ruleset (the genre tier IS the SRD rulebook). This story adds a fail-loud loader
check — no allow-list, no warning-and-continue (No Silent Fallbacks, claude.md).

Scope of THIS validator (narrowed per Keith 2026-06-15): it rejects only *declared*
bespoke (``mode == "bespoke"``). It does NOT require provenance *presence* and does
NOT require *verbatim-only* — that stricter rule (which would also reject
unprovenanced genre items in caverns_and_claudes / road_warrior) is **epic 120**.

Gate: the rule applies to the Without Number family (``awn``/``cwn``/``wwn``/
``swn``). NATIVE-ruleset packs are EXEMPT — their genre inventory is authored
content with no SRD to be verbatim from, so genre-tier bespoke there is legitimate
homebrew (this protects native-pack / homebrew authoring).

Driven through the REAL loader (``load_genre_pack``) against tmp copies of the SWN
and native fixture packs, so these are refactor-stable behavior tests, not
source-grep wiring tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from tests._helpers.fixture_packs import fixture_pack_path

_SWN_FIXTURE = "swn_test_pack"  # ruleset: swn (WN-family) — no genre inventory today
# ``test_genre`` ships no ``ruleset:`` line → loads as the default ``native`` ruleset.
# Its lethality_policy ``genre_key`` is "test_genre", so it must be copied to a dir
# of that exact name (the genre_key/dir-name guard, lethality_policy_loader.py:47).
_NATIVE_FIXTURE = "test_genre"
_SWN_WORLD = "test_world"


def _bespoke_item(item_id: str) -> dict:
    return {
        "id": item_id,
        "name": item_id.replace("_", " ").title(),
        "description": "A bespoke item used by the 114-14 D3 validator test.",
        "category": "tool",
        "provenance": {"mode": "bespoke"},
    }


def _verbatim_item(item_id: str, srd: str = "swn") -> dict:
    return {
        "id": item_id,
        "name": item_id.replace("_", " ").title(),
        "description": "An SRD-verbatim item used by the 114-14 D3 validator test.",
        "category": "tool",
        "provenance": {
            "mode": "verbatim",
            "srd": srd,
            "srd_ref": f"{srd.upper()} SRD Equipment — 114-14 test",
            "license": "wn-free",
        },
    }


def _copy_fixture(slug: str, tmp_path: Path) -> Path:
    dest = tmp_path / slug
    shutil.copytree(fixture_pack_path(slug), dest)
    return dest


def _append_catalog_item(inv_path: Path, item: dict) -> None:
    """Append ``item`` to the ``item_catalog`` of an inventory.yaml (creating the
    file/key as needed), preserving any existing content via a YAML round-trip."""
    data = {}
    if inv_path.exists():
        data = yaml.safe_load(inv_path.read_text(encoding="utf-8")) or {}
    data.setdefault("item_catalog", []).append(item)
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_wn_family_genre_tier_bespoke_is_rejected(tmp_path: Path) -> None:
    """RED: a WN-family pack whose genre ``item_catalog`` carries a ``mode=bespoke``
    item must FAIL to load, loud, naming the offending id. Today no validator exists
    → the pack loads and ``pytest.raises`` fails (the RED state). Dev raises a
    ``PackError`` (or equivalent loader error) that names the id."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _bespoke_item("contraband_relic_114_14"))

    with pytest.raises(Exception) as exc_info:
        load_genre_pack(pack_dir)
    message = str(exc_info.value)
    assert "contraband_relic_114_14" in message, (
        "the D3 validator must fail loud and NAME the offending genre-tier bespoke "
        f"id (No Silent Fallbacks); error was: {message!r}"
    )


def test_wn_family_genre_tier_verbatim_is_allowed(tmp_path: Path) -> None:
    """Guard: an SRD-verbatim genre-tier item is legal. The validator must reject
    bespoke, not over-reach and reject the SRD baseline gear it exists to protect."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _verbatim_item("srd_sourced_114_14"))

    pack = load_genre_pack(pack_dir)
    assert pack.inventory is not None
    assert any(i.id == "srd_sourced_114_14" for i in pack.inventory.item_catalog)


def test_wn_family_world_tier_bespoke_is_allowed(tmp_path: Path) -> None:
    """Guard: bespoke is a WORLD-tier privilege (ADR-145 D3). A WN-family pack whose
    genre baseline is clean (a verbatim item) but whose WORLD ships a bespoke item
    must load clean — the validator only governs the genre tier."""
    pack_dir = _copy_fixture(_SWN_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _verbatim_item("srd_sourced_114_14"))
    world_inv = pack_dir / "worlds" / _SWN_WORLD / "inventory.yaml"
    _append_catalog_item(world_inv, _bespoke_item("street_relic_114_14"))

    pack = load_genre_pack(pack_dir)
    world = pack.worlds.get(_SWN_WORLD)
    assert world is not None and world.inventory is not None
    assert any(i.id == "street_relic_114_14" for i in world.inventory.item_catalog), (
        "a world-tier bespoke item is legal and must survive load"
    )


def test_native_pack_genre_tier_bespoke_is_exempt(tmp_path: Path) -> None:
    """Boundary guard: a NATIVE-ruleset pack's genre inventory is authored content
    (no SRD to be verbatim from); genre-tier bespoke there is legitimate homebrew.
    The validator is gated to the WN family and must NOT fire for native packs —
    otherwise it breaks native-pack / homebrew authoring."""
    pack_dir = _copy_fixture(_NATIVE_FIXTURE, tmp_path)
    _append_catalog_item(pack_dir / "inventory.yaml", _bespoke_item("homebrew_trinket_114_14"))

    pack = load_genre_pack(pack_dir)  # must NOT raise — native is exempt
    assert pack.rules.ruleset == "native"
    assert pack.inventory is not None
    assert any(i.id == "homebrew_trinket_114_14" for i in pack.inventory.item_catalog)
