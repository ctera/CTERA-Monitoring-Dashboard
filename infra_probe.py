#!/usr/bin/env python3
"""Deprecated wrapper: infrastructure probes run via ssh_collect_from_pg.py --infra-out."""

import sys


def main():
    print(
        "infra_probe.py is deprecated; infra_jobs.sh calls ssh_collect_from_pg.py --infra-out directly.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
