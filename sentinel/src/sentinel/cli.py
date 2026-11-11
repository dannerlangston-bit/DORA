"""`sentinel` command-line entry point.

Subcommands:
  sentinel lint <config-dir> [--all] [--base-ref REV] [--phase N] [--today YYYY-MM-DD] [--json] [--show-skipped]
                                              the whole checkout (Nightly); --base-ref adds the history rules (4.15)
  sentinel lint <config-dir> --changed BASE [--review-comment FILE] [--approved FILE] [--aa-results FILE]
                                              a sentinel-config pull request: errors block, warnings are the review
                                              comment
  sentinel lint [<config-dir>] --manifest PATH [--base-ref REV] [--ticket-types FILE]
                                              a product pull request's manifest: the sentinel/manifest commit status
  sentinel lint <config-dir> --all --config-commit SHA [--run-context FILE]
                                              the run-start lint of the tree at SHA (12.7 G1)
  sentinel lint <config-dir> --file FLOW      one flow in repository context (Recorder, Explorer candidate)
  sentinel lint --code [ROOT] [--drill-allowlist FILE] [--json]
                                              the gateway single-entry-point lint over ROOT/src (5.4.1, 5.4.9, 6.2)
  sentinel schemas [--check] [--out DIR]      write or check the generated JSON Schemas (L-XREF-09)
  sentinel taxonomy --check                   check config/taxonomy.yaml against the spec (L-XREF-08)
  sentinel demo <scenario|all> [--out DIR] [--today YYYY-MM-DD] [--reviewer simulated|live]
                                              a simulated end-to-end run on config/example (sentinel.demo)
  sentinel run <config-dir> --scenario NAME [--out DIR] [--today YYYY-MM-DD] [--reviewer simulated|live]
                                              the same simulated run on another sentinel-config checkout
  sentinel review [--live] <scenario> [--out DIR] [--today YYYY-MM-DD] [--config DIR]
                                              the Reviewer items of a simulated PR run; with --live they also go
                                              to the real Reviewer, from inside the run and through its egress
                                              gate (pipeline.live; ANTHROPIC_API_KEY needed)
  sentinel stop <rig> [--reason TEXT] [--by NAME] [--root DIR]
                                              trip the kill switch on a rig: write its file flag (6.6); --root for
                                              simulated rigs (orchestrator.killflag)
  sentinel reenable --db PATH {login|rig|quarantine|proof|incident} ...
                                              the human safety actions of 6.6 and 6.12 (sentinel.reenable)
  sentinel calibrate <config-dir> PLAN.json --out DIR [--db PATH]
                                              one Calibration run over the plan on the simulated rigs (3.9, 11.2):
                                              floors and fix rows into DIR/sentinel.db (sentinel.calibration_run)
  sentinel depmap --jdeps FILE --config DIR --build ID --now TIME [--out FILE] [--patch FILE] [--status FILE]
                                              derive the Dependency Map from a bytecode scan and write the proposed
                                              map and its patch (2.10.1, 4.4; sentinel.depscan)
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
    p.add_argument("--reviewer", choices=("simulated", "live"), default="simulated")
    a = p.parse_args(argv)
    if not Path(a.config_dir).is_dir():
        print(f"sentinel run: {a.config_dir} is not a directory", file=sys.stderr)
        return 2
    return demo_main([a.scenario, "--out", a.out, "--today", a.today.isoformat(), "--config", a.config_dir,
                      "--reviewer", a.reviewer])


def _review(argv: list[str]) -> int:
    """`sentinel review [--live] <scenario>`: run the scenario's PR run with the offline Reviewer, keep every
    Reviewer item (3.12, with its 1:1 crops) and list them; with `--live` (and a key), each item is also reviewed by
    the real Reviewer inside the run, through the run's egress gate (6.10: allowlisted and logged to egress_log)."""
    import json
    from dataclasses import asdict
    from datetime import date

    from .demo import DEFAULT_TODAY, EXAMPLE, PR_SCENARIOS
    from .pipeline.live import CollectingReviewer, live_job, load_reviewer_config
    from .pipeline.run import run_pipeline
    from .reviewer import NetworkNotAllowed

    p = argparse.ArgumentParser(prog="sentinel review", description=_review.__doc__)
    p.add_argument("scenario", choices=sorted(PR_SCENARIOS))
    p.add_argument("--live", action="store_true", help="send the items to the real Reviewer (needs ANTHROPIC_API_KEY)")
    p.add_argument("--out", default="out")
    p.add_argument("--today", type=date.fromisoformat, default=DEFAULT_TODAY)
    p.add_argument("--config", default=str(EXAMPLE))
    p.add_argument("--reviewer-config", help="a YAML file whose `reviewer:` block sets the live Reviewer (3.15)")
    a = p.parse_args(argv)
    settings = load_reviewer_config(a.reviewer_config)
    out = Path(a.out) / f"review-{a.scenario}"
    factory, refused = None, None
    if a.live:
        try:
            live_job(config=settings)                   # the key and environment checks; this job sends nothing
        except NetworkNotAllowed as exc:
            refused = exc
        else:
            factory = lambda gate: live_job(config=settings, gate=gate)     # noqa: E731 - the run's gated job
    rev = CollectingReviewer()
    run_pipeline(a.config, PR_SCENARIOS[a.scenario](a.today), out, a.today, reviewer=rev, live_factory=factory)
    listing = [{"queue_key": i.queue_key, "task": i.task, "checkpoints": list(i.checkpoint_ids), "region": i.region,
                "crop_sizes": i.crop_sizes()} for i in rev.items]
    (out / "review-items.json").write_text(json.dumps(listing, indent=1) + "\n")
    print(f"{len(rev.items)} Reviewer item(s) -> {out / 'review-items.json'}")
    if not a.live:
        return 0
    if refused is not None:
        print(f"sentinel review --live: {refused}", file=sys.stderr)
        return 2
    verdicts = rev.verdicts
    (out / "review-live.json").write_text(json.dumps({k: asdict(v) for k, v in verdicts.items()}, indent=1,
                                                     default=str) + "\n")
    for k, v in verdicts.items():
        print(f"{k}: {v.state}" + (f" {v.answer['verdict']} {v.answer['confidence']}" if v.answer else
                                   f" ({v.cause})"))
    return 0


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
    if cmd == "review":
        return _review(rest)
    if cmd == "stop":
        from .orchestrator.killflag import main as stop_main
        return stop_main(rest)
    if cmd == "reenable":
        from .reenable import main as reenable_main
        return reenable_main(rest)
    if cmd == "calibrate":
        from .calibration_run import main as calibrate_main
        return calibrate_main(rest)
    if cmd == "depmap":
        from .depscan import main as depmap_main
        return depmap_main(rest)
    print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
