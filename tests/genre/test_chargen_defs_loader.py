"""Tests: loader picks up backgrounds.yaml / foci.yaml / skills.yaml
and populates GenrePack.backgrounds / GenrePack.foci / GenrePack.skills.

Uses a SYNTHETIC pack dir (tmp_path) — never loads a real pack from
genre_packs/.  Mirrors the shape of test_classes_yaml_loader.py but for
the ADR-143 chargen-def files.

Also covers:
  - absent files → empty dict/list (the documented correct state, not a
    fallback masking a config error)
  - present-but-malformed file raises (pydantic error propagates; no swallow)
  - resolve_backgrounds / resolve_foci world-first semantics
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from tests._helpers.genre_paths import find_pack_path

_CAVERNS_PACK_DIR = find_pack_path("caverns_and_claudes")


def _clone_pack(src: Path, dst: Path) -> Path:
    """Deep-copy a pack so the test can mutate the copy safely.

    Also updates lethality_policy.yaml genre_key to match the new directory
    name, since the loader validates genre_key matches the pack directory name.
    """
    shutil.copytree(src, dst)
    lethality_yaml = dst / "lethality_policy.yaml"
    if lethality_yaml.exists():
        with lethality_yaml.open("r", encoding="utf-8") as f:
            policy_data = yaml.safe_load(f)
        policy_data["genre_key"] = dst.name
        with lethality_yaml.open("w", encoding="utf-8") as f:
            yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)
    return dst


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_backgrounds_absent_yields_empty_dict(tmp_path: Path) -> None:
    """A pack without backgrounds.yaml loads with pack.backgrounds == {}."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_no_backgrounds")
    # Ensure the file does not exist
    bg_file = pack_dir / "backgrounds.yaml"
    if bg_file.exists():
        bg_file.unlink()

    pack = load_genre_pack(pack_dir)
    assert pack.backgrounds == {}


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_foci_absent_yields_empty_dict(tmp_path: Path) -> None:
    """A pack without foci.yaml loads with pack.foci == {}."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_no_foci")
    foci_file = pack_dir / "foci.yaml"
    if foci_file.exists():
        foci_file.unlink()

    pack = load_genre_pack(pack_dir)
    assert pack.foci == {}


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_skills_absent_yields_empty_list(tmp_path: Path) -> None:
    """A pack without skills.yaml loads with pack.skills == []."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_no_skills")
    skills_file = pack_dir / "skills.yaml"
    if skills_file.exists():
        skills_file.unlink()

    pack = load_genre_pack(pack_dir)
    assert pack.skills == []


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_backgrounds_yaml_loads_entries(tmp_path: Path) -> None:
    """backgrounds.yaml is parsed and entries become Background instances keyed by id."""
    from sidequest.genre.loader import load_genre_pack
    from sidequest.genre.models.character import Background

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_with_backgrounds")
    (pack_dir / "backgrounds.yaml").write_text(
        "- id: locksmith\n"
        "  display_name: Locksmith\n"
        "  description: You pick locks for a living.\n"
        "  free_skill: Sneak\n"
        "  quick_skills: [Notice, Connect]\n"
        "- id: soldier\n"
        "  display_name: Soldier\n"
        "  description: You served in a militia.\n"
        "  free_skill: Stab\n"
        "  quick_skills: [Exert, Survive]\n",
        encoding="utf-8",
    )
    pack = load_genre_pack(pack_dir)
    assert len(pack.backgrounds) == 2
    assert all(isinstance(b, Background) for b in pack.backgrounds.values())
    assert set(pack.backgrounds.keys()) == {"locksmith", "soldier"}
    assert pack.backgrounds["locksmith"].free_skill == "Sneak"


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_foci_yaml_loads_entries(tmp_path: Path) -> None:
    """foci.yaml is parsed and entries become Focus instances keyed by id."""
    from sidequest.genre.loader import load_genre_pack
    from sidequest.genre.models.character import Focus

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_with_foci")
    (pack_dir / "foci.yaml").write_text(
        "- id: die_hard\n"
        "  display_name: Die Hard\n"
        "  description: You are hard to kill.\n"
        "  levels:\n"
        "    - skills:\n"
        "        Endure: 1\n"
        "      abilities: []\n"
        "    - skills:\n"
        "        Endure: 2\n"
        "      abilities: []\n"
        "- id: alert\n"
        "  display_name: Alert\n"
        "  description: You are never caught off guard.\n"
        "  levels:\n"
        "    - skills:\n"
        "        Notice: 1\n"
        "      abilities: []\n",
        encoding="utf-8",
    )
    pack = load_genre_pack(pack_dir)
    assert len(pack.foci) == 2
    assert all(isinstance(f, Focus) for f in pack.foci.values())
    assert set(pack.foci.keys()) == {"die_hard", "alert"}
    assert pack.foci["die_hard"].levels[0].skills == {"Endure": 1}


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_backgrounds_yaml_non_list_fails_loud(tmp_path: Path) -> None:
    """A present-but-malformed backgrounds.yaml (top-level mapping, not a list)
    fails loud with GenreLoadError — no silent fallback to {}.
    """
    from sidequest.genre.error import GenreLoadError
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_bad_backgrounds")
    # Top-level mapping (a dict) instead of the required list.
    (pack_dir / "backgrounds.yaml").write_text(
        "locksmith:\n  display_name: Locksmith\n  free_skill: Sneak\n",
        encoding="utf-8",
    )
    with pytest.raises(GenreLoadError):
        load_genre_pack(pack_dir)


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_foci_yaml_missing_id_raises_validation_error(tmp_path: Path) -> None:
    """A foci.yaml entry missing the required ``id`` field raises a pydantic
    ValidationError (NOT a bare KeyError) because the loader validates each
    entry into a Focus model before keying the dict by the validated ``.id``.
    """
    from pydantic import ValidationError

    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_focus_no_id")
    # Entry is missing the required ``id`` field.
    (pack_dir / "foci.yaml").write_text(
        "- display_name: Die Hard\n"
        "  description: You are hard to kill.\n"
        "  levels:\n"
        "    - skills:\n"
        "        Endure: 1\n"
        "      abilities: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_genre_pack(pack_dir)


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_skills_yaml_loads_entries(tmp_path: Path) -> None:
    """skills.yaml is parsed and entries become a list of skill name strings."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "pack_with_skills")
    (pack_dir / "skills.yaml").write_text(
        "- Administer\n- Connect\n- Convince\n",
        encoding="utf-8",
    )
    pack = load_genre_pack(pack_dir)
    assert pack.skills == ["Administer", "Connect", "Convince"]


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------


def test_resolve_backgrounds_world_first() -> None:
    """World-tier backgrounds replace genre-tier when world declares any."""
    from typing import cast

    from sidequest.genre.models.character import Background
    from sidequest.genre.models.pack import GenrePack, World
    from sidequest.server.dispatch.chargen_defs_resolve import resolve_backgrounds

    genre_bg = cast(
        Background,
        Background.model_construct(id="soldier", display_name="Soldier", free_skill="Stab"),
    )
    world_bg = cast(
        Background,
        Background.model_construct(id="locksmith", display_name="Locksmith", free_skill="Sneak"),
    )
    world_obj = cast(World, World.model_construct(backgrounds={"locksmith": world_bg}))
    pack = cast(
        GenrePack,
        GenrePack.model_construct(
            backgrounds={"soldier": genre_bg},
            worlds={"the_world": world_obj},
        ),
    )

    result = resolve_backgrounds(pack, "the_world")
    assert set(result.keys()) == {"locksmith"}, "world roster must replace genre roster"
    assert result["locksmith"].free_skill == "Sneak"


def test_resolve_backgrounds_falls_back_to_genre() -> None:
    """World with no backgrounds falls through to genre tier."""
    from typing import cast

    from sidequest.genre.models.character import Background
    from sidequest.genre.models.pack import GenrePack, World
    from sidequest.server.dispatch.chargen_defs_resolve import resolve_backgrounds

    genre_bg = cast(
        Background,
        Background.model_construct(id="soldier", display_name="Soldier", free_skill="Stab"),
    )
    world_obj = cast(World, World.model_construct(backgrounds={}))
    pack = cast(
        GenrePack,
        GenrePack.model_construct(
            backgrounds={"soldier": genre_bg},
            worlds={"the_world": world_obj},
        ),
    )

    result = resolve_backgrounds(pack, "the_world")
    assert set(result.keys()) == {"soldier"}


def test_resolve_foci_world_first() -> None:
    """World-tier foci replace genre-tier when world declares any."""
    from typing import cast

    from sidequest.genre.models.character import Focus
    from sidequest.genre.models.pack import GenrePack, World
    from sidequest.server.dispatch.chargen_defs_resolve import resolve_foci

    genre_focus = cast(Focus, Focus.model_construct(id="alert", display_name="Alert", levels=[]))
    world_focus = cast(
        Focus, Focus.model_construct(id="die_hard", display_name="Die Hard", levels=[])
    )
    world_obj = cast(World, World.model_construct(foci={"die_hard": world_focus}))
    pack = cast(
        GenrePack,
        GenrePack.model_construct(
            foci={"alert": genre_focus},
            worlds={"the_world": world_obj},
        ),
    )

    result = resolve_foci(pack, "the_world")
    assert set(result.keys()) == {"die_hard"}, "world foci must replace genre foci"


def test_resolve_foci_falls_back_to_genre() -> None:
    """World with no foci falls through to genre tier."""
    from typing import cast

    from sidequest.genre.models.character import Focus
    from sidequest.genre.models.pack import GenrePack, World
    from sidequest.server.dispatch.chargen_defs_resolve import resolve_foci

    genre_focus = cast(Focus, Focus.model_construct(id="alert", display_name="Alert", levels=[]))
    world_obj = cast(World, World.model_construct(foci={}))
    pack = cast(
        GenrePack,
        GenrePack.model_construct(
            foci={"alert": genre_focus},
            worlds={"the_world": world_obj},
        ),
    )

    result = resolve_foci(pack, "the_world")
    assert set(result.keys()) == {"alert"}


def test_resolve_backgrounds_none_world_slug() -> None:
    """None world_slug falls through to genre tier."""
    from typing import cast

    from sidequest.genre.models.character import Background
    from sidequest.genre.models.pack import GenrePack
    from sidequest.server.dispatch.chargen_defs_resolve import resolve_backgrounds

    genre_bg = cast(
        Background,
        Background.model_construct(id="soldier", display_name="Soldier", free_skill="Stab"),
    )
    pack = cast(
        GenrePack,
        GenrePack.model_construct(backgrounds={"soldier": genre_bg}, worlds={}),
    )

    result = resolve_backgrounds(pack, None)
    assert set(result.keys()) == {"soldier"}


def test_resolve_foci_none_world_slug() -> None:
    """None world_slug falls through to genre tier."""
    from typing import cast

    from sidequest.genre.models.character import Focus
    from sidequest.genre.models.pack import GenrePack
    from sidequest.server.dispatch.chargen_defs_resolve import resolve_foci

    genre_focus = cast(Focus, Focus.model_construct(id="alert", display_name="Alert", levels=[]))
    pack = cast(
        GenrePack,
        GenrePack.model_construct(foci={"alert": genre_focus}, worlds={}),
    )

    result = resolve_foci(pack, None)
    assert set(result.keys()) == {"alert"}


# ---------------------------------------------------------------------------
# Builder setter wiring test
# ---------------------------------------------------------------------------


def test_with_chargen_defs_stores_backgrounds_and_foci() -> None:
    """with_chargen_defs() attaches backgrounds + foci and returns self (fluent)."""
    from typing import cast

    from sidequest.game.builder import CharacterBuilder
    from sidequest.genre.models.character import (
        Background,
        CharCreationChoice,
        CharCreationScene,
        Focus,
        MechanicalEffects,
    )
    from sidequest.genre.models.rules import RulesConfig

    bg = cast(
        Background,
        Background.model_construct(id="locksmith", display_name="Locksmith", free_skill="Sneak"),
    )
    focus = cast(Focus, Focus.model_construct(id="die_hard", display_name="Die Hard", levels=[]))

    # Build a minimal scene for the builder constructor
    scene = CharCreationScene(
        id="origin",
        title="Origin",
        narration="Who are you?",
        choices=[
            CharCreationChoice(
                label="Wanderer",
                description="You wander.",
                mechanical_effects=MechanicalEffects(),
            )
        ],
    )
    rules = cast(
        RulesConfig,
        RulesConfig.model_construct(
            ruleset="dial",
            stat_generation="roll_4d6_drop_lowest",
            ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            default_class=None,
            default_race=None,
            edge_config=None,
            point_buy_budget=27,
            standard_array=None,
            race_label=None,
            class_label=None,
            disposition_thresholds=None,
            allowed_classes=[],
            confrontations=[],
        ),
    )

    builder = CharacterBuilder(scenes=[scene], rules=rules)
    returned = builder.with_chargen_defs(backgrounds={"locksmith": bg}, foci={"die_hard": focus})

    assert returned is builder, "with_chargen_defs must return self (fluent)"
    assert builder._backgrounds == {"locksmith": bg}
    assert builder._foci == {"die_hard": focus}


def test_builder_defaults_empty_chargen_defs() -> None:
    """Builder without with_chargen_defs() has empty _backgrounds/_foci by default."""
    from typing import cast

    from sidequest.game.builder import CharacterBuilder
    from sidequest.genre.models.character import (
        CharCreationChoice,
        CharCreationScene,
        MechanicalEffects,
    )
    from sidequest.genre.models.rules import RulesConfig

    scene = CharCreationScene(
        id="origin",
        title="Origin",
        narration="Who are you?",
        choices=[
            CharCreationChoice(
                label="Wanderer",
                description="You wander.",
                mechanical_effects=MechanicalEffects(),
            )
        ],
    )
    rules = cast(
        RulesConfig,
        RulesConfig.model_construct(
            ruleset="dial",
            stat_generation="roll_4d6_drop_lowest",
            ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            default_class=None,
            default_race=None,
            edge_config=None,
            point_buy_budget=27,
            standard_array=None,
            race_label=None,
            class_label=None,
            disposition_thresholds=None,
            allowed_classes=[],
            confrontations=[],
        ),
    )

    builder = CharacterBuilder(scenes=[scene], rules=rules)
    assert builder._backgrounds == {}
    assert builder._foci == {}
