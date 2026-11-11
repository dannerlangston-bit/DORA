"""The runtime scenarios of `sentinel demo` (w4 and w5a wiring): the operations around a run rather than what it
compares. Each runs PR 4823's clean change (`demo.clean_scenario`) or a Nightly run on the example config with one
operational fact scripted, and writes the usual `summary.txt` with what a tester should see:

- `rotation-hold`: the rig-01 login is inside its password rotation window (ops/credential_rotation.yaml); its shard
  is held until the window ends and run health says so (7.7).
- `maintenance-reschedule`: the login host is down for a planned window (schedule/windows.yaml); preflight N1 fails
  inside it, so the run is rescheduled once to the window end plus 15 minutes instead of env_error (7.11).
- `lost-rig`: rig-03 loses power after its first checkpoint; its shard is re-queued in full to the spare, rig-06, as
  attempt 2, and the run completes (10.7, 8.9).
- `nightly-maintenance`: a Nightly run with the night's store upkeep: backup, retention, the weekly artifact audit and
  the store health check, with the Store health alert for a restore drill never run (8.6, 8.9, 9.10).
- `stale-authorization`: the authorization document was edited after it was signed; its recorded sha256 no longer
  matches, so preflight G1 refuses the run, config_invalid (6.12, L-XREF-20).
- `pool-saturated`: every pool rig is out of service when PR 4823's webhook arrives; the run queue holds the run and at
  its 45-minute start deadline the Reporter writes it aborted_budget (pool_saturated) without it ever starting (11.2).
"""
from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path
from typing import Any

from .orchestrator import queue as q
from .orchestrator.clock import ManualClock
from .pipeline.config import SentinelConfig, parse_time
from .pipeline.orchestration import RigScript
from .pipeline.queue_driver import QueueDriver
from .pipeline.run import NightlyScenario, RunResult, run_nightly, run_pipeline
from .pipeline.sim import SimBuild
from .store import Store

ROTATION_WINDOW = ('window: {start: "2026-10-17T22:00:00-04:00", end: "2026-10-17T23:30:00-04:00"}',
                   'window: {start: "{day}T10:00:00-05:00", end: "{day}T11:00:00-05:00"}')
MAINTENANCE = '  - {weekday: Sat, start: "22:00", end: "02:00", source: "ops calendar 2026-10"}'


def _demo():
    from . import demo                                   # demo imports this module for its registry
    return demo


def _rows(r: RunResult, labels: tuple[str, ...]) -> list[str]:
    """The run-health rows of `labels`, as the summary prints them."""
    with Store(r.out_dir / "sentinel.db") as s:
        rows = s.run_summary(r.run_id).get("health_rows") or ()
    return [f"- {label}: {text}" for label, text in rows if label in labels]


def demo_rotation_hold(out: Path, today: date, config: Path, reviewer: Any = None) -> list[RunResult]:
    d = _demo()
    sc = d.clean_scenario(today)
    rot = (Path(config) / "ops" / "credential_rotation.yaml").read_text(encoding="utf-8").replace(
        ROTATION_WINDOW[0], ROTATION_WINDOW[1].replace("{day}", today.isoformat()))
    r = run_pipeline(config, dataclasses.replace(sc, overlay={**sc.overlay, "ops/credential_rotation.yaml": rot}), out,
                     today, **d._rev(reviewer))
    story = ["PR 4823's clean change while rig-01's login is inside its password rotation window, 10:00 to 11:00 New",
             "York (ops/credential_rotation.yaml). The run is due at 10:31: the shard on rig-01 is held until the",
             "window ends, then runs; the other rigs' logins are not rotating and run at once (7.7).",
             "", "- Run health has a Rotation hold row naming the login, the owner and the window.",
             "- The run is complete and the check passes, as in clean-pr."]
    d._write_summary(out, "Scenario rotation-hold: a run held in its login's rotation window", story, [r],
                     _rows(r, ("Rotation hold",)))
    return [r]


def demo_maintenance_reschedule(out: Path, today: date, config: Path, reviewer: Any = None) -> list[RunResult]:
    d = _demo()
    sc = d.clean_scenario(today)
    day = today.strftime("%a")
    win = (Path(config) / "schedule" / "windows.yaml").read_text(encoding="utf-8").replace(
        MAINTENANCE, MAINTENANCE + f'\n  - {{weekday: {day}, start: "10:00", end: "11:00", source: "login host '
                                   f'upgrade {today.isoformat()}"}}')
    sc = dataclasses.replace(sc, overlay={**sc.overlay, "schedule/windows.yaml": win},
                             rig=RigScript(preflight_down=(("N1", 30.0),)))
    r = run_pipeline(config, sc, out, today, **d._rev(reviewer))
    story = ["PR 4823's clean change at 10:31 while the login host is down for a planned upgrade, 10:00 to 11:00",
             "(a maintenance window in schedule/windows.yaml). Preflight N1 cannot reach the login host, fails again",
             "60 s later, and because every failed item is a network item inside a maintenance window the run is",
             "rescheduled once, to 11:15, instead of ending env_error (7.11, 12.7). At 11:15 every item passes.",
             "", "- Run health has a Maintenance window row per shard with the failure time and the new start.",
             "- The Preflight rows say all 32 items passed on the rescheduled attempt.",
             "- The run is complete and the check passes."]
    d._write_summary(out, "Scenario maintenance-reschedule: preflight N1 inside a maintenance window", story, [r],
                     _rows(r, ("Maintenance window", "Preflight")))
    return [r]


def demo_lost_rig(out: Path, today: date, config: Path, reviewer: Any = None) -> list[RunResult]:
    d = _demo()
    sc = dataclasses.replace(d.clean_scenario(today), rig=RigScript(power_off_at=("rig-03", 1)))
    r = run_pipeline(config, sc, out, today, **d._rev(reviewer))
    with Store(out / "sentinel.db") as s:
        shards = [f"- {x['shard_id']}: {x['rig_id']}, attempt {x['attempt']}, {x['state']}" for x in s.query(
            "SELECT shard_id, rig_id, attempt, state FROM shards WHERE run_id = ? ORDER BY shard_id", (r.run_id,))]
    story = ["PR 4823's clean change; rig-03 loses power after its shard captured one checkpoint. Its heartbeat stops,",
             "the shard is marked lost and re-queued once, in full, to a compatible rig: every pool rig is busy, so",
             "the spare, rig-06, takes it as attempt 2 (10.7, 8.9). The lost attempt's captures are superseded: they",
             "stay on rig-03 and never count.",
             "", "- Run health has Lost rig and Re-queued rows; the shard table below shows rig-06, attempt 2.",
             "- The run is complete and the check passes: every checkpoint has exactly one outcome.",
             "- alerts.json has one harness_error alert for the lost rig (9.10)."]
    d._write_summary(out, "Scenario lost-rig: a rig lost mid-run is re-queued to the spare", story, [r],
                     _rows(r, ("Lost rig", "Re-queued")) + ["", "Shards:"] + shards)
    return [r]


def demo_nightly_maintenance(out: Path, today: date, config: Path, reviewer: Any = None) -> list[RunResult]:
    d = _demo()
    n = run_nightly(config, NightlyScenario("nightly-maintenance", f"run-{today.isoformat()}-nightly-01",
                                            SimBuild(d.BASE_ID), overlay=dict([d._main_build()]),
                                            mutants=("M03", "M09")), out, today)
    lines = n.canary["run_health"].get("store") or []
    story = ["A Nightly run (two canary mutants) and the night's store maintenance on the control host (8.6): the",
             "backup (VACUUM INTO a snapshot, CSV exports, the pinned objects mirrored), the retention sweep",
             "(nothing has expired in a fresh store), the weekly artifact audit (every pinned copy and a 5 percent",
             "sample re-hashed) and the store state check (8.9, 9.10).",
             "", "- The lines below: backup ok, retention swept 0, the audit's sample size.",
             "- No restore drill has ever been recorded, so the state check raises the Store health alert (P2) and",
             "  alerts.json has it; a restore drill (`sentinel restore-drill`) clears it."]
    d._write_summary(out, "Scenario nightly-maintenance: backup, retention, audit and the store check", story, [n],
                     ["Store maintenance:"] + [f"- {x}" for x in lines])
    return [n]


def demo_stale_authorization(out: Path, today: date, config: Path, reviewer: Any = None) -> list[RunResult]:
    d = _demo()
    sc = d.clean_scenario(today)
    doc = (Path(config) / "authorization" / "v1.md").read_text(encoding="utf-8") + \
        "\nAddendum: one more account, added after the signatures.\n"
    r = run_pipeline(config, dataclasses.replace(sc, overlay={**sc.overlay, "authorization/v1.md": doc}), out, today,
                     **d._rev(reviewer))
    story = ["PR 4823's clean change on a config whose authorization document (authorization/v1.md) was edited after",
             "its three signatures: the record's document_sha256 no longer matches the document. Preflight G1 refuses",
             "the run (6.12, L-XREF-20) and it ends aborted_harness (reason: config_invalid) before any product action;",
             "the run row carries no authorization hash.",
             "", "- The PR comment's abort block names the rule; the check is error; alerts.json pages at P1."]
    d._write_summary(out, "Scenario stale-authorization: a run refused for a stale authorization hash", story, [r],
                     d._safety_lines(r), abort_expected=True)
    return [r]


def demo_pool_saturated(out: Path, today: date, config: Path, reviewer: Any = None) -> list[RunResult]:
    d = _demo()
    sc = d.clean_scenario(today)
    cfg = SentinelConfig.load(config)
    trigger = parse_time(f"{today.isoformat()}T{sc.clock}-05:00")
    clock = ManualClock(start=trigger)
    results: list[RunResult] = []
    with Store(out / "sentinel.db") as store:
        drv = QueueDriver(store, clock, cfg_root=Path(config), windows=cfg.windows,
                          state_path=out / "state" / "queue.jsonl", run_ids=lambda _t: sc.run_id,
                          start=lambda req, rigs: "not started")
        for rig in (1, 2, 3, 4):
            drv.submit(q.RigState(rig, up=False))
        drv.submit(q.PrBuild(4823, sc.candidate.build_id, trigger))
        drv.advance(46 * 60)
        out_row = store.run_outcome(sc.run_id)
        comment, _check = drv.deadline_comments.get(sc.run_id, (None, None))
        alerts = [f"- {a.priority} {a.rule}: {a.detail}" for a in drv.alerts]
        queued = [f"- {o.at:%H:%M} run_queued position {o.position}, depth {o.depth}" for o in drv.log
                  if isinstance(o, q.RunQueued)][:3]
    results.append(RunResult(sc.run_id, "pr", out_row.status, out_row.reason, out, comment=comment or ""))
    (out / "pr-comment.md").write_text((comment or "") + "\n", encoding="utf-8")
    story = ["PR 4823's webhook arrives at 10:31 while rigs 1 to 4 are all out of service. The run queue holds the",
             "run, sends run_queued every minute and the 20-minute pool saturated alert, and at the 45-minute start",
             "deadline the Reporter writes the run aborted_budget (reason: pool_saturated) with how many runs were",
             "ahead: the run never started, so nothing was captured (11.2).",
             "", "- The run row is aborted_budget (pool_saturated); pr-comment.md has the comment the PR gets.",
             "- The queue journal in state/queue.jsonl survives a control-host restart (it is replayed)."]
    d._write_summary(out, "Scenario pool-saturated: a run that never starts reaches its start deadline", story,
                     results, ["Queue:"] + queued + ["", "Queue alerts:"] + alerts)
    return results


DEMOS = {"rotation-hold": demo_rotation_hold, "maintenance-reschedule": demo_maintenance_reschedule,
         "lost-rig": demo_lost_rig, "nightly-maintenance": demo_nightly_maintenance,
         "stale-authorization": demo_stale_authorization, "pool-saturated": demo_pool_saturated}

INDEX = {
    "rotation-hold": ("A run whose rig-01 login is inside its rotation window (7.7).",
                      "run health has a Rotation hold row for the rig-01 shard and the run is complete."),
    "maintenance-reschedule": ("Preflight N1 fails inside a planned maintenance window (7.11).",
                               "each shard's Maintenance window row says rescheduled once to 11:15, all 32 items "
                               "then pass and the run is complete."),
    "lost-rig": ("A rig loses power mid-run and its shard is re-queued to the spare (10.7).",
                 "the shard table in summary.txt shows rig-06 attempt 2 and the run is complete."),
    "nightly-maintenance": ("The Nightly run's store upkeep: backup, retention, audit, store check (8.6).",
                            "summary.txt lists backup ok, retention swept 0 and the audit sample, and alerts.json "
                            "has the Store health alert for the missing restore drill."),
    "stale-authorization": ("An authorization document edited after signing (6.12).",
                            "the run is aborted_harness (config_invalid) and the abort block names L-XREF-20."),
    "pool-saturated": ("A PR run that never starts because the pool is out of service (11.2).",
                       "the run is aborted_budget (pool_saturated) at its 45-minute start deadline."),
}
