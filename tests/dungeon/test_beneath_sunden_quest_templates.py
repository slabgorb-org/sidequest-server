"""Content-validation test: every beneath_sunden theme carries a quest_template.

Resolves the world dir the same way test_session_integration.py does
(_beneath_sunden_world_dir uses parents[3] to walk up from tests/dungeon/<file>
to the sidequest-content sibling repo).

RED contract: fails until all five theme YAMLs declare a quest_template block.
GREEN contract: every theme in the palette has:
  - quest_template is not None
  - signature in {"reach_deep", "set_piece"}  (big_bad resolution deferred)
  - non-empty title and objective
"""

from __future__ import annotations

from pathlib import Path


def _beneath_sunden_world_dir() -> Path:
    # tests/dungeon/<file> -> tests -> sidequest-server -> repo root;
    # sidequest-content is a sibling of sidequest-server (parents[3]).
    return (
        Path(__file__).resolve().parents[3]
        / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
    )


def test_every_beneath_sunden_theme_has_a_quest_template() -> None:
    from sidequest.dungeon.themes import load_theme_palette

    palette = load_theme_palette(_beneath_sunden_world_dir())
    for tid, theme in palette.themes.items():
        assert theme.quest_template is not None, (
            f"theme {tid!r} is missing a quest_template block — "
            "add quest_template: (signature/title/objective) to its YAML"
        )
        assert theme.quest_template.signature in {"reach_deep", "set_piece"}, (
            f"theme {tid!r} quest_template.signature is {theme.quest_template.signature!r}; "
            "big_bad resolution is not yet wired — use reach_deep or set_piece for now"
        )
        assert theme.quest_template.title.strip(), (
            f"theme {tid!r} quest_template.title is blank"
        )
        assert theme.quest_template.objective.strip(), (
            f"theme {tid!r} quest_template.objective is blank"
        )
