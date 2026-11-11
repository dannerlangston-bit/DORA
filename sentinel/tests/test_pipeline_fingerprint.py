"""The 4.12 fingerprint wired into the simulated PR run (w2 wiring item 3): built from the simulated rig's reads
(`SimulatedRig.read_os`, `read_app`), stored once per shard and side, checked against the rig specification and the
other half before the second half's first capture, re-read at every flow boundary and after every relaunch; every
capture and outcome carries its id, the floors its calibration key, and a render-probe mismatch the L-FPR-05 note."""
from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from sentinel.canonical import calibration_filename, calibration_key
from sentinel.demo import EXAMPLE, wrong_number_scenario
from sentinel.fingerprint import RASTERIZATION_NOTE
from sentinel.orchestrator.simrig import SimScenario, SimulatedRig
from sentinel.pipeline.orchestration import RigScript
from sentinel.pipeline.run import run_pipeline
from sentinel.store import Store

TODAY = date(2026, 11, 10)


def _run(tmp_path, **script):
    sc = dataclasses.replace(wrong_number_scenario(TODAY), rig=RigScript(**script))
    return run_pipeline(EXAMPLE, sc, tmp_path)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    out = tmp_path_factory.mktemp("fp-clean")
    return out, _run(out)


def test_each_shard_stores_both_halves_and_every_record_carries_the_candidates_id(clean):
    out, r = clean
    with Store(out / "sentinel.db") as store:
        shards = [s["shard_id"] for s in r.shards if s["checkpoints"]]
        fps = {(sh, side): store.fingerprint(sh, side) for sh in shards for side in ("candidate", "base")}
    assert all(fps.values())
    cand = {sh: fps[(sh, "candidate")] for sh in shards}
    assert {o.fingerprint_id for o in r.outcomes} == {f.fingerprint_id for f in cand.values()}
    base = fps[(shards[0], "base")]
    assert base.match["build_id"] != cand[shards[0]].match["build_id"]       # the pair differs only in build id
    assert base.fingerprint_id == cand[shards[0]].fingerprint_id


def test_the_floors_are_keyed_by_the_calibration_key(clean):
    out, r = clean
    with Store(out / "sentinel.db") as store:
        fp = store.fingerprint(next(s["shard_id"] for s in r.shards if s["checkpoints"]), "candidate")
    assert (out / "calibration" / calibration_filename(calibration_key(fp.match))).is_file()


def test_a_base_half_rendered_by_another_rasterizer_is_not_comparable_with_the_l_fpr_05_note(tmp_path):
    r = _run(tmp_path, base_fingerprint=(("render_probe_hash", "0badc0ffee000000"),))
    assert {(o.outcome, o.reason) for o in r.outcomes} == {("not_comparable", "fingerprint_mismatch")}
    assert {o.detail for o in r.outcomes} == {"field: render_probe_hash"}
    assert r.status == "complete_with_env_errors"
    text = (tmp_path / "pr-comment.md").read_text()
    assert RASTERIZATION_NOTE in text


def test_an_unreadable_os_field_is_env_error_at_provisioned(tmp_path):
    r = _run(tmp_path, fp_unreadable="cleartype")
    assert {(o.outcome, o.reason, o.gate) for o in r.outcomes} == {
        ("env_error", "fingerprint_unreadable:cleartype", "provisioned")}


def test_a_cleartype_change_mid_run_makes_the_rest_of_the_shard_not_comparable(tmp_path):
    r = _run(tmp_path, fp_change=("cleartype", 4))     # G5, provisioned, attested, then the first boundary
    moved = [o for o in r.outcomes if o.reason == "fingerprint_mismatch"]
    assert moved and {o.detail for o in moved} == {"field: cleartype"}
    assert any(o.outcome == "identical" for o in r.outcomes)            # what the first units captured stands
    assert r.status == "complete_with_env_errors"


def test_the_simulated_reads_meet_the_rig_specification():
    rig = SimulatedRig(SimScenario())
    rig.launch()
    assert rig.read_app().build_id_title == rig.read_title_build_id()
    assert rig.read_os().display["awareness"] == "PER_MONITOR_AWARE_V2"
    later = SimulatedRig(SimScenario(fp_change=("cleartype", 1)))
    assert later.read_os().cleartype["gamma"] == 1800 and later.read_os().cleartype["gamma"] == 2200
