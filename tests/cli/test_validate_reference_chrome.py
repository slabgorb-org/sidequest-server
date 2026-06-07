"""Tests for ``python -m sidequest.cli.validate reference-chrome``.

Validates that a pack's theme.yaml carries every field the v3 reference
renderer requires. Fixture-driven only — never reads live ``genre_packs/*``
(per project rule: no-content-coupled-tests). Live-pack validation is the
CLI's job at runtime, not pytest's.

Story 63-5, Task 23.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from sidequest.cli.validate.reference_chrome import main


def _pack(tmp_path: Path, name: str, theme: dict) -> Path:
    p = tmp_path / name
    p.mkdir()
    (p / "theme.yaml").write_text(yaml.safe_dump(theme))
    return p


_COMPLETE_THEME: dict = {
    "archetype": "rugged",
    "web_font_family": "IM Fell English",
    "display_font_family": "Pirata One",
    "primary": "#8B0000",
    "accent": "#B8860B",
    "background": "#0F0A0A",
    "dinkus": {"glyph": {"light": "†", "medium": "✠", "heavy": "⸸"}},
}


class TestPassingValidation:
    def test_all_fields_present_exits_zero(self, tmp_path: Path) -> None:
        pack = _pack(tmp_path, "good", _COMPLETE_THEME)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code == 0, result.output

    def test_ok_line_includes_pack_name(self, tmp_path: Path) -> None:
        pack = _pack(tmp_path, "good_pack", _COMPLETE_THEME)
        result = CliRunner().invoke(main, [str(pack)])
        assert "[OK]" in result.output
        assert "good_pack" in result.output


class TestMissingTopLevelFields:
    @pytest.mark.parametrize(
        "missing_field",
        [
            "archetype",
            "web_font_family",
            "display_font_family",
            "primary",
            "accent",
            "background",
        ],
    )
    def test_missing_field_exits_nonzero(
        self, tmp_path: Path, missing_field: str
    ) -> None:
        theme = {k: v for k, v in _COMPLETE_THEME.items() if k != missing_field}
        pack = _pack(tmp_path, "bad", theme)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output
        assert missing_field in result.output


class TestMissingNestedDinkusFields:
    @pytest.mark.parametrize("glyph_key", ["light", "medium", "heavy"])
    def test_missing_dinkus_glyph_exits_nonzero(
        self, tmp_path: Path, glyph_key: str
    ) -> None:
        theme = {**_COMPLETE_THEME}
        glyphs = dict(_COMPLETE_THEME["dinkus"]["glyph"])
        del glyphs[glyph_key]
        theme["dinkus"] = {"glyph": glyphs}
        pack = _pack(tmp_path, "bad_dinkus", theme)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output
        assert f"dinkus.glyph.{glyph_key}" in result.output


class TestMissingDinkusStructure:
    def test_missing_dinkus_key_entirely(self, tmp_path: Path) -> None:
        theme = {k: v for k, v in _COMPLETE_THEME.items() if k != "dinkus"}
        pack = _pack(tmp_path, "no_dinkus", theme)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output

    def test_dinkus_without_glyph_key(self, tmp_path: Path) -> None:
        theme = {**_COMPLETE_THEME, "dinkus": {"something_else": "x"}}
        pack = _pack(tmp_path, "bad_dinkus_struct", theme)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output


class TestEdgeCases:
    def test_empty_string_field_fails(self, tmp_path: Path) -> None:
        theme = {**_COMPLETE_THEME, "display_font_family": ""}
        pack = _pack(tmp_path, "empty_field", theme)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output
        assert "display_font_family" in result.output

    def test_whitespace_only_field_fails(self, tmp_path: Path) -> None:
        theme = {**_COMPLETE_THEME, "archetype": "   "}
        pack = _pack(tmp_path, "whitespace", theme)
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output

    def test_no_theme_yaml_fails(self, tmp_path: Path) -> None:
        pack = tmp_path / "no_theme"
        pack.mkdir()
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output

    def test_malformed_yaml_fails(self, tmp_path: Path) -> None:
        pack = tmp_path / "bad_yaml"
        pack.mkdir()
        (pack / "theme.yaml").write_text(": : : not valid yaml [[[")
        result = CliRunner().invoke(main, [str(pack)])
        assert result.exit_code != 0
        assert "[FAIL]" in result.output


class TestWiring:
    def test_subcommand_registered_in_validate_group(self) -> None:
        """Proves reference-chrome is wired into the validate click group."""
        from sidequest.cli.validate.__main__ import cli

        result = CliRunner().invoke(cli, ["reference-chrome", "--help"])
        assert result.exit_code == 0
        assert "reference-chrome" in result.output or "theme.yaml" in result.output
