"""Legacy --trace/--list adapter for the installed, read-only Astrolabe CLI."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from astrolabe.cli import main as cli_main


def main(argv: list[str] | None = None, *, out: TextIO | None = None,
         err: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='~/.astrolabe/astrolabe.db')
    parser.add_argument('--trace', metavar='TRACE_ID')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--host')
    args = parser.parse_args(argv)
    if not args.list and not args.trace:
        parser.print_usage(file=err or sys.stderr)
        print('error: --trace TRACE_ID is required (or use --list)', file=err or sys.stderr)
        return 2
    invocation = ['--db', args.db, 'list'] if args.list else ['--db', args.db, 'show', args.trace]
    if args.host is not None:
        invocation.extend(['--host', args.host])
    return cli_main(invocation, out=out, err=err)


if __name__ == '__main__':
    raise SystemExit(main())
