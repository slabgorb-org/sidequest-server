"""Weather generator CLI — invokes ``WeatherGenerator`` against an authored pack.

Mirrors the ``namegen`` / ``encountergen`` CLI shape: argparse → load pack →
generate → JSON to stdout. The narrator agent's tool-call pipeline reaches
``WeatherGenerator`` through ``sidequest.game.weather`` directly; this CLI
is the human-facing entry point and the wiring path exercised by the test
suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from sidequest.game.weather import (
    UnknownWeatherSeason,
    UnknownWeatherZone,
    WeatherGenerator,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidequest-weathergen",
        description="Generate a typed WeatherState from a pack's climate rules",
    )
    p.add_argument(
        "--genre-packs-path",
        type=Path,
        default=os.environ.get("SIDEQUEST_CONTENT_PATH"),
        required="SIDEQUEST_CONTENT_PATH" not in os.environ,
        help="Path to the genre_packs/ directory. Also reads SIDEQUEST_CONTENT_PATH.",
    )
    p.add_argument(
        "--genre",
        required=True,
        help="Genre slug (e.g., tea_and_murder).",
    )
    p.add_argument(
        "--zone",
        required=True,
        help="Climate zone id (e.g., glen_floor, highland_pass).",
    )
    p.add_argument(
        "--season",
        required=True,
        help="Season id (e.g., spring, summer, autumn, winter).",
    )
    p.add_argument(
        "--seed",
        type=int,
        required=True,
        help="RNG seed (integer) — same seed yields same WeatherState.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.genre_packs_path is None or str(args.genre_packs_path) in ("", "."):
        # An empty SIDEQUEST_CONTENT_PATH env var resolves to Path('.') here,
        # which would silently look for weather.yaml under CWD. Fail loud.
        print(
            "sidequest-weathergen: --genre-packs-path is empty — set "
            "SIDEQUEST_CONTENT_PATH or pass a concrete path",
            file=sys.stderr,
        )
        return 2
    pack_dir = args.genre_packs_path / args.genre
    weather_path = pack_dir / "weather.yaml"

    try:
        generator = WeatherGenerator(weather_path)
    except FileNotFoundError:
        print(
            f"sidequest-weathergen: no weather.yaml at {weather_path} "
            f"(genre '{args.genre}' has no authored climate rules)",
            file=sys.stderr,
        )
        return 2
    except (yaml.YAMLError, ValidationError, ValueError) as e:
        print(
            f"sidequest-weathergen: failed to load {weather_path}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    try:
        state = generator.generate(zone=args.zone, season=args.season, seed=args.seed)
    except (UnknownWeatherZone, UnknownWeatherSeason) as e:
        print(f"sidequest-weathergen: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(
            f"sidequest-weathergen: weather sampling failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(state.model_dump(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
