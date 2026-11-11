"""`sentinel reenable`: the human safety actions of 6.6 and 6.12 from the command line (8.7, `store.safety`).

    sentinel reenable --db PATH login ALIAS      --by NAME --reason TEXT --ticket KEY --build BUILD_ID [--role ROLE]
    sentinel reenable --db PATH rig RIG_ID       (the same options)
    sentinel reenable --db PATH quarantine ID    (the same options)
    sentinel reenable --db PATH proof (--shard SHARD_ID | --run RUN_ID) --by NAME
    sentinel reenable --db PATH incident ID      --by NAME --reason TEXT [--ticket KEY] [--postmortem REF]

`login`, `rig` and `quarantine` re-enable what a safety control took out of service, refused unless the canary or
drill that would have caught it passed on `--build` (6.12); `proof` records the hand verification of an UNPROVEN
proof (6.6); `incident` closes an incident (6.12). The person acting is `--by`; `--role` is the role they act in
(`system_owner` where 6.6 asks for the system owner's signature). Exit 0 when written, 1 when refused (the reason on
stderr), 2 on a usage error or a database that does not exist.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .store import safety
from .store.store import Store, StoreRefused


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel reenable", description="Human safety actions of 6.6 and 6.12.")
    p.add_argument("--db", required=True, help="the Central Store database")
    sub = p.add_subparsers(dest="what", required=True)
    for kind in safety.REENABLE_KINDS:
        s = sub.add_parser(kind, help=f"re-enable a {kind} (6.12)")
        s.add_argument("target")
        s.add_argument("--by", required=True)
        s.add_argument("--role", default="")
        s.add_argument("--reason", required=True)
        s.add_argument("--ticket", required=True)
        s.add_argument("--build", required=True, help="the current build the canary or drill must have passed on")
    s = sub.add_parser("proof", help="clear an UNPROVEN post-run proof after the hand verification (6.6)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--shard")
    g.add_argument("--run", help="the run-level proof of a run that started no shard")
    s.add_argument("--by", required=True)
    s = sub.add_parser("incident", help="close an incident (6.12)")
    s.add_argument("incident_id", type=int)
    s.add_argument("--by", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--ticket")
    s.add_argument("--postmortem")
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if not Path(a.db).is_file():
        print(f"sentinel reenable: no database at {a.db}", file=sys.stderr)
        return 2
    try:
        with Store(a.db) as store:
            if a.what in safety.REENABLE_KINDS:
                r = safety.reenable(store, a.what, a.target, by=a.by, role=a.role, reason=a.reason, ticket=a.ticket,
                                    build_id=a.build)
                print(f"re-enabled {r.kind} {r.target} by {r.by} (ticket {r.ticket}"
                      + (f"; passed in run {r.evidence_run_id})" if r.evidence_run_id else ")"))
            elif a.what == "proof":
                pid = safety.clear_proof(store, by=a.by, shard_id=a.shard, run_id=a.run)
                print(f"proof {pid} cleared by {a.by}")
            else:
                inc = safety.close_incident(store, a.incident_id, by=a.by, reason=a.reason, ticket_ref=a.ticket,
                                            postmortem_ref=a.postmortem)
                print(f"incident {inc.incident_id} ({inc.severity}) closed by {inc.closed_by}")
    except StoreRefused as e:
        print(f"sentinel reenable: refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
