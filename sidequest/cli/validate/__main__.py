"""Entry point: ``python -m sidequest.cli.validate <subcommand>``.

Dispatches to one of:

* ``locations`` — Story 54-3 location-manifest validator.
* ``audio`` — Story 50-9 audio.yaml + rules.yaml mood-reference audit.
* ``projection-check`` — projection.yaml audit (legacy single-genre).

Direct module entry remains available for backwards compatibility:
``python -m sidequest.cli.validate.projection_check <genre_dir>``.
"""

from __future__ import annotations

import sys

import click

from sidequest.cli.validate.audio import main as audio_main
from sidequest.cli.validate.locations import main as locations_main
from sidequest.cli.validate.pack import main as pack_main
from sidequest.cli.validate.projection_check import main as projection_check_main


@click.group()
def cli() -> None:
    """SideQuest content validators."""


# ``locations``, ``audio``, and ``pack`` are click.commands — register directly.
cli.add_command(locations_main, name="locations")
cli.add_command(audio_main, name="audio")
cli.add_command(pack_main, name="pack")


@cli.command(name="projection-check")
@click.argument("genre_dir")
def projection_check(genre_dir: str) -> None:
    """Audit a genre pack's projection.yaml."""
    sys.exit(projection_check_main([genre_dir]))


if __name__ == "__main__":
    cli()
