"""`sentinel` command-line entry point.

Subcommands:
  sentinel lint <config-dir> [--phase N] [--today YYYY-MM-DD] [--json] [--show-skipped]
  sentinel schemas [--check] [--out DIR]      write or check the generated JSON Schemas (L-XREF-09)
  sentinel taxonomy --check                   check config/taxonomy.yaml against the spec (L-XREF-08)
  sentinel demo <scenario|all> [--out DIR] [--today YYYY-MM-DD]
                                              a simulated end-to-end run on config/example (sentinel.demo)
  sentinel run <config-dir> --scenario NAME [--out DIR] [--today YYYY-MM-DD]
                                              the same simulated run on another sentinel-config checkout
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _schemas(argv: list[str]) -> int:
    from .lint import schemas

    p = argparse.ArgumentParser(prog="sentinel schemas")
    p.add_argument("--check", action="store_true")
    p.add_argument("--out", default=str(ROOT / "schemas"))
    a = p.parse_args(argv)
    out = Path(a.out)
    if a.check:
        problems = schemas.check_schemas(out)
        for line in problems or []:
            print(line)
        return 1 if problems else 0
    schemas.write_schemas(out)
    print(f"wrote schemas to {out}")
    return 0


def _taxonomy(argv: list[str]) -> int:
    tool = ROOT / "tools" / "gen_taxonomy.py"
    return subprocess.call([sys.executable, str(tool), *argv])


def _run(argv: list[str]) -> int:
    from datetime import date

    from .demo import DEFAULT_TODAY, SCENARIOS
    from .demo import main as demo_main

    p = argparse.ArgumentParser(prog="sentinel run",
                                description="Run a simulated scenario's pipeline against a sentinel-config checkout.")
    p.add_argument("config_dir")
    p.add_argument("--scenario", required=True, choices=SCENARIOS + ("all",))
    p.add_argument("--out", default="out")
    p.add_argument("--today", type=date.fromisoformat, default=DEFAULT_TODAY)
    a = p.parse_args(argv)
    if not Path(a.config_dir).is_dir():
        print(f"sentinel run: {a.config_dir} is not a directory", file=sys.stderr)
        return 2
    return demo_main([a.scenario, "--out", a.out, "--today", a.today.isoformat(), "--config", a.config_dir])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "lint":
        from .lint.cli import main as lint_main
        return lint_main(rest)
    if cmd == "schemas":
        return _schemas(rest)
    if cmd == "taxonomy":
        return _taxonomy(rest)
    if cmd == "demo":
        from .demo import main as demo_main
        return demo_main(rest)
    if cmd == "run":
        return _run(rest)
    print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
