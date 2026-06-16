"""python -m sidequest.cli.validate.projection_check — projection.yaml audit tool."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(genre_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sidequest.cli.validate.projection_check", str(genre_dir)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),  # sidequest-server root
    )


def test_valid_projection_yaml_prints_table(tmp_path: Path) -> None:
    genre_dir = tmp_path / "testgenre"
    genre_dir.mkdir()
    (genre_dir / "projection.yaml").write_text(
        """rules:
  - kind: NARRATION
    redact_fields:
      - field: text
        unless: is_gm()
        mask: null
"""
    )
    result = _run(genre_dir)
    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"
    assert "NARRATION" in result.stdout
    assert "text" in result.stdout
    assert "is_gm" in result.stdout


def test_invalid_projection_yaml_exits_nonzero(tmp_path: Path) -> None:
    genre_dir = tmp_path / "bad"
    genre_dir.mkdir()
    (genre_dir / "projection.yaml").write_text(
        """rules:
  - kind: NOT_A_REAL_KIND
    target_only:
      field: to
"""
    )
    result = _run(genre_dir)
    assert result.returncode != 0
    assert "unknown kind" in (result.stderr + result.stdout).lower()


def test_missing_projection_yaml_is_ok(tmp_path: Path) -> None:
    genre_dir = tmp_path / "empty"
    genre_dir.mkdir()
    result = _run(genre_dir)
    assert result.returncode == 0
    assert "no projection.yaml" in result.stdout.lower()
