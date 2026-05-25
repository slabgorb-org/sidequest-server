"""LethalityPolicy — per-genre lethality tuning (Group C spec §4.4 + §10)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidequest.genre.lethality_policy_loader import (
    LethalityPolicyMissingError,
    load_lethality_policy,
)
from sidequest.genre.models.lethality import LethalityPolicy, VerdictsOnZeroHp

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def test_minimal_policy_roundtrips():
    policy = LethalityPolicy(
        genre_key="caverns_and_claudes",
        default_reversibility="narrative_only",
        verdicts_on_zero_hp=VerdictsOnZeroHp(pc="humiliated", npc="defeated"),
        soul_md_constraint="genre_truth:comedic_danger_no_permadeath",
        must_narrate="A beat of slapstick pain. Keep it comedic.",
        must_not_narrate="graphic permadeath; somber elegy; last-rites speech",
    )
    assert policy.genre_key == "caverns_and_claudes"
    assert policy.default_reversibility == "narrative_only"
    assert policy.verdicts_on_zero_hp.pc == "humiliated"


def test_unknown_verdict_kind_rejected():
    """Validator must reject verdict kinds not in the LethalityVerdictKind literal."""
    with pytest.raises(ValidationError):
        VerdictsOnZeroHp(pc="obliterated", npc="defeated")  # "obliterated" not in enum


def test_extra_fields_forbidden():
    """`extra='forbid'` catches typos in YAML — silent drop would mask content bugs."""
    with pytest.raises(ValidationError):
        LethalityPolicy(
            genre_key="x",
            default_reversibility="permanent",
            verdicts_on_zero_hp=VerdictsOnZeroHp(pc="dead", npc="dead"),
            soul_md_constraint="x",
            must_narrate="x",
            must_not_narrate="x",
            nonsense_field=True,  # type: ignore[call-arg]
        )


def test_must_narrate_and_must_not_narrate_both_non_blank():
    """Both narrator-tone strings must be non-blank — they ship as a pair."""
    with pytest.raises(ValidationError):
        LethalityPolicy(
            genre_key="x",
            default_reversibility="permanent",
            verdicts_on_zero_hp=VerdictsOnZeroHp(pc="dead", npc="dead"),
            soul_md_constraint="x",
            must_narrate="",
            must_not_narrate="nope",
        )


# Loader tests (appended for Task 2)


def test_loader_reads_valid_yaml(tmp_path: Path):
    pack_dir = tmp_path / "caverns_and_claudes"
    pack_dir.mkdir()
    (pack_dir / "lethality_policy.yaml").write_text(
        textwrap.dedent("""
        genre_key: caverns_and_claudes
        default_reversibility: narrative_only
        verdicts_on_zero_hp:
          pc: humiliated
          npc: defeated
        soul_md_constraint: "genre_truth:comedic_danger_no_permadeath"
        must_narrate: "A beat of slapstick pain. Keep it comedic."
        must_not_narrate: "graphic permadeath; somber elegy; last-rites speech"
    """).strip()
    )
    policy = load_lethality_policy(pack_dir)
    assert policy.genre_key == "caverns_and_claudes"
    assert policy.verdicts_on_zero_hp.pc == "humiliated"


def test_loader_fails_loud_on_missing_file(tmp_path: Path):
    pack_dir = tmp_path / "empty_pack"
    pack_dir.mkdir()
    with pytest.raises(LethalityPolicyMissingError) as exc:
        load_lethality_policy(pack_dir)
    assert "empty_pack" in str(exc.value)


def test_loader_rejects_genre_key_mismatch(tmp_path: Path):
    """genre_key inside the YAML must match the pack directory name."""
    pack_dir = tmp_path / "caverns_and_claudes"
    pack_dir.mkdir()
    (pack_dir / "lethality_policy.yaml").write_text(
        textwrap.dedent("""
        genre_key: some_other_pack
        default_reversibility: permanent
        verdicts_on_zero_hp:
          pc: dead
          npc: dead
        soul_md_constraint: x
        must_narrate: x
        must_not_narrate: x
    """).strip()
    )
    with pytest.raises(ValueError) as exc:
        load_lethality_policy(pack_dir)
    assert "genre_key mismatch" in str(exc.value)


# GenrePack wiring test (Task 4)


def test_genre_pack_exposes_lethality_policy():
    """Once a pack is loaded, its `lethality_policy` attribute is populated."""

    from sidequest.genre.loader import load_genre_pack

    pack = load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes")
    assert pack.lethality_policy is not None
    assert pack.lethality_policy.genre_key == "caverns_and_claudes"
    assert pack.lethality_policy.verdicts_on_zero_hp.pc == "humiliated"


def test_load_genre_pack_wraps_malformed_lethality_policy_in_genre_load_error(tmp_path):
    """Malformed lethality_policy.yaml must surface as GenreLoadError, not raw
    pydantic.ValidationError.

    Mirrors the visibility_baseline precedent in loader.py — every pack-load
    failure flows through GenreLoadError so callers get a unified surface.
    """
    import shutil

    from sidequest.genre.error import GenreLoadError
    from sidequest.genre.loader import load_genre_pack

    src = CONTENT_GENRE_PACKS / "caverns_and_claudes"
    dst = tmp_path / "caverns_and_claudes"
    shutil.copytree(src, dst)
    (dst / "lethality_policy.yaml").write_text(
        "genre_key: caverns_and_claudes\n"
        "default_reversibility: INVALID_ENUM_VALUE\n"
        "verdicts_on_zero_hp:\n"
        "  pc: humiliated\n"
        "  npc: defeated\n"
        "soul_md_constraint: x\n"
        "must_narrate: x\n"
        "must_not_narrate: x\n"
    )

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(dst)
    assert "lethality_policy.yaml" in str(exc_info.value)
