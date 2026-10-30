#!/usr/bin/env python3
"""Build snapshot.json from Seller Central and Smart Warehousing exports.

Stdlib only. Writes the snapshot you choose with --output; do not point that
at this public repository.
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seller.build import main
from seller.schema import SnapshotError

if __name__ == "__main__":
    try:
        main()
    except (SnapshotError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
