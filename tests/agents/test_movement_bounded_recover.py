"""movement.py bounded branch: cookbook-free routing + loud-but-recoverable
(ADR-157, story 164-10). A materialize failure must NOT propagate out of
run_movement_dispatch — the dispatch contract is recoverable failures only.
"""

from __future__ import annotations

import inspect


def test_bounded_branch_no_longer_imports_cookbook_loader() -> None:
    """The bounded path is cookbook-free: movement.py must not import
    load_cookbook / load_theme_palette / GenreLoadError anymore."""
    import sidequest.agents.subsystems.movement as mv

    for gone in ("load_cookbook", "load_theme_palette", "GenreLoadError"):
        assert not hasattr(mv, gone), f"{gone} should be removed from movement.py"


def test_ensure_bounded_call_is_cookbook_free() -> None:
    """The dispatch calls ensure_bounded_site_materialized with only
    site/archetype/dungeon_repository — no bundle/palette kwargs."""
    from sidequest.dungeon.bounded_site import ensure_bounded_site_materialized

    params = set(inspect.signature(ensure_bounded_site_materialized).parameters)
    assert params == {"site", "archetype", "dungeon_repository"}
