"""``python -m alice_acp.transport_service`` — the runnable LTC transport service.

Thin launcher: it reads the process environment (NO args parsed here — the service
is env-driven, like the credit server's scheduler flags) and hands off to
:func:`alice_acp.transport_service.deploy.build_and_run`, which wires + runs the
asyncio service and returns a process exit code. Fail-soft: a misconfiguration is a
clear, secret-free stderr message + a non-zero exit, never a traceback/crash.

CREDIT-ONLY: nothing here sets a reward/payout/chain symbol; the credited unit is the
validator's shared-store write (the credit server drains it). No secret is read or
printed here.
"""

from __future__ import annotations

import sys

from alice_acp.transport_service.deploy import build_and_run


def main() -> int:
    # Production: env=None => the real process environment is authoritative for both
    # the config and the resolver's gate/lane lookup.
    return build_and_run(env=None)


if __name__ == "__main__":
    sys.exit(main())
