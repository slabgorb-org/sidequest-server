"""Entry point for python -m sidequest.cli.cwn_equip_extract."""

from __future__ import annotations

import sys

from sidequest.cli.cwn_equip_extract.cwn_equip_extract import main

if __name__ == "__main__":
    sys.exit(main())
