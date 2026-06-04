from __future__ import annotations

import sys
from collections.abc import Sequence

from alice_acp.api_chat_gateway.service import main as service_main


def main(argv: Sequence[str] | None = None) -> int:
    return service_main(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
