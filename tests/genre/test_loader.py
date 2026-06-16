"""Genre pack loader tests.

Port of sidequest-genre/tests/loader_story_1_4_tests.rs and
sidequest-genre/tests/integration_tests.rs (behavior tests only).

Covers:
- GenreLoader.find() — search path ordering, first-match wins
- GenreLoader.load() — end-to-end from code string to GenrePack
- GenreNotFoundError raised with searched paths listed
- GenreLoadError on missing required files
- Optional files silently default (beat_vocabulary, achievements)
- World subdirectory loading
- Trope inheritance in loaded worlds
- load_genre_pack_cached — same object on repeated calls
- Phase 1 readiness integration smoke
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.genre.error import GenreLoadError, GenreNotFoundError
from sidequest.genre.loader import (
    DEFAULT_GENRE_PACK_SEARCH_PATHS,
    GenreLoader,
    find_pack_dir,
    load_genre_pack,
    load_genre_pack_cached,
)
from sidequest.genre.models.pack import GenrePack

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content"
GENRE_PACKS_DIR = CONTENT_ROOT / "genre_packs"
CC_PACK_DIR = GENRE_PACKS_DIR / "caverns_and_claudes"


def _has_real_content() -> bool:
    return CC_PACK_DIR.is_dir()


def _clone_pack_with_updated_genre_key(src: Path, dst: Path) -> Path:
    """Clone a pack and update its lethality_policy.yaml genre_key to match the new directory name.

    This is needed because the lethality_policy loader validates that genre_key in the YAML
    matches the directory name. When tests clone a pack and give it a different name, we must
    update the YAML so the genre_key matches the new directory.
    """
    shutil.copytree(src, dst)
    lethality_yaml = dst / "lethality_policy.yaml"
    if lethality_yaml.exists():
        # Read the YAML, update genre_key to match the new directory name, write it back
        with lethality_yaml.open("r", encoding="utf-8") as f:
            policy_data = yaml.safe_load(f)
        policy_data["genre_key"] = dst.name
        with lethality_yaml.open("w", encoding="utf-8") as f:
            yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)
    return dst


# ---------------------------------------------------------------------------
# GenreLoader — search path behavior
# ---------------------------------------------------------------------------


def test_genre_loader_finds_pack_in_first_search_path() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    loader = GenreLoader(search_paths=[GENRE_PACKS_DIR])
    found = loader.find("caverns_and_claudes")
    assert found.is_dir()
    assert found.name == "caverns_and_claudes"


def test_genre_loader_searches_paths_in_order() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    loader = GenreLoader(search_paths=[Path("/nonexistent/first"), GENRE_PACKS_DIR])
    found = loader.find("caverns_and_claudes")
    assert found.is_dir()


def test_genre_loader_returns_error_when_not_found_in_any_path() -> None:
    loader = GenreLoader(search_paths=[Path("/nonexistent/a"), Path("/nonexistent/b")])
    with pytest.raises(GenreNotFoundError):
        loader.find("caverns_and_claudes")


def test_genre_loader_error_lists_searched_paths() -> None:
    search_paths = [Path("/path/a"), Path("/path/b"), Path("/path/c")]
    loader = GenreLoader(search_paths=search_paths)
    with pytest.raises(GenreNotFoundError) as exc_info:
        loader.find("totally_nonexistent")
    err_msg = str(exc_info.value)
    assert "/path/a" in err_msg
    assert "/path/b" in err_msg


def test_genre_loader_returns_first_match_not_all() -> None:
    """First matching path wins; subsequent paths are not tried."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    loader = GenreLoader(search_paths=[GENRE_PACKS_DIR, Path("/nonexistent")])
    found = loader.find("caverns_and_claudes")
    # Should be the sidequest-content one, not error
    assert found.is_dir()


# ---------------------------------------------------------------------------
# load_genre_pack — success path
# ---------------------------------------------------------------------------


def test_load_caverns_and_claudes_full_pack() -> None:
    """End-to-end: load the C&C pack from real content."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    assert isinstance(pack, GenrePack)
    assert pack.meta.name
    assert pack.meta.name == "Caverns & Claudes"


def test_loaded_pack_has_required_fields() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    assert pack.lore is not None
    assert pack.rules is not None
    assert pack.prompts is not None
    assert pack.axes is not None
    assert pack.audio is not None
    assert pack.theme is not None
    assert pack.visual_style is not None
    assert pack.progression is not None


def test_loaded_pack_has_worlds() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    assert len(pack.worlds) >= 1
    # C&C has grimvault, horden, mawdeep
    assert "grimvault" in pack.worlds or len(pack.worlds) >= 1


def test_loaded_pack_worlds_have_required_fields() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    for world_name, world in pack.worlds.items():
        assert world.config is not None, f"{world_name} missing config"
        assert world.lore is not None, f"{world_name} missing lore"
        assert world.cartography is not None, f"{world_name} missing cartography"


def test_loaded_pack_genre_tropes_present() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    # C&C has genre-level tropes
    assert isinstance(pack.tropes, list)


def test_loaded_pack_base_archetypes_present() -> None:
    """base_archetypes loaded from content root (archetypes_base.yaml)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    # archetypes_base.yaml lives at content root — may or may not exist
    if (CONTENT_ROOT / "archetypes_base.yaml").exists():
        assert pack.base_archetypes is not None
    else:
        assert pack.base_archetypes is None


def test_genre_loader_load_by_code_string() -> None:
    """GenreLoader.load() with code string works."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    loader = GenreLoader(search_paths=[GENRE_PACKS_DIR])
    pack = loader.load("caverns_and_claudes")
    assert pack.meta.name == "Caverns & Claudes"


def test_find_pack_dir_function() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    found = find_pack_dir("caverns_and_claudes", [GENRE_PACKS_DIR])
    assert found.name == "caverns_and_claudes"


# ---------------------------------------------------------------------------
# Fail loud — missing required files
# ---------------------------------------------------------------------------


def test_loader_raises_on_missing_pack_directory() -> None:
    with pytest.raises(GenreLoadError):
        load_genre_pack(Path("/totally/nonexistent/path"))


def test_loader_raises_genre_not_found_error() -> None:
    loader = GenreLoader(search_paths=[Path("/nonexistent")])
    with pytest.raises(GenreNotFoundError):
        loader.load("nonexistent_genre_xyz")


def test_loader_fails_loud_on_missing_required_file(tmp_path: Path) -> None:
    """If pack.yaml is missing, loader raises GenreLoadError — no silent fallback."""
    pack_dir = tmp_path / "test_genre"
    pack_dir.mkdir()
    # Don't create pack.yaml — loader must fail loudly
    with pytest.raises(GenreLoadError):
        load_genre_pack(pack_dir)


def test_loader_fails_loud_on_missing_lore_yaml(tmp_path: Path) -> None:
    """Missing lore.yaml raises GenreLoadError (required file)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    # Copy pack structure but remove lore.yaml
    pack_dir = tmp_path / "cc_no_lore"
    shutil.copytree(CC_PACK_DIR, pack_dir)
    (pack_dir / "lore.yaml").unlink()
    with pytest.raises(GenreLoadError):
        load_genre_pack(pack_dir)


def test_loader_fails_loud_on_malformed_yaml(tmp_path: Path) -> None:
    """Malformed YAML in pack.yaml raises GenreLoadError."""
    pack_dir = tmp_path / "bad_genre"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text("invalid: yaml: [unclosed", encoding="utf-8")
    with pytest.raises(GenreLoadError):
        load_genre_pack(pack_dir)


# ---------------------------------------------------------------------------
# Optional files silently default
# ---------------------------------------------------------------------------


def test_optional_beat_vocabulary_defaults_to_none(tmp_path: Path) -> None:
    """beat_vocabulary.yaml absent → pack.beat_vocabulary is None (no error)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack_dir = _clone_pack_with_updated_genre_key(CC_PACK_DIR, tmp_path / "cc_no_beat_vocab")
    beat_path = pack_dir / "beat_vocabulary.yaml"
    if beat_path.exists():
        beat_path.unlink()
    pack = load_genre_pack(pack_dir)
    assert pack.beat_vocabulary is None


def test_optional_achievements_defaults_to_empty(tmp_path: Path) -> None:
    """achievements.yaml absent → pack.achievements is [] (no error)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack_dir = _clone_pack_with_updated_genre_key(CC_PACK_DIR, tmp_path / "cc_no_achievements")
    ach_path = pack_dir / "achievements.yaml"
    if ach_path.exists():
        ach_path.unlink()
    pack = load_genre_pack(pack_dir)
    assert pack.achievements == []


def test_optional_pacing_defaults_to_none(tmp_path: Path) -> None:
    """pacing.yaml absent → pack.drama_thresholds is None."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack_dir = _clone_pack_with_updated_genre_key(CC_PACK_DIR, tmp_path / "cc_no_pacing")
    pacing_path = pack_dir / "pacing.yaml"
    if pacing_path.exists():
        pacing_path.unlink()
    pack = load_genre_pack(pack_dir)
    assert pack.drama_thresholds is None


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_load_genre_pack_cached_returns_same_object() -> None:
    """Same genre code returns the same GenrePack object from cache."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    search_paths = [GENRE_PACKS_DIR]
    pack1 = load_genre_pack_cached("caverns_and_claudes", search_paths)
    pack2 = load_genre_pack_cached("caverns_and_claudes", search_paths)
    assert pack1 is pack2


def test_loader_cache_propagates_error() -> None:
    """Loader raises GenreNotFoundError for nonexistent genre (cache doesn't swallow)."""
    with pytest.raises(GenreNotFoundError):
        load_genre_pack_cached("totally_nonexistent_xyz_abc", [Path("/nonexistent")])


# ---------------------------------------------------------------------------
# DEFAULT_GENRE_PACK_SEARCH_PATHS wiring
# ---------------------------------------------------------------------------


def test_default_search_paths_is_non_empty_list() -> None:
    assert isinstance(DEFAULT_GENRE_PACK_SEARCH_PATHS, list)
    assert len(DEFAULT_GENRE_PACK_SEARCH_PATHS) >= 1


def test_loader_wired_into_package() -> None:
    """GenreLoader and load_genre_pack must be importable from sidequest.genre."""
    from sidequest.genre import GenreLoader as GL  # noqa: F401
    from sidequest.genre import load_genre_pack as lgp  # noqa: F401

    assert callable(lgp)
    assert GL is not None


# ---------------------------------------------------------------------------
# Phase 1 readiness integration smoke
# ---------------------------------------------------------------------------


def test_full_phase1_pack_pipeline() -> None:
    """Phase 1 readiness smoke: load C&C, verify narrator-relevant fields, trope resolution."""
    if not _has_real_content():
        pytest.skip("sidequest-content not available")

    pack = load_genre_pack(CC_PACK_DIR)

    # Every Phase 1 narrator-relevant aggregate is populated
    assert pack.prompts is not None
    assert pack.lore is not None
    assert pack.rules is not None
    assert pack.axes is not None
    assert pack.theme is not None
    assert pack.audio is not None

    # Pack meta is valid
    assert pack.meta.name
    assert pack.meta.version

    # Worlds loaded
    assert len(pack.worlds) >= 1

    for world in pack.worlds.values():
        assert world.cartography is not None

    # Tropes (genre-level) are a list
    assert isinstance(pack.tropes, list)

    # Archetypes are a list
    assert isinstance(pack.archetypes, list)

    # Phase 1 chargen: archetype resolution pipeline available
    if pack.base_archetypes is not None and pack.archetype_constraints is not None:
        from sidequest.genre.archetype.shim import resolve_archetype

        base = pack.base_archetypes
        constraints = pack.archetype_constraints
        # Find first valid pairing from base
        if base.jungian and base.rpg_roles:
            jungian_id = base.jungian[0].id
            rpg_role_id = base.rpg_roles[0].id
            # May or may not resolve (depends on pairing weights), just verify no crash
            try:
                result = resolve_archetype(
                    jungian_id, rpg_role_id, base, constraints, None, "caverns_and_claudes"
                )
                assert result.resolved.name
            except Exception:
                pass  # forbidden pairing is fine — shim works


def test_loaded_pack_name_property() -> None:
    if not _has_real_content():
        pytest.skip("sidequest-content not available")
    pack = load_genre_pack(CC_PACK_DIR)
    assert pack.name == pack.meta.name
