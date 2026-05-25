"""Validate that a pack's theme.yaml carries every field the v3 reference renderer requires.

Wraps ``load_reference_theme`` — the same loader the renderer uses at
request time — so validation and rendering share one definition of
"required fields". Missing field → ``[FAIL]`` + non-zero exit.
"""

from __future__ import annotations

from pathlib import Path

import click

from sidequest.server.reference_theme import MissingThemeFieldError, load_reference_theme


@click.command(name="reference-chrome")
@click.argument("pack_dir", type=click.Path(exists=True, file_okay=False))
def main(pack_dir: str) -> None:
    """Validate that <pack_dir>/theme.yaml carries every chrome field."""
    pack_path = Path(pack_dir)
    pack_name = pack_path.name
    try:
        theme = load_reference_theme(pack_path)
    except MissingThemeFieldError as exc:
        click.echo(f"[FAIL] {pack_name}: {exc}", err=True)
        raise SystemExit(1) from None
    click.echo(f"[OK]   {pack_name}: theme={theme.archetype}/{theme.display_font_family}")
