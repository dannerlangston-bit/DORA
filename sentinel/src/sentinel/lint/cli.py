"""`sentinel lint`: the modes of 4.15 "Where the lint runs", and `sentinel lint --code` (5.4.1, 5.4.9, 6.2).

    sentinel lint <config-dir> [--all] [--base-ref REV]   whole checkout (Nightly); --base-ref adds the history rules
    sentinel lint <config-dir> --changed BASE              sentinel-config pull request: errors block, warnings are the
                                                           review comment (--review-comment FILE writes it as JSON)
    sentinel lint [<config-dir>] --manifest PATH           product pull request, the sentinel/manifest check: prints the
                                                           commit-status JSON last; informational in Phase 1, blocking
                                                           from Phase 2 (4.3); --base-ref REV adds L-MAN-10
    sentinel lint <config-dir> --all --config-commit SHA   run start (12.7 G1): the run-start lint of the tree at SHA;
                                                           --run-context FILE adds L-XREF-16 and L-MAN-09
    sentinel lint <config-dir> --file FLOW                 one flow in repository context (Recorder, Explorer candidate)

Prints one line per violation, `L-XXX-nn severity file: [location: ]message`, or with `--json` the JSON line of 4.15
(`rule`, `severity`, `file`, `path`, `message`); a count goes to stderr. Returns 1 when any `error` was found (in
`--manifest` mode only from Phase 2), 0 otherwise, 2 on a usage error, a missing directory or a git failure. The
config directory may also be given as `--config DIR`. With `--code` the arguments go to `sentinel.lint.code.main`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from . import diffmode
from .repo import Repo
from .rules import LintReport, Violation, lint


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel lint", description="Validate a sentinel-config checkout (4.1, 4.15).")
    p.add_argument("config_dir", nargs="?", help="root of the sentinel-config checkout")
    p.add_argument("--config", dest="config_opt", metavar="DIR", help="the checkout, when not given positionally")
    p.add_argument("--phase", type=int, choices=(0, 1, 2, 3), default=0,
                   help="project phase: TBD globs and unresolved Phase 0 branches are errors from 1 (default 0)")
    p.add_argument("--today", type=date.fromisoformat, default=None, help="date for expiries, YYYY-MM-DD (default today)")
    p.add_argument("--json", action="store_true", help="one JSON object per violation (4.15)")
    p.add_argument("--show-skipped", action="store_true", help="also list the rules not checkable from files")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--changed", metavar="BASE", help="lint a pull request against the git revision BASE")
    mode.add_argument("--manifest", metavar="PATH", help="lint one change manifest (the sentinel/manifest check)")
    mode.add_argument("--config-commit", metavar="SHA", help="the run-start lint of the checkout's tree at SHA")
    mode.add_argument("--file", metavar="FLOW", help="lint one flow file in the context of the checkout")
    p.add_argument("--all", action="store_true", help="the whole checkout (the default; with --config-commit at run start)")
    p.add_argument("--base-ref", metavar="REV", help="history rules against REV; with --manifest, L-MAN-10 against "
                                                     "the registry at REV (the base build's config commit)")
    p.add_argument("--approved", metavar="FILE", help="approved checkpoint ids for L-XREF-10 (one per line or JSON)")
    p.add_argument("--aa-results", metavar="FILE", help="latest nightly A/A results for L-MSK-05 (JSON list)")
    p.add_argument("--review-comment", metavar="FILE", help="--changed: write the warnings' review comment as JSON")
    p.add_argument("--run-context", metavar="FILE", help="--config-commit: JSON with authorization_ref, "
                                                         "authorization_sha256, candidate and manifest")
    p.add_argument("--ticket-types", metavar="FILE", help="--manifest: JSON map ticket key -> Jira issue type (L-MAN-10)")
    return p


def _json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise diffmode.LintModeError(f"cannot read {path}: {exc}") from exc


def _emit(violations: Iterable[Violation], skipped: dict[str, str], args: argparse.Namespace) -> None:
    for v in violations:
        print(json.dumps(v.as_dict()) if args.json else v.line())
    if args.show_skipped:
        for rid, why in sorted(skipped.items()):
            print(json.dumps({"rule": rid, "severity": "skipped", "message": why}) if args.json else f"{rid} skipped: {why}")


def _count(report: LintReport) -> None:
    print(f"{len(report.errors)} errors, {len(report.warnings)} warnings, {len(report.skipped)} rules skipped",
          file=sys.stderr)


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
    root = args.config_dir or args.config_opt
    if root is None or not Path(root).is_dir():
        print(f"sentinel lint: {root or 'a config directory'} is not a directory", file=sys.stderr)
        return 2
    try:
        return _run(Path(root), args)
    except diffmode.LintModeError as exc:
        print(f"sentinel lint: {exc}", file=sys.stderr)
        return 2


def _run(root: Path, args: argparse.Namespace) -> int:
    approved = diffmode.load_approved(args.approved) if args.approved else None
    aa = diffmode.load_aa_results(args.aa_results) if args.aa_results else None
    if args.manifest:
        return _manifest(root, args)
    if args.file:
        found = diffmode.lint_file(Repo.load(root), args.file, args.phase, args.today)
        report = LintReport(found)
    elif args.changed:
        report, _changed = diffmode.lint_changed(root, args.changed, args.phase, args.today, approved=approved, aa=aa)
        if args.review_comment:
            Path(args.review_comment).write_text(json.dumps(diffmode.review_comment(report), indent=2), encoding="utf-8")
    elif args.config_commit:
        context = _json(args.run_context) if args.run_context else None
        report = diffmode.lint_config_commit(root, args.config_commit, args.phase, args.today, context=context)
        for rec in report.expired:
            print(f"run health: {rec.run_health_line()}", file=sys.stderr)
    else:
        report = lint(root, phase=args.phase, today=args.today)
        if args.base_ref:
            report = _with_history(root, args, report, approved, aa)
    _emit(report.violations, report.skipped, args)
    _count(report)
    return 1 if report.errors else 0


def _with_history(root: Path, args: argparse.Namespace, report: LintReport, approved: Any, aa: Any) -> LintReport:
    history = diffmode.history_at(root, args.base_ref, approved=approved, aa=aa, today=args.today)
    skipped = {k: v for k, v in report.skipped.items() if k not in diffmode.HISTORY_RULES}
    return LintReport(sorted(set(report.violations + history)), skipped, report.expired)


def _manifest(root: Path, args: argparse.Namespace) -> int:
    base_ids = diffmode.registry_ids_at(root, args.base_ref) if args.base_ref else None
    types = _json(args.ticket_types) if args.ticket_types else None
    found = diffmode.lint_manifest_file(Repo.load(root), args.manifest, args.phase, args.today,
                                        base_registry_ids=base_ids, ticket_types=types)
    status, code = diffmode.manifest_status(found, args.phase)
    _emit(found, {}, args)
    print(json.dumps(status))
    _count(LintReport(found))
    return code


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
