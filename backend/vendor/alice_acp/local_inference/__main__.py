"""``python -m alice_acp.local_inference`` -- run Alice open models locally.

Thin launcher for the LOCAL private-inference CLI. Data never leaves the
device: NO network call, NO credit/ledger, NO side-channel.
"""

from __future__ import annotations

import sys

from alice_acp.local_inference.cli import main

if __name__ == "__main__":
    sys.exit(main())
