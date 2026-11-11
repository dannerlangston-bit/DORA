"""The Calibration run (1.6, 3.1, 3.9 steps 1 and 2, 5.15, 7.13, 11.2): `calibration_run`."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
import yaml

from sentinel import synth
from sentinel.calibration_run import (
    CalibrationItem, CalibrationPlan, CalibrationShard, SessionReport, Shot, items_for, main, owner_of,
    run_calibration,
)
from sentinel.canonical import calibration_filename, calibration_key
from sentinel.diff.calibration import StoreCalibration, checkpoint_versions, has_calibration, lookup, open_fixes
from sentinel.diff.engine import DiffContext, diff_pair
from sentinel.diff.regions import UNCALIBRATED, Landmark, Rect, Region, RegionsFile
from sentinel.pipeline.config import SentinelConfig
from sentinel.records import Fingerprint
from sentinel.scope import PlannedCheckpoint
from sentinel.store.store import Store

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "calibration.schema.json").read_text())
BUILD = "main-1998.2.0-4f1c9a2"
RUN = "cal-20261113"
START = datetime(2026, 11, 14, 3, 15, tzinfo=timezone.utc)           # Friday 22:15 New York
NIGHT2 = datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)           # Saturday night
END = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)             # Sunday 17:00
SIZE = (320, 180)
LAYOUT = {"type": "window", "name": "w", "children": [copy.deepcopy(synth.node(synth.sample_layout(0), "watchlist"))]}
LAYOUT["children"][0]["rect"] = [6, 6, 306, 168]
BASE = synth.render(LAYOUT, SIZE)
GX, GY, GW, GH = BASE.get("watchlist.grid").bbox
MATCH = {
    "build_id": BUILD, "jre_release": {"sha256": "sha256:" + "1" * 64, "java_version": "17.0.12"},
    "vmoptions_hash": "sha256:" + "2" * 64, "java2d_pipeline": {"pinned": "software", "confirmed": "software"},
    "windows_build": {"current_build": "26100", "ubr": "2314"}, "display": {"width": 1920, "height": 1080, "dpi": 96},
    "cleartype": {"smoothing": True}, "gpu": {"name": "Hyper-V Video", "driver_version": "10.0"},
    "os_theme": {"apps_light": False}, "app_theme": {"scheme": "dark", "font_size": "medium"},
    "data_epoch": {"epoch_id": "sha256:" + "5" * 64},
    "harness": {"capture_lib": "mss-9.0.1", "settle_params_sha256": "sha256:" + "6" * 64,
                "diff_engine_params_sha256": "sha256:" + "7" * 64},
    "render_probe_hash": "e3b0c44298fc1c14",
}
KEY = calibration_key(MATCH)


def cp(surface, ws, persona, mode="frozen", tier=2):
    return PlannedCheckpoint(f"census/{surface}/{ws}", surface, "census", tier, ws, persona, mode)


CHART = cp("charts.flexible-grid.chart-cell", "ws.tier-a.swing-trader", "swing_trader")
LEVEL2 = cp("charts.flexible-grid.level2-cell", "ws.tier-a.active-trader", "active_trader")
POSITIONS = cp("monitor.activity.position-statement", "ws.tier-a.active-trader", "active_trader", "masked_live", 1)


def plan(*shards, run_id=RUN, night2=NIGHT2):
    return CalibrationPlan(run_id, BUILD, tuple(CalibrationShard(rig, tuple(CalibrationItem(c, f"owner-{rig}")
                                                                            for c in cps)) for rig, cps in shards),
                           START, END, night2)


def registry(cid):
    lx, ly, _, _ = BASE.get("watchlist.title.label").bbox
    lm = Landmark("lm.watchlist", BASE.crop("watchlist.title.label", 2), expected=(lx - 2, ly - 2))
    grid = Region("rgn.watchlist.grid", "diff", "lm.watchlist", Rect(GX - lx + 2, GY - ly + 2, GW, GH))
    return RegionsFile(cid, [lm], [grid])


@dataclass
class FakeRigs:
    """A `RigFactory` over synth frames. `noisy`: checkpoints whose grid gets a few changed pixels per capture;
    `stop`: (rig, night) sessions that stop at fixture_verified; `other_key`: (rig, night) sessions on another
    rendering key. `calls` records (rig, night, shard id, checkpoint, capture index)."""

    noisy: frozenset = frozenset()
    stop: frozenset = frozenset()
    other_key: frozenset = frozenset()
    settle_ms: int = 900
    calls: list = field(default_factory=list)

    def __call__(self, shard, night, shard_id):
        return FakeSession(self, shard, night, shard_id)


@dataclass
class FakeSession:
    rigs: FakeRigs
    shard: CalibrationShard
    night: int
    shard_id: str

    def run(self, work):
        if (self.shard.rig_id, self.night) in self.rigs.stop:
            return SessionReport("complete_with_env_errors", "fixture_verified", stopped=("env_error", "fixture_drift"))
        match = MATCH if (self.shard.rig_id, self.night) not in self.rigs.other_key else \
            {**MATCH, "gpu": {"name": "NVIDIA T4", "driver_version": "551.61"}}
        fp = Fingerprint(run_id=RUN if self.shard_id.startswith(RUN) else self.shard_id.split("/")[0],
                         side="candidate", rig_id=self.shard.rig_id, read_at=START.isoformat(), match=match,
                         shard_id=self.shard_id)
        work(self)
        return SessionReport("complete", "teardown", fingerprint=fp, redaction_version="redaction-1-aaa",
                             started_at=START if self.night == 1 else NIGHT2)

    def capture(self, item, index):
        cid = item.checkpoint.checkpoint_id
        self.rigs.calls.append((self.shard.rig_id, self.night, self.shard_id, cid, index))
        img = BASE.image
        if cid in self.rigs.noisy:
            img = synth.perturb(BASE.image, index, count=3, amplitude=90, rect=(GX + 10, GY + 40, 150, 50))
        return Shot(img.copy(), time_to_settle_ms=self.rigs.settle_ms + 100 * index, park=(SIZE[0] - 8, SIZE[1] - 8))

    def registry(self, item):
        return registry(item.checkpoint.checkpoint_id)


@pytest.fixture(scope="module")
def cfg():
    return SentinelConfig.load(EXAMPLE)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "sentinel.db")
    for n, rig in enumerate(("rig-01", "rig-02"), 1):
        s.register_rig(rig, login_alias=f"paper-0{n}", owner="qa", snapshot_id=f"snap-{rig}", allowlist_entry=n)
    s.register_build(BUILD, channel="main", source_commit="4f1c9a2", pipeline_artifact_url="sim://b",
                     installer_sha256="c" * 64)
    yield s
    s.close()


# -- the plan ---------------------------------------------------------------------------------------------------

def test_the_plan_keeps_each_checkpoint_on_one_rig_and_r6_on_the_following_night(cfg):
    p = plan(("rig-01", [CHART]), ("rig-02", [LEVEL2, POSITIONS]))
    assert CalibrationPlan.from_dict(json.loads(json.dumps(p.to_dict()))) == p
    with pytest.raises(ValueError, match="one rig"):
        plan(("rig-01", [CHART]), ("rig-02", [CHART]))                  # 3.9: a checkpoint's captures on one rig
    with pytest.raises(ValueError, match="one shard per rig"):
        plan(("rig-01", [CHART]), ("rig-01", [LEVEL2]))
    with pytest.raises(ValueError, match="following night"):
        plan(("rig-01", [CHART]), night2=START + timedelta(hours=1))     # still Friday night in New York
    with pytest.raises(ValueError, match="inside the window"):
        plan(("rig-01", [CHART]), night2=END + timedelta(hours=1))
    assert owner_of(cfg, CHART) == "tos-charts-ui"
    flow = PlannedCheckpoint("flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07",
                             "dialog.order-confirmation", "flow", 1, "ws.tier-a.swing-trader", "swing_trader",
                             "frozen", "flow.trade.order-ticket.stock.limit", "s07")
    assert [i.owner for i in items_for(cfg, [flow])] == ["tos-trade-ui"]


# -- the run ----------------------------------------------------------------------------------------------------

def test_a_calibration_run_writes_floors_for_every_checkpoint_and_fix_rows_for_non_zero_floors(cfg, store, tmp_path):
    """1.6, 3.9: five captures in one session and the sixth the next night on the same rig; the run row, shards and
    fingerprints are recorded; floors for every checkpoint, a fix row for the noisy one routed to its owner."""
    rigs = FakeRigs(noisy=frozenset({LEVEL2.checkpoint_id}))
    p = plan(("rig-01", [CHART]), ("rig-02", [LEVEL2]))
    res = run_calibration(cfg, rigs, store, p, out_dir=tmp_path)
    for c in (CHART, LEVEL2):
        mine = [x for x in rigs.calls if x[3] == c.checkpoint_id]
        assert [x[4] for x in mine] == [0, 1, 2, 3, 4, 5] and len({x[0] for x in mine}) == 1
        assert {x[2] for x in mine if x[1] == 1} != {x[2] for x in mine if x[1] == 2}     # r6 in a fresh session
    run = store.query("SELECT run_type, outcome, scope_count FROM runs WHERE run_id = ?", (RUN,))[0]
    assert tuple(run) == ("calibration", "complete", 0) and store.outcomes(RUN) == []    # statistics only (3.1)
    assert len(store.query("SELECT 1 FROM shards WHERE run_id = ? AND state = 'imported'", (RUN,))) == 4
    assert len(store.query("SELECT 1 FROM env_fingerprints WHERE run_id = ?", (RUN,))) == 4
    assert set(lookup(store, KEY, CHART.checkpoint_id)) == {"rgn.watchlist.grid", "_rest", "*"}
    assert lookup(store, KEY, LEVEL2.checkpoint_id)["rgn.watchlist.grid"].floors.changed_px == 6
    assert lookup(store, KEY, CHART.checkpoint_id)["_rest"].settle_p50_ms == 1150      # the measured p50 (3.5)
    [item] = open_fixes(store, KEY)
    assert (item.checkpoint_id, item.region_id, item.owner) == (LEVEL2.checkpoint_id, "rgn.watchlist.grid", "owner-rig-02")
    assert res.outcome.status == "complete" and res.keys == {CHART.checkpoint_id: KEY, LEVEL2.checkpoint_id: KEY}
    assert list(res.summary["fix_routes"]) == ["owner-rig-02"] and len(res.summary["fix_list"]) == 1
    [path] = res.files
    assert path == tmp_path / "calibration" / calibration_filename(KEY)
    jsonschema.validate(yaml.safe_load(path.read_text()), SCHEMA)
    summary = json.loads(store.query("SELECT summary_json FROM runs WHERE run_id = ?", (RUN,))[0][0])
    assert summary["calibration"]["checkpoints"] == 2


def test_a_following_pr_run_reads_the_stored_floors_and_a_new_fingerprint_queues_a_calibration(cfg, store):
    """01: the next PR run on the fingerprint uses the stored thresholds; on a new rendering key every region is
    uncalibrated (needs_human) and a Calibration run is queued (7.13, 12.7 G5)."""
    run_calibration(cfg, FakeRigs(), store, plan(("rig-01", [CHART]), ("rig-02", [LEVEL2])))
    reg = registry(CHART.checkpoint_id)
    sc = StoreCalibration(store, KEY, live={CHART.checkpoint_id: checkpoint_versions(reg)})
    res = diff_pair(BASE.image, BASE.image.copy(), reg, calibration=sc, context=DiffContext(checkpoint_id=CHART.checkpoint_id))
    assert sc.complete and res.threshold_source == "calibrated" and sc.request() is None
    other = calibration_key({**MATCH, "gpu": {"name": "NVIDIA T4", "driver_version": "551.61"}})
    assert has_calibration(store, KEY) and not has_calibration(store, other)        # G5 calibration_record
    fresh = StoreCalibration(store, other)
    changed = synth.perturb(BASE.image, 1, count=8, amplitude=90, rect=(GX + 10, GY + 40, 150, 50))
    res = diff_pair(BASE.image, changed, reg, calibration=fresh, context=DiffContext(checkpoint_id=CHART.checkpoint_id))
    assert res.threshold_source == "uncalibrated" and fresh.lookup(CHART.checkpoint_id, "_rest") == (UNCALIBRATED,
                                                                                                    "uncalibrated")
    assert fresh.request().to_dict() == {"calibration_key": other, "requested_by": None,
                                         "checkpoints": [CHART.checkpoint_id], "regions": 3,
                                         "reasons": {"no_calibration": 3}}


def test_failed_sessions_and_a_changed_key_are_fix_items_never_dropped(cfg, store):
    """3.9 step 2: a session stopped at a gate gives its captures the gate's outcome; a second night on another
    rendering key gives r6 env_error (rig_spec_mismatch); the run ends with the worst shard (8.2)."""
    rigs = FakeRigs(stop=frozenset({("rig-02", 1)}), other_key=frozenset({("rig-01", 2)}))
    res = run_calibration(cfg, rigs, store, plan(("rig-01", [CHART]), ("rig-02", [LEVEL2])))
    chart = res.calibrations[CHART.checkpoint_id]
    assert {(i.outcome, i.reason) for i in chart.fix_list} == {("env_error", "rig_spec_mismatch")} and chart.failed
    level2 = res.calibrations[LEVEL2.checkpoint_id]
    assert {(i.cause, i.outcome, i.reason) for i in level2.fix_list} == {("settle", "env_error", "fixture_drift")}
    assert res.outcome.status == "complete_with_env_errors"
    assert store.query("SELECT count(*) FROM calibration_floors")[0][0] == 0            # no valid floor anywhere
    rows = store.query("SELECT checkpoint_id, outcome, reason, owner FROM calibration_fixes ORDER BY checkpoint_id")
    assert {tuple(r) for r in rows} == {(CHART.checkpoint_id, "env_error", "rig_spec_mismatch", "owner-rig-01"),
                                        (LEVEL2.checkpoint_id, "env_error", "fixture_drift", "owner-rig-02")}
    assert len(res.summary["fix_list"]) == 2 and res.keys[LEVEL2.checkpoint_id] is None


def test_a_second_night_on_another_key_ends_the_shards_and_the_run_with_env_errors(cfg, store, tmp_path):
    """1.10, 3.9 step 2, 8.2 (review calibration:4): when both night-2 sessions run on another rendering key every
    r6 is env_error (rig_spec_mismatch) and nothing is calibrated, so those shards and the run end
    complete_with_env_errors, not complete, and `sentinel calibrate` exits 1."""
    rigs = FakeRigs(other_key=frozenset({("rig-01", 2), ("rig-02", 2)}))
    res = run_calibration(cfg, rigs, store, plan(("rig-01", [CHART]), ("rig-02", [LEVEL2])))
    assert all(c.failed for c in res.calibrations.values()) and res.summary["calibrated"] == 0
    assert (res.outcome.status, res.outcome.reason) == ("complete_with_env_errors", None)
    shards = store.query("SELECT shard_id, shard_outcome FROM shards WHERE run_id = ? ORDER BY shard_id", (RUN,))
    assert [tuple(r) for r in shards] == [(f"{RUN}/1", "complete"), (f"{RUN}/2", "complete"),
                                          (f"{RUN}/3", "complete_with_env_errors"),
                                          (f"{RUN}/4", "complete_with_env_errors")]
    assert [(s["night"], s["status"]) for s in res.sessions] == [(1, "complete"), (1, "complete"),
                                                                 (2, "complete_with_env_errors"),
                                                                 (2, "complete_with_env_errors")]
    p = plan(("rig-01", [CHART]), ("rig-02", [LEVEL2]), run_id="cal-20261120")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(p.to_dict()))
    assert main([str(EXAMPLE), str(plan_file), "--out", str(tmp_path / "out")], rig_factory=rigs) == 1


# -- sentinel calibrate on the simulated rig --------------------------------------------------------------------

def test_sentinel_calibrate_on_the_simulated_rig(tmp_path):
    """`sentinel calibrate`: the simulated rigs pass the Orchestrator's gates, record their fingerprints and capture
    through settle and redaction; the floors land in the store and calibration/<hex>.yaml, under the key a PR run on
    the simulated rig uses."""
    p = plan(("rig-01", [CHART]), ("rig-02", [POSITIONS]))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(p.to_dict()))
    assert main([str(EXAMPLE), str(plan_file), "--out", str(tmp_path / "out")]) == 0
    [path] = (tmp_path / "out" / "calibration").glob("*.yaml")
    doc = yaml.safe_load(path.read_text())
    jsonschema.validate(doc, SCHEMA)
    assert sorted(c["checkpoint_id"] for c in doc["checkpoints"]) == sorted([CHART.checkpoint_id,
                                                                              POSITIONS.checkpoint_id])
    with Store(tmp_path / "out" / "sentinel.db") as s:
        fps = s.query("SELECT fields_json FROM env_fingerprints WHERE run_id = ?", (RUN,))
        assert len(fps) == 4 and {calibration_key(json.loads(r[0])["match"]) for r in fps} == {doc["calibration_key"]}
        assert s.query("SELECT outcome FROM runs WHERE run_id = ?", (RUN,))[0][0] == "complete"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**p.to_dict(), "build_id": "main-1999.1.0-0000000"}))
    assert main([str(EXAMPLE), str(bad), "--out", str(tmp_path / "out2")]) == 2           # no build record
    assert main([str(EXAMPLE), str(tmp_path / "missing.json"), "--out", str(tmp_path / "out3")]) == 2
