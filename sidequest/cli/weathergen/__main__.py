"""Entry point for ``python -m sidequest.cli.weathergen``."""

from __future__ import annotations

import sys

from sidequest.cli.weathergen.weathergen import main

if __name__ == "__main__":
    sys.exit(main())
