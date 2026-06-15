"""RED-phase tests for story 114-12: complete the CWN/WN inventory verbatim
round-trip + harden the WN extraction CLIs.

Contract under test (ADR-145 D2/D4; builds on 114-3/114-5):

1. **DRY shared WN-extraction core.** The two CLIs share their genuinely-identical
   section parsers. `general` and `ranged` use the SAME column layout in both WWN and
   CWN, so an identical row must extract to an identical `CatalogItem` through either
   tool (behavioral parity). (`armor` and `melee` legitimately DIVERGE — CWN armor adds
   a Soak column and CWN melee adds a Trauma column — so they are NOT parity-tested.)
2. **Dual AC / soak.** CWN armor carries BOTH `armor_class` (ascending AC) and
   `mitigation` (SWN/CWN soak) verbatim. WWN armor is single-AC and leaves
   `mitigation=None`.
3. **Trauma-Target.** CWN melee populates `trauma_die` / `trauma_rating` /
   `trauma_target` verbatim; an N/A Trauma cell emits NO trauma fields (no invented
   numbers — mirrors the `_NA_CELLS` discipline for Shock/magazine). WWN melee has no
   Trauma column and leaves the trauma fields at their defaults.
4. **Ranged +N.** A ranged Damage cell of `NdM+B` parses into `damage.dice == "NdM"`
   and `damage.bonus == B` (the `DamageSpec.dice` validator rejects a `+B` suffix, so
   the parser must split before constructing the spec). A plain `NdM` yields `bonus == 0`.
   A malformed bonus fails loud with row context (No Silent Fallbacks).
5. **Verbatim round-trip.** An item carrying the new fields survives both a
   `model_dump()` → `CatalogItem(**dump)` reconstruction AND a CLI-stdout JSON round-trip
   with every new field intact (mitigation / trauma / bonus not dropped or truncated).
6. **CLI hardening.** `--srd` and `--license` are `argparse` `choices`-constrained
   (an unknown value exits, never silently passes); `main()` resolves `--srd-path` via
   `Path.resolve()` so the fail-loud "does not exist" message names the resolved
   absolute path.

Test-design note (deviation logged in the session): the new-schema rows are exercised
via INLINE fixture text (the codebase's idiom for parser edge cases — cf.
`test_none_shock_melee_weapon_emits_without_inventing_shock`), NOT by mutating the
shared `tests/fixtures/{wwn,cwn}_srd/equipment_chapter.txt` files. That keeps the
pre-existing 114-3/114-5 count/entrypoint tests green during RED and keeps every
failure here attributable to one new capability. The synthetic CWN column layouts are:

    Armor  : Name  AC  Soak  Cost  Enc                    (Soak added; '-'/None ⇒ no soak)
    Melee  : Name  Damage  Shock  Trauma  Enc  Cost  Attr (Trauma added)
    Ranged : Name  Damage  Range  Mag  Enc  Cost          (Damage may be NdM or NdM+B)

The synthetic Trauma cell encodes all three fields compactly as
``<die>/x<rating>/T<target>`` (e.g. ``1d10/x2/T6``), or ``None`` for no-trauma. Tests
assert only the RESULT (`trauma_die`/`trauma_rating`/`trauma_target`), never the parse
mechanics — Dev picks the parser shape.

These are RED until 114-12 lands the dual-AC/soak + Trauma + ranged-bonus extraction
and the argparse hardening.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sidequest.cli.cwn_equip_extract.cwn_equip_extract import (
    build_parser as cwn_build_parser,
)
from sidequest.cli.cwn_equip_extract.cwn_equip_extract import (
    extract_catalog as cwn_extract,
)
from sidequest.cli.cwn_equip_extract.cwn_equip_extract import (
    main as cwn_main,
)
from sidequest.cli.wwn_equip_extract.wwn_equip_extract import (
    build_parser as wwn_build_parser,
)
from sidequest.cli.wwn_equip_extract.wwn_equip_extract import (
    extract_catalog as wwn_extract,
)
from sidequest.genre.models.inventory import CatalogItem

# ---------------------------------------------------------------------------
# Synthetic full-section text builders (every section header must be present, or
# extract_catalog fails loud — so each builder emits the complete section set).
# Defaults are NEW-schema rows that the GREEN parser must handle.
# ---------------------------------------------------------------------------


def _wwn_text(
    *,
    armor: str = "Test Padded Vest    13    20      1",
    melee: str = "Test Iron Cudgel    1d8      2/AC15     1     12     Str",
    ranged: str = "Test Sling Carbine  1d6      pistol     6     1     40",
    general: str = "Test Hemp Rope 50ft 4       1",
) -> str:
    return (
        "§3.0.0 Equipment\n\n"
        "§3.0.1 Armor\n"
        "Name                AC    Cost    Enc\n"
        f"{armor}\n\n"
        "§3.0.2 Melee Weapons\n"
        "Name                Damage   Shock      Enc   Cost   Attribute\n"
        f"{melee}\n\n"
        "§3.0.3 Ranged Weapons\n"
        "Name                Damage   Range      Mag   Enc   Cost\n"
        f"{ranged}\n\n"
        "§3.0.4 General Equipment\n"
        "Name                Cost    Enc\n"
        f"{general}\n"
    )


def _cwn_text(
    *,
    armor: str = "Test Armor Weave    14    2    300     1",
    melee: str = "Test Mono Katana    1d8      2/AC15    1d10/x2/T6   1     500    Dex",
    ranged: str = "Test Mag Pistol     1d6      pistol     9     1     250",
    general: str = "Test Med Patch      25      0",
    cyberware: str = (
        "Test Cybereyes       10000   Sensory     Sight          0.25            "
        "Flash-protected synthetic eyes"
    ),
) -> str:
    return (
        "§3.0.0 Equipment\n\n"
        "§3.0.1 Armor\n"
        "Name                AC    Soak   Cost    Enc\n"
        f"{armor}\n\n"
        "§3.0.2 Melee Weapons\n"
        "Name                Damage   Shock     Trauma       Enc   Cost   Attribute\n"
        f"{melee}\n\n"
        "§3.0.3 Ranged Weapons\n"
        "Name                Damage   Range      Mag   Enc   Cost\n"
        f"{ranged}\n\n"
        "§3.0.4 General Equipment\n"
        "Name                Cost    Enc\n"
        f"{general}\n\n"
        "§3.0.5 Cyberware\n"
        "Name                 Cost    Location    Concealment    System Strain   Effect\n"
        f"{cyberware}\n"
    )


def _by_name(items: list[CatalogItem]) -> dict[str, CatalogItem]:
    return {it.name: it for it in items}


# ===========================================================================
# AC1 — DRY shared core: behavioral parity on the genuinely-identical sections
# ===========================================================================


def test_general_section_parity_across_both_clis() -> None:
    """An identical General-Equipment row extracts to an identical CatalogItem
    (modulo the slug-derived id/srd/srd_ref) through BOTH CLIs — proving the
    general parser is a single shared implementation, not two drifting copies."""
    row = "Shared Field Ration 7       1"
    wwn_item = _by_name(wwn_extract(_wwn_text(general=row), srd="wwn"))["Shared Field Ration"]
    cwn_item = _by_name(cwn_extract(_cwn_text(general=row), srd="cwn"))["Shared Field Ration"]
    assert wwn_item.category == cwn_item.category == "general"
    assert wwn_item.value == cwn_item.value == 7
    assert wwn_item.weight == cwn_item.weight == 1.0
    assert wwn_item.damage is None and cwn_item.damage is None
    assert wwn_item.armor_class is None and cwn_item.armor_class is None


def test_ranged_section_parity_across_both_clis() -> None:
    """An identical plain-damage Ranged row extracts identically through both CLIs
    (shared ranged parser). Range band, magazine, dice, and a zero bonus all match."""
    row = "Shared Carbine      1d6      pistol     6     1     40"
    wwn_item = _by_name(wwn_extract(_wwn_text(ranged=row), srd="wwn"))["Shared Carbine"]
    cwn_item = _by_name(cwn_extract(_cwn_text(ranged=row), srd="cwn"))["Shared Carbine"]
    assert wwn_item.category == cwn_item.category == "ranged_weapon"
    assert wwn_item.range_band == cwn_item.range_band == "pistol"
    assert wwn_item.magazine == cwn_item.magazine == 6
    assert wwn_item.value == cwn_item.value == 40
    assert wwn_item.damage is not None and cwn_item.damage is not None
    assert wwn_item.damage.dice == cwn_item.damage.dice == "1d6"
    assert wwn_item.damage.bonus == cwn_item.damage.bonus == 0


# ===========================================================================
# AC2 — Dual AC / soak
# ===========================================================================


def test_cwn_armor_populates_both_armor_class_and_mitigation() -> None:
    """CWN armor is dual-stat: the row's AC populates armor_class AND its Soak column
    populates mitigation, both verbatim. (Currently mitigation is never set → RED.)"""
    items = _by_name(cwn_extract(_cwn_text(armor="Heavy Plate Rig     17    5    4000    2"), srd="cwn"))
    plate = items["Heavy Plate Rig"]
    assert plate.category == "armor"
    assert plate.armor_class == 17  # ascending AC verbatim
    assert plate.mitigation == 5  # soak verbatim — the new field
    assert plate.value == 4000
    assert plate.weight == 2.0


def test_cwn_armor_na_soak_cell_leaves_mitigation_none() -> None:
    """A '-' Soak cell is a verbatim 'no soak' — mitigation must be None, never an
    invented 0 and never a dropped row (mirrors the _NA_CELLS Shock/magazine rule)."""
    items = _by_name(cwn_extract(_cwn_text(armor="Light Mesh Vest     13    -    150     1"), srd="cwn"))
    vest = items["Light Mesh Vest"]
    assert vest.armor_class == 13
    assert vest.mitigation is None
    assert vest.value == 150


def test_wwn_armor_is_single_ac_and_leaves_mitigation_none() -> None:
    """Regression guard: WWN armor is single-AC (no Soak column). It must keep
    armor_class set and mitigation None — the dual-AC change must not bleed into WWN."""
    items = _by_name(wwn_extract(_wwn_text(armor="Test War Plate      16    250     3"), srd="wwn"))
    plate = items["Test War Plate"]
    assert plate.armor_class == 16
    assert plate.mitigation is None


# ===========================================================================
# AC3 — Trauma-Target on CWN melee
# ===========================================================================


def test_cwn_melee_populates_trauma_die_rating_and_target() -> None:
    """A CWN melee Trauma cell '1d10/x2/T6' populates trauma_die='1d10',
    trauma_rating=2, trauma_target=6 verbatim. (Currently no parser reads Trauma → RED.)"""
    items = _by_name(
        cwn_extract(
            _cwn_text(melee="War Cleaver        1d8      2/AC15    1d10/x2/T6   1     500    Str"),
            srd="cwn",
        )
    )
    cleaver = items["War Cleaver"]
    assert cleaver.damage is not None
    assert cleaver.damage.dice == "1d8"
    assert cleaver.damage.trauma_die == "1d10"
    assert cleaver.damage.trauma_rating == 2
    assert cleaver.damage.trauma_target == 6
    # Shock still parses verbatim alongside the new Trauma column.
    assert cleaver.damage.shock == 2
    assert cleaver.damage.shock_ac == 15


def test_cwn_melee_na_trauma_emits_no_trauma_fields() -> None:
    """A 'None' Trauma cell is a verbatim 'no trauma' — trauma_die stays None and
    trauma_rating stays at the model default (1), never an invented value."""
    items = _by_name(
        cwn_extract(
            _cwn_text(melee="Stun Baton         1d6      1/AC13    None         1     100    Str"),
            srd="cwn",
        )
    )
    baton = items["Stun Baton"]
    assert baton.damage is not None
    assert baton.damage.trauma_die is None
    assert baton.damage.trauma_rating == 1  # default, not invented
    assert baton.damage.trauma_target is None


def test_wwn_melee_has_no_trauma_fields() -> None:
    """Regression guard: WWN melee has no Trauma column; its items must leave the
    trauma fields at defaults (the CWN Trauma change must not bleed into WWN)."""
    items = _by_name(wwn_extract(_wwn_text(melee="Test Iron Cudgel    1d8      2/AC15     1     12     Str"), srd="wwn"))
    cudgel = items["Test Iron Cudgel"]
    assert cudgel.damage is not None
    assert cudgel.damage.trauma_die is None
    assert cudgel.damage.trauma_target is None


# ===========================================================================
# AC4 — Ranged +N damage bonus
# ===========================================================================


@pytest.mark.parametrize(
    ("extract", "text_builder", "srd"),
    [
        (cwn_extract, _cwn_text, "cwn"),
        (wwn_extract, _wwn_text, "wwn"),
    ],
)
def test_ranged_plus_n_splits_into_dice_and_bonus(extract, text_builder, srd) -> None:  # type: ignore[no-untyped-def]
    """A ranged Damage of '1d8+2' splits into dice='1d8' and bonus=2 (the dice
    validator rejects a '+B' suffix, so the parser must split). Shared parser, so
    BOTH CLIs gain it."""
    row = "Heavy Slugthrower   1d8+2    rifle      8     2     900"
    item = _by_name(extract(text_builder(ranged=row), srd=srd))["Heavy Slugthrower"]
    assert item.damage is not None
    assert item.damage.dice == "1d8"
    assert item.damage.bonus == 2


def test_ranged_plain_damage_has_zero_bonus() -> None:
    """A plain 'NdM' ranged Damage (no +N) yields bonus == 0 — not None, not dropped."""
    item = _by_name(cwn_extract(_cwn_text(ranged="Hold Out Pistol     1d4      pistol     6     1     80"), srd="cwn"))[
        "Hold Out Pistol"
    ]
    assert item.damage is not None
    assert item.damage.dice == "1d4"
    assert item.damage.bonus == 0


def test_ranged_malformed_bonus_fails_loud_with_row_context() -> None:
    """A non-numeric bonus ('1d8+x') must fail LOUD naming the offending row — never a
    silently swallowed or zeroed bonus (No Silent Fallbacks)."""
    bad = "Glitch Rifle        1d8+x    rifle      8     2     900"
    with pytest.raises(ValueError) as exc:
        cwn_extract(_cwn_text(ranged=bad), srd="cwn")
    msg = str(exc.value)
    assert "Glitch Rifle" in msg, f"error did not name the offending row: {msg!r}"


# ===========================================================================
# AC5 — Verbatim round-trip (model + CLI stdout JSON)
# ===========================================================================


def test_new_field_items_survive_model_dump_roundtrip() -> None:
    """Every extracted item — including ones carrying the new soak/trauma/bonus fields
    — survives model_dump() → CatalogItem(**dump) with full equality (nothing dropped
    or truncated on the round-trip)."""
    items = cwn_extract(
        _cwn_text(
            armor="Heavy Plate Rig     17    5    4000    2",
            melee="War Cleaver        1d8      2/AC15    1d10/x2/T6   1     500    Str",
            ranged="Heavy Slugthrower   1d8+2    rifle      8     2     900",
        ),
        srd="cwn",
    )
    assert items, "no items extracted"
    for it in items:
        reconstructed = CatalogItem(**it.model_dump())
        assert reconstructed == it, f"{it.id} did not survive the model round-trip"
    # And the specific new fields are present on the reconstructed items.
    by_name = _by_name([CatalogItem(**it.model_dump()) for it in items])
    assert by_name["Heavy Plate Rig"].mitigation == 5
    assert by_name["War Cleaver"].damage is not None
    assert by_name["War Cleaver"].damage.trauma_die == "1d10"
    assert by_name["Heavy Slugthrower"].damage is not None
    assert by_name["Heavy Slugthrower"].damage.bonus == 2


def test_cli_stdout_json_roundtrip_preserves_new_fields(tmp_path: Path) -> None:
    """The real `python -m` CLI emits JSON that round-trips back into CatalogItems with
    the new fields intact — proving the new schema survives the stdout serialization
    boundary, through the actual entry point (wiring test for the new fields)."""
    fixture = tmp_path / "cwn_new_schema.txt"
    fixture.write_text(
        _cwn_text(
            armor="Heavy Plate Rig     17    5    4000    2",
            melee="War Cleaver        1d8      2/AC15    1d10/x2/T6   1     500    Str",
            ranged="Heavy Slugthrower   1d8+2    rifle      8     2     900",
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "sidequest.cli.cwn_equip_extract", "--srd-path", str(fixture), "--srd", "cwn"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    catalog = json.loads(result.stdout)
    rebuilt = _by_name([CatalogItem(**d) for d in catalog])  # reconstruct from stdout JSON
    assert rebuilt["Heavy Plate Rig"].mitigation == 5
    assert rebuilt["Heavy Plate Rig"].armor_class == 17
    cleaver = rebuilt["War Cleaver"]
    assert cleaver.damage is not None
    assert cleaver.damage.trauma_die == "1d10"
    assert cleaver.damage.trauma_target == 6
    slug = rebuilt["Heavy Slugthrower"]
    assert slug.damage is not None
    assert slug.damage.dice == "1d8"
    assert slug.damage.bonus == 2


# ===========================================================================
# AC6 — CLI hardening: argparse choices + Path.resolve()
# ===========================================================================


@pytest.mark.parametrize("build_parser", [cwn_build_parser, wwn_build_parser])
def test_unknown_srd_value_is_rejected_by_choices(build_parser) -> None:  # type: ignore[no-untyped-def]
    """--srd is choices-constrained: an unknown SRD slug must exit, not silently pass
    through to a self-inconsistent provenance stamp."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--srd-path", "/tmp/x.txt", "--srd", "definitely-not-an-srd"])


@pytest.mark.parametrize("build_parser", [cwn_build_parser, wwn_build_parser])
def test_unknown_license_value_is_rejected_by_choices(build_parser) -> None:  # type: ignore[no-untyped-def]
    """--license is choices-constrained: an unknown license must exit (defense in depth
    over the model's verbatim-license invariant)."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--srd-path", "/tmp/x.txt", "--srd", "cwn", "--license", "made-up-license"])


def test_canonical_srd_and_license_values_are_accepted() -> None:
    """Positive guard: the tool's own canonical --srd / --license values still parse —
    the choices constraint must not reject the happy path."""
    args = cwn_build_parser().parse_args(["--srd-path", "/tmp/x.txt", "--srd", "cwn", "--license", "wn-free"])
    assert args.srd == "cwn"
    assert args.license == "wn-free"


def test_missing_relative_path_error_names_the_resolved_absolute_path(capsys: pytest.CaptureFixture[str]) -> None:
    """main() resolves --srd-path via Path.resolve() so the fail-loud 'does not exist'
    message names the absolute path the operator actually pointed at — not the bare
    relative string (CWE-59 path hardening, lang-review #5)."""
    rel = "nonexistent_114_12_relative.txt"
    rc = cwn_main(["--srd-path", rel, "--srd", "cwn"])
    assert rc != 0
    err = capsys.readouterr().err
    resolved = str(Path(rel).resolve())
    assert os.path.isabs(resolved)
    assert resolved in err, f"error message did not name the resolved absolute path: {err!r}"
