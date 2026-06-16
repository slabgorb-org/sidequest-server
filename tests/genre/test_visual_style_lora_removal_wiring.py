"""Wiring tests for Epic 43: VisualStyle LoRA-removal cleanup.

These tests exercise the *system*, not the model in isolation:

- Production source (sidequest/) must contain no `.lora`, `.lora_trigger`,
  `.lora_scale`, or `.lora_path` attribute access — guards against the
  field being silently re-added or accessed via duck typing.
- The genre-pack loader must successfully load a real, clean pack
  (heavy_metal) and surface a VisualStyle with the expected core fields.
- Post-43-4: every `visual_style.yaml` in the content tree (both
  genre-level and world-level) must be free of `loras:` and
  `lora_triggers:` blocks. `test_no_visual_style_yaml_has_lora_keys`
  enforces the invariant going forward.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models import VisualStyle
from tests._helpers.genre_paths import find_pack_path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_SRC = REPO_ROOT / "sidequest-server" / "sidequest"
CONTENT_GENRE_PACKS = REPO_ROOT / "sidequest-content" / "genre_packs"


_LORA_ATTR_PATTERN = re.compile(r"\.(lora|lora_trigger|lora_scale|lora_path)\b")


def _iter_server_python_files() -> list[Path]:
    """Return every .py file under SERVER_SRC, skipping __pycache__ trees."""
    return [p for p in SERVER_SRC.rglob("*.py") if "__pycache__" not in p.parts]


class TestNoLoraAttributeAccessInServer:
    def test_no_lora_attribute_access_in_production_code(self) -> None:
        """No production module may read .lora / .lora_trigger /
        .lora_scale / .lora_path off any object. Story 43-2 will scrub the
        daemon side; this test covers the server side and is the wiring
        proof for AC3.

        Scope note: the regex matches dot-prefixed attribute access only
        (e.g. `vs.lora`). It will not flag bare-word mentions of `lora_*`
        in comments, docstrings, or string literals — those would require
        an AST walk. If a future story wants comment/docstring coverage,
        widen the pattern; for now AC3's "grep lora_ across server" is
        satisfied by the attribute-access form, which is what production
        code actually does.
        """
        offenders: list[str] = []
        for path in _iter_server_python_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _LORA_ATTR_PATTERN.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert offenders == [], "Found .lora* attribute access in production code:\n" + "\n".join(
            offenders
        )


class TestVisualStyleLoaderStillWorks:
    def test_load_clean_pack_yields_visual_style(self) -> None:
        """heavy_metal/visual_style.yaml has no LoRA references; it must
        round-trip through the loader to a VisualStyle whose values match
        the on-disk pack content.
        """
        pack = load_genre_pack(find_pack_path("heavy_metal"))
        visual_style = pack.visual_style
        assert isinstance(visual_style, VisualStyle), (
            f"Expected VisualStyle, got {type(visual_style).__name__}"
        )
        # heavy_metal's positive_suffix opens with the Doré reference;
        # the substring is stable on-disk content and a sharper assertion
        # than truthy.
        assert "Doré" in visual_style.positive_suffix, (
            "heavy_metal positive_suffix must reference Gustave Doré "
            f"(got: {visual_style.positive_suffix!r})"
        )
        assert visual_style.preferred_model == "dev", (
            f"heavy_metal preferred_model expected 'dev', got {visual_style.preferred_model!r}"
        )

    def test_no_visual_style_yaml_has_lora_keys(self) -> None:
        """Post-43-4: every `visual_style.yaml` in the content tree —
        genre-level OR world-level — must be free of LoRA YAML keys
        (`loras`, `lora_triggers`, `lora`, `lora_trigger`, `lora_scale`,
        `lora_path`).

        Implementation note: the genre-pack loader exposes the *genre*
        `VisualStyle` via `pack.visual_style`, but it loads world-level
        `visual_style.yaml` as raw `Any` (see `loader._load_single_world`
        — no Pydantic validation, no `__pydantic_extra__` to inspect).
        So rather than route through the loader, we scan every
        `visual_style.yaml` file directly with `yaml.safe_load` and
        assert the top-level mapping carries no forbidden key. This
        catches both genre and world tiers in one pass.

        Also asserts a minimum file count to prevent vacuous success if
        `CONTENT_GENRE_PACKS` ever resolves to the wrong path.

        This is the load-bearing wiring proof that Epic 43 fully removed
        LoRA references from on-disk content — a future commit re-adding
        a `loras:` block to any pack (genre or world) would fail this
        test.
        """
        forbidden_keys = {
            "loras",
            "lora_triggers",
            "lora",
            "lora_trigger",
            "lora_scale",
            "lora_path",
        }
        offenders: list[str] = []
        files_checked = 0
        for visual_yaml in CONTENT_GENRE_PACKS.rglob("visual_style.yaml"):
            raw = yaml.safe_load(visual_yaml.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                offenders.append(
                    f"{visual_yaml.relative_to(REPO_ROOT)}: top-level is not a mapping"
                )
                continue
            for key in forbidden_keys & raw.keys():
                offenders.append(
                    f"{visual_yaml.relative_to(REPO_ROOT)}: contains forbidden key '{key}'"
                )
            files_checked += 1

        # Hard floor: the content tree currently ships 5 genre packs with
        # visual_style.yaml at the genre tier (caverns_and_claudes,
        # elemental_harmony, mutant_wasteland, space_opera, tea_and_murder) plus
        # the two leaf worlds that ship their own override (caverns_sunden,
        # coyote_star) — 7 files. A misconfigured CONTENT_GENRE_PACKS path
        # finding fewer should fail loudly rather than vacuously pass.
        # Bumping this floor is the right reaction when packs are added;
        # lowering it requires explicit justification (most recently:
        # 2026-05-06 Sünden hub-world revert removed the per-dungeon
        # visual_style.yaml files).
        MIN_VISUAL_STYLE_FILES = 7
        assert files_checked >= MIN_VISUAL_STYLE_FILES, (
            f"Expected at least {MIN_VISUAL_STYLE_FILES} visual_style.yaml "
            f"files under {CONTENT_GENRE_PACKS}; found {files_checked}. "
            "CONTENT_GENRE_PACKS may be misconfigured."
        )
        assert offenders == [], (
            "Found LoRA YAML keys still present after Epic 43 cleanup:\n" + "\n".join(offenders)
        )
        # Typed-field surface: nothing LoRA-shaped on the model itself.
        for forbidden in ("lora", "lora_trigger", "lora_scale", "lora_path", "lora_triggers"):
            assert forbidden not in VisualStyle.model_fields, (
                f"VisualStyle.{forbidden} re-introduced into model_fields"
            )

        # Sanity-check that the loader still returns a VisualStyle on a
        # representative pack now that no `loras:` extras exist anywhere.
        pack = load_genre_pack(CONTENT_GENRE_PACKS / "elemental_harmony")
        assert isinstance(pack.visual_style, VisualStyle), (
            f"elemental_harmony visual_style: expected VisualStyle, "
            f"got {type(pack.visual_style).__name__}"
        )


class TestVisualStyleSchemaSurface:
    def test_visual_style_declared_fields_match_post_removal_set(self) -> None:
        """Lock down the post-removal field surface so a future edit can't
        silently re-add a LoRA field. The set is the four core image-gen
        fields plus the visual_tag_overrides map — anything else (or
        anything missing) breaks this test deliberately.
        """
        expected = {
            "positive_suffix",
            "negative_prompt",
            "preferred_model",
            "base_seed",
            "visual_tag_overrides",
        }
        declared = set(VisualStyle.model_fields.keys())
        assert declared == expected, (
            f"VisualStyle declared fields drifted: "
            f"unexpected={declared - expected}, missing={expected - declared}"
        )
