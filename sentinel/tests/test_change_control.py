"""Change control: the reviewer version tuple, the hashes a run records, the canary re-run rule and L-XREF-29
(sentinel.change_control, sentinel.lint.checks.change_control; 3.7, 3.15, 4.16, 8.3)."""
from __future__ import annotations

import dataclasses
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel import change_control as cc
from sentinel.demo import EXAMPLE
from sentinel.diff import load_params, params_sha256
from sentinel.lint.checks import change_control as lint_cc
from sentinel.lint.repo import Repo
from sentinel.lint.rules import lint
from sentinel.reviewer import CropPolicy, ReviewerConfig
from sentinel.reviewer import evals as E
from sentinel.store import RunPlan, ScopeEntry, Store

MODEL = "claude-opus-5-5"
TODAY = date(2026, 11, 10)
CP = "census/monitor.activity.position-statement/ws.tier-a.active-trader"


@pytest.fixture
def pinned(tmp_path) -> Path:
    """The example config with a real model pinned in phase0/branches.yaml."""
    root = tmp_path / "config"
    shutil.copytree(EXAMPLE, root)
    p = root / "phase0" / "branches.yaml"
    p.write_text(p.read_text().replace("simulated-reviewer-offline", MODEL))
    return root


# -- the reviewer version tuple (3.15, 4.16) ------------------------------------------------------------------------

def test_no_tuple_is_in_force_for_the_offline_stand_in_or_an_undecided_dr07(pinned):
    assert cc.tuple_in_force(EXAMPLE) is None
    p = pinned / "phase0" / "branches.yaml"
    text = p.read_text()
    p.write_text(text.replace("computer_use: {branch: A}", "computer_use: {branch: undecided, deadline: 2026-12-01}"))
    assert cc.tuple_in_force(pinned) is None


def test_the_tuple_in_force_pins_the_branches_model_and_the_code_prompt(pinned):
    t = cc.tuple_in_force(pinned)
    assert t is not None and t.model_id == MODEL and t.schema_version == 1 and t.few_shot_ids == ()
    assert t.prompt_sha256.startswith("sha256:") and t.crop_policy_version == "crop-1"
    assert t.redaction_version == cc.stage_redaction_version(pinned)
    assert cc.ReviewerTuple.from_doc(t.doc()) == t and t.hex == t.sha256.removeprefix("sha256:")
    assert t.sha256 == cc.sha256_of(t.doc())


def test_every_element_of_the_tuple_changes_its_hash(pinned):
    t = cc.tuple_in_force(pinned)
    effort = cc.tuple_in_force(pinned, config=ReviewerConfig(effort="max"))
    assert effort.differences(t) == ("prompt_sha256", "effort", "request_settings")
    crop = cc.tuple_in_force(pinned, config=ReviewerConfig(crop=CropPolicy(version="crop-2")))
    assert "crop_policy_version" in crop.differences(t) and crop.sha256 != t.sha256
    anchors = pinned / "redaction" / "anchors.yaml"
    doc = yaml.safe_load(anchors.read_text())
    doc["regions"][0]["rect"]["w"] += 4
    anchors.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert cc.tuple_in_force(pinned).differences(t) == ("redaction_version",)
    other = dataclasses.replace(t, model_id="claude-other-1")
    assert other.differences(t) == ("model_id",)


def test_the_tuple_carries_the_redaction_version_every_verdict_is_stamped_with(pinned):
    """Choice 2: the run's stamp (with the login's sweep) is the tuple's redaction version; an account rotation
    changes neither."""
    from sentinel.capture import RedactionList, Sweep, redaction_version
    t = cc.tuple_in_force(pinned)
    anchors = RedactionList.load(pinned / "redaction" / "anchors.yaml", pinned)
    stamps = {redaction_version(anchors, Sweep.for_login([acct], "Test Login")) for acct in ("D-20481133", "D-20489999")}
    assert stamps == {t.redaction_version}
    fx = pinned / "fixtures" / "fx.rig-01.paper.yaml"
    fx.write_text(fx.read_text().replace("D-20481133", "D-20489999"))
    assert cc.tuple_in_force(pinned) == t


# -- the hashes a run records (3.7, 8.3) ----------------------------------------------------------------------------

def test_the_diff_hash_covers_the_whole_diff_engine_file_settle_included():
    params = load_params()
    moved = {**params, "settle": {**params["settle"], "_probe": 1}}
    assert params_sha256(moved) == params_sha256(params)               # the fingerprint's hash leaves settle out
    assert cc.diff_params_sha256(moved) != cc.diff_params_sha256(params)
    assert cc.diff_params_sha256() == cc.diff_params_sha256(params)


def test_hashes_go_on_the_run_row(tmp_path, pinned):
    class Cfg:
        root = pinned

    h = cc.hashes(Cfg())
    assert h == cc.hashes(str(pinned)) and h.reviewer is not None and h.reviewer_version_sha256 == h.reviewer.sha256
    assert cc.hashes(EXAMPLE).reviewer_version_sha256 is None
    with Store(tmp_path / "s.db") as store:
        store.begin_run(dataclasses.replace(plan("run-1", "pr", "2026-11-10T10:00:00+00:00"), **h.plan_fields()))
        row = store.query("SELECT diff_params_sha256, reviewer_version_sha256 FROM runs")[0]
    assert tuple(row) == (h.diff_params_sha256, h.reviewer_version_sha256)


# -- the canary re-run rule (3.7, 3.15) -----------------------------------------------------------------------------

def plan(run_id: str, run_type: str, at: str) -> RunPlan:
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=14400,
                   started_at=at, scope=(ScopeEntry(CP, "ws.tier-a.active-trader", "active_trader", 1, "frozen"),))


H1 = cc.ChangeHashes("sha256:" + "1" * 64, "sha256:" + "a" * 64)
H2 = cc.ChangeHashes("sha256:" + "2" * 64, "sha256:" + "a" * 64)


def nightly(store: Store, run_id: str, at: str, h: cc.ChangeHashes, *, ran: bool = True, state: str = "ok") -> None:
    store.begin_run(dataclasses.replace(plan(run_id, "nightly", at), **h.plan_fields()))
    store.update_summary(run_id, {"canary": {"canary_state": state, "no_canary_ran": not ran}})


def test_a_product_run_after_a_canary_pass_on_its_hashes_is_not_refused(tmp_path):
    with Store(tmp_path / "s.db") as store:
        nightly(store, "run-2026-11-10-nightly-01", "2026-11-10T08:12:00+00:00", H1, state="detector_blind")
        cc.refuse_if_stale(store, H1, "pr")                             # a blind result banners; it does not refuse
        assert cc.last_canary_run(store).canary_state == "detector_blind"


def test_a_diff_engine_change_without_a_newer_canary_blocks_the_next_product_run(tmp_path):
    with Store(tmp_path / "s.db") as store:
        nightly(store, "run-2026-11-10-nightly-01", "2026-11-10T08:12:00+00:00", H1)
        with pytest.raises(cc.CanaryStale) as exc:
            cc.refuse_if_stale(store, H2, "merge")
        assert exc.value.verdict == ("aborted_harness", "config_invalid")
        assert exc.value.staleness.changed == ("diff_params",)
        assert "diff_engine.yaml parameters sha256:222222222222" in str(exc.value)
        assert "run-2026-11-10-nightly-01" in str(exc.value)
        cc.refuse_if_stale(store, H2, "nightly")                        # the Nightly run is the canary pass
        cc.refuse_if_stale(store, H2, "calibration")
        nightly(store, "run-2026-11-11-nightly-01", "2026-11-11T08:12:00+00:00", H2)
        cc.refuse_if_stale(store, H2, "rc")
        with pytest.raises(cc.CanaryStale):                             # a revert is a change too
            cc.refuse_if_stale(store, H1, "pr")


def test_a_reviewer_tuple_change_is_stale_and_an_unpinned_tuple_is_not_compared(tmp_path):
    with Store(tmp_path / "s.db") as store:
        nightly(store, "run-2026-11-10-nightly-01", "2026-11-10T08:12:00+00:00", H1)
        new_tuple = dataclasses.replace(H1, reviewer_version_sha256="sha256:" + "b" * 64)
        assert cc.staleness(store, new_tuple).changed == ("reviewer_version",)
        assert cc.staleness(store, dataclasses.replace(H1, reviewer_version_sha256=None)) is None


def test_no_canary_pass_yet_or_one_that_did_not_run_is_stale(tmp_path):
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(cc.CanaryStale, match="never run"):
            cc.refuse_if_stale(store, H1)
        nightly(store, "run-2026-11-10-nightly-01", "2026-11-10T08:12:00+00:00", H1)
        nightly(store, "run-2026-11-11-nightly-01", "2026-11-11T08:12:00+00:00", H2, ran=False)
        assert cc.last_canary_run(store).run_id == "run-2026-11-10-nightly-01"
        cc.refuse_if_stale(store, H1)


# -- L-XREF-29 (4.15, 4.16) -----------------------------------------------------------------------------------------

def run_check(root: Path) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    lint_cc.check(Repo.load(root), lambda rule, file, message, path="", severity=None:
                  found.append((rule, file, message)), 1, TODAY)
    return found


def write_report(root: Path, t: cc.ReviewerTuple, rel: str | None = None, **gate_values) -> Path:
    gates = []
    for g in E.GATES:
        value = gate_values.get(g.name, 1.0 if g.op == ">=" else 0.0)
        gates.append(E.GateResult(g.name, value, g.threshold, g.passes(value)))
    p = root / (rel or E.report_path(t))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(E.report_doc(t, gates, eval_set_version="v1", run_at="2026-11-10T08:00:00Z"),
                                sort_keys=False))
    return p


def test_the_example_config_is_clean_and_the_rule_is_no_longer_skipped():
    report = lint(EXAMPLE, 1, TODAY)
    assert not [v for v in report.violations if v.rule == "L-XREF-29"]
    assert "L-XREF-29" not in report.skipped


def test_a_tuple_in_force_without_its_eval_report_is_an_error(pinned):
    t = cc.tuple_in_force(pinned)
    (found,) = run_check(pinned)
    assert found[0] == "L-XREF-29" and found[1] == f"eval/reports/{t.hex}.yaml" and "has no eval report" in found[2]
    errors = [v for v in lint(pinned, 1, TODAY).errors if v.rule == "L-XREF-29"]
    assert len(errors) == 1


def test_a_passing_report_for_the_tuple_clears_the_rule(pinned):
    write_report(pinned, cc.tuple_in_force(pinned))
    assert run_check(pinned) == []
    assert not [v for v in lint(pinned, 1, TODAY).violations if v.rule == "L-XREF-29"]


def test_a_failing_gate_or_a_changed_tuple_is_an_error(pinned):
    t = cc.tuple_in_force(pinned)
    write_report(pinned, t, hallucination_rate=0.05)
    assert any("gate hallucination_rate fails" in m for _, _, m in run_check(pinned))
    write_report(pinned, t)
    p = pinned / "phase0" / "branches.yaml"                               # the PR moves the pin, no new report
    p.write_text(p.read_text().replace(MODEL, "claude-sonnet-5"))
    (found,) = run_check(pinned)
    assert "has no eval report" in found[2]


def test_an_inconsistent_report_anywhere_is_an_error(pinned):
    t = cc.tuple_in_force(pinned)
    write_report(pinned, t)
    old = dataclasses.replace(t, model_id="claude-old-1")
    write_report(pinned, old, rel="eval/reports/" + "0" * 64 + ".yaml", hallucination_rate=0.5)   # failing is fine
    found = run_check(pinned)
    assert [m for _, f, m in found] == ["the file name is not the tuple hash " + old.sha256 + " without its prefix "
                                        "(4.16)"]
