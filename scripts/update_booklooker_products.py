#!/usr/bin/env python3
"""CLI wrapper for the Booklooker product synchronizer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from booklooker_client.product_sync import main


if __name__ == "__main__":
    raise SystemExit(main())
