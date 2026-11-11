"""The runtime demo scenarios (sentinel.demo_ops): each one's outcome and the lines a tester checks (TESTING.md)."""
from __future__ import annotations

import json

import pytest

from sentinel.demo import EXAMPLE, INDEX, SCENARIOS, run_demo
from sentinel.demo_ops import DEMOS
from sentinel.pipeline.run import DEFAULT_TODAY
from sentinel.store import Store


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    return tmp_path_factory.mktemp("demo-ops")


def summary(out, name):
    return (out / name / "summary.txt").read_text()


def test_every_runtime_scenario_is_registered_with_its_index_line():
    assert set(DEMOS) <= set(SCENARIOS) and set(DEMOS) <= set(INDEX)


def test_rotation_hold(out):
    (r,) = run_demo("rotation-hold", out, DEFAULT_TODAY, EXAMPLE)
    assert (r.status, r.commit_status["state"]) == ("complete", "success")
    assert "- Rotation hold: run-2026-11-10-pr-4823-01/1: run held at Tue 2026-11-10 10:31 EST" in summary(out, r.out_dir.name)


def test_maintenance_reschedule(out):
    (r,) = run_demo("maintenance-reschedule", out, DEFAULT_TODAY, EXAMPLE)
    text = summary(out, "maintenance-reschedule")
    assert r.status == "complete" and text.count("run rescheduled once to Tue 2026-11-10 11:15 EST") == 4
    assert text.count(": 32 of 32 items passed\n") == 4               # the rescheduled attempt needs no retry


def test_lost_rig(out):
    (r,) = run_demo("lost-rig", out, DEFAULT_TODAY, EXAMPLE)
    text = summary(out, "lost-rig")
    assert (r.status, r.commit_status["state"]) == ("complete", "success")
    assert "- run-2026-11-10-pr-4823-01/3: rig-06, attempt 2, imported" in text
    assert "- Lost rig: run-2026-11-10-pr-4823-01/3: rig-03 lost power after 1 checkpoints" in text
    with Store(out / "lost-rig" / "sentinel.db") as s:            # one outcome per checkpoint, none lost
        assert not s.missing_outcomes(r.run_id)
    assert any(a["rule"] == "harness_error" for a in json.loads((out / "lost-rig" / "alerts.json").read_text()))


def test_nightly_maintenance(out):
    (n,) = run_demo("nightly-maintenance", out, DEFAULT_TODAY, EXAMPLE)
    text = summary(out, "nightly-maintenance")
    assert n.status == "complete" and "- Backup ok at " in text and "- Retention swept 0 expired copies" in text
    assert "- Artifact audit sample: " in text
    assert any(a["rule"] == "store_health" for a in n.alerts)


def test_stale_authorization(out):
    (r,) = run_demo("stale-authorization", out, DEFAULT_TODAY, EXAMPLE)
    assert (r.status, r.reason, r.commit_status["state"]) == ("aborted_harness", "config_invalid", "error")
    assert any(e.startswith("L-XREF-20") for e in r.lint_errors)


def test_pool_saturated(out):
    (r,) = run_demo("pool-saturated", out, DEFAULT_TODAY, EXAMPLE)
    assert (r.status, r.reason) == ("aborted_budget", "pool_saturated")
    assert "P2 pool_saturated" in summary(out, "pool-saturated")
    assert (out / "pool-saturated" / "state" / "queue.jsonl").read_text().count("\n") == 5
