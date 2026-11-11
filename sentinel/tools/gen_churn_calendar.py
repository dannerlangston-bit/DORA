"""Generate a fixture's `churn_calendar` (4.9, 7.9) from the fixture and an explicit exchange holiday list.

Run: PYTHONPATH=src python tools/gen_churn_calendar.py config/example/fixtures/fx.rig-01.paper.yaml \\
         --start 2026-11-09 --holiday 2026-11-26 --holiday 2026-12-25 [--earnings aapl=2027-01-28] \\
         [--ex-dividend aapl=2026-11-10] [--horizon 90]

Prints the `churn_calendar:` block as YAML for the fixture file, which is checked in (4.9). The logic is
`sentinel.churn.generate`; holidays, earnings and ex-dividend dates are explicit until the vendored exchange calendar
of 7.10 (wave 4) and the market-data API supply them, so this tool makes no network call.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import yaml

from sentinel.churn import HORIZON_DAYS, generate


def _by_symbol(pairs: Sequence[str]) -> dict[str, list[date]]:
    """`sym=YYYY-MM-DD` pairs -> symbol id -> dates."""
    out: dict[str, list[date]] = {}
    for p in pairs:
        sid, sep, day = p.partition("=")
        if not sep or not sid:
            raise SystemExit(f"expected <symbol id>=<date>, got {p!r}")
        out.setdefault(sid, []).append(date.fromisoformat(day))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fixture", type=Path)
    ap.add_argument("--start", type=date.fromisoformat, required=True, help="first date of the calendar (New York)")
    ap.add_argument("--holiday", type=date.fromisoformat, action="append", default=[], help="an exchange holiday")
    ap.add_argument("--earnings", action="append", default=[], help="<symbol id>=<date>")
    ap.add_argument("--ex-dividend", action="append", default=[], help="<symbol id>=<date>")
    ap.add_argument("--horizon", type=int, default=HORIZON_DAYS, help="days after --start (default 90, 7.10 G4)")
    args = ap.parse_args(argv)
    fixture = yaml.safe_load(args.fixture.read_text())
    events = generate(fixture, start=args.start, holidays=args.holiday, earnings=_by_symbol(args.earnings),
                      ex_dividend=_by_symbol(args.ex_dividend), horizon_days=args.horizon)
    lines = ["churn_calendar:"]
    for e in events:
        flow = yaml.safe_dump(e.to_dict(), default_flow_style=True, sort_keys=False, width=200).strip()
        lines.append(f"  - {flow}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
