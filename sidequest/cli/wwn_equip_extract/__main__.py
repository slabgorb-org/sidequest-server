"""Entry point for python -m sidequest.cli.wwn_equip_extract."""

from __future__ import annotations

import sys

from sidequest.cli.wwn_equip_extract.wwn_equip_extract import main

if __name__ == "__main__":
    sys.exit(main())
