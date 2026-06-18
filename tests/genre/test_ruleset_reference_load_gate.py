# sidequest-server/tests/genre/test_ruleset_reference_load_gate.py
from pathlib import Path

import pytest

from sidequest.genre.error import GenreLoadError
from sidequest.genre.ruleset_reference import validate_ruleset_reference


def _write(p: Path, anchor: str, order: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nsrd: fate\nsrd_ref: \"R\"\nlicense: ccby\nanchor: {anchor}\ntitle: T\norder: {order}\n---\nbody\n",
        encoding="utf-8",
    )


def test_gate_passes_when_required_content_present(tmp_path: Path):
    root = tmp_path / "rulesets"
    _write(root / "fate" / "srd" / "01.md", "fate-basics", 1)
    validate_ruleset_reference("fate", rulesets_root=root, pack_name="wry_whimsy", required=frozenset({"fate"}))


def test_gate_fails_when_required_content_missing(tmp_path: Path):
    with pytest.raises(GenreLoadError):
        validate_ruleset_reference(
            "fate", rulesets_root=tmp_path / "rulesets", pack_name="wry_whimsy", required=frozenset({"fate"})
        )


def test_gate_noop_for_unrequired_ruleset(tmp_path: Path):
    # wwn not in the required set this phase -> no error even with no content
    validate_ruleset_reference("wwn", rulesets_root=tmp_path / "rulesets", pack_name="caverns_and_claudes", required=frozenset({"fate"}))
