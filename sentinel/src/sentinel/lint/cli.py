"""`sentinel lint <config-dir> [--phase 0|1]` (4.1, 4.15), and `sentinel lint --code` (5.4.1, 5.4.9, 6.2).

Prints one line per violation, `L-XXX-nn severity file: [location: ]message`, or with `--json` the
JSON line of 4.15 (`rule`, `severity`, `file`, `path`, `message`); a count goes to stderr. Returns 1
when any `error` was found, 0 otherwise, 2 on a usage error or a missing directory. With `--code` the arguments go
to `sentinel.lint.code.main`, the gateway single-entry-point lint over the harness source (1 on any violation).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .rules import lint


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel lint", description="Validate a sentinel-config checkout (4.1, 4.15).")
    p.add_argument("config_dir", help="root of the sentinel-config checkout")
    p.add_argument("--phase", type=int, choices=(0, 1, 2, 3), default=0,
                   help="project phase: TBD globs and unresolved Phase 0 branches are errors from 1 (default 0)")
    p.add_argument("--today", type=date.fromisoformat, default=None, help="date for expiries, YYYY-MM-DD (default today)")
    p.add_argument("--json", action="store_true", help="one JSON object per violation (4.15)")
    p.add_argument("--show-skipped", action="store_true", help="also list the rules not checkable from files")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the lint; 1 on any error, 0 otherwise, 2 on bad arguments."""
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in[:1] == ["lint"]:
        args_in = args_in[1:]
    if "--code" in args_in:
        from .code import main as code_main
        return code_main(args_in)
    try:
        args = _parser().parse_args(args_in)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not Path(args.config_dir).is_dir():
        print(f"sentinel lint: {args.config_dir} is not a directory", file=sys.stderr)
        return 2
    report = lint(args.config_dir, phase=args.phase, today=args.today)
    for v in report.violations:
        print(json.dumps(v.as_dict()) if args.json else v.line())
    if args.show_skipped:
        for rid, why in sorted(report.skipped.items()):
            print(json.dumps({"rule": rid, "severity": "skipped", "message": why}) if args.json else f"{rid} skipped: {why}")
    print(f"{len(report.errors)} errors, {len(report.warnings)} warnings, {len(report.skipped)} rules skipped",
          file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
