"""The example's Position Statement census visits (4.8, 3.9, 10.6): their regions files declare the live columns where
the simulated app draws them, so calibration is complete and the Nightly A/A pass stays below its global trip."""
from __future__ import annotations

from datetime import datetime, timezone

from sentinel import aa_pass
from sentinel.demo import BASE_ID, EXAMPLE
from sentinel.diff.regions import park_rect
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.run import _park, _Run, planned_for
from sentinel.pipeline.sim import SimBuild

PS_CENSUS = [f"census/monitor.activity.position-statement/ws.tier-{w}" for w in (
    "a.active-trader", "a.futures-trader", "a.swing-trader", "b.active-dense", "b.dense-charts", "b.futures-detached",
    "b.options-sidebar")]


def test_the_position_statement_censuses_calibrate_complete_and_pass_the_aa(tmp_path):
    """3.9: every Position Statement census visit declares its live mark and P/L columns where the simulated app draws
    them (rows from dy 38), so the A/A calibration opens no `_rest` fix item, the calibration file is complete (one
    open item would leave every region of every checkpoint uncalibrated) and the Nightly A/A pass (10.6) stays below
    the global trip."""
    cfg = SentinelConfig.load(EXAMPLE)
    b = SimBuild(BASE_ID)
    run = _Run(cfg, "run-2026-11-10-nightly-01", "nightly", b, b, datetime(2026, 11, 10, 8, 12, tzinfo=timezone.utc),
               None, None, lambda s: "undeclared")
    cps = [planned_for(cfg, cid) for cid in PS_CENSUS]
    run.calibrate(cps, tmp_path)
    assert run.calibration.complete and run.calibration.fix_list() == []

    def pair(cp):
        a, c = run.capture(cp, "base", tick=20), run.capture(cp, "base", tick=21)
        red = run.redact(cp, a, c, a.screen.image, c.screen.image, count=False)
        return aa_pass.PassPair(red.base, red.candidate, run.registry(cp, a), red.boxes, [park_rect(_park(a))],
                                frames=(a.models, c.models), rules=run.surface_rules(cp))
    res = aa_pass.run(cps, pair, run_id=run.run_id, calibration=run.calibration, watchdog_event=lambda c: "wd-1",
                      checker_config=run.checker(cps[0]))
    assert not res.invalidate and res.queue_calibration is None
    assert {(r.outcome, r.reason) for r in res.rows} <= {("identical", None), ("noise", "below_floor")}
