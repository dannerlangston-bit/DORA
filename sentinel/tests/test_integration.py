"""The integrated pipeline: the Oracle, the Orchestrator and the Reviewer adapter in the demo runs, and the five
scenarios that exercise them end to end (sentinel.pipeline, sentinel.demo). No test makes a network call."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel import records
from sentinel.demo import (AT, EXAMPLE, FREEZE_AT, INDEX, INDEX_FILES, LIVE_ACCOUNT, SCENARIOS, STOCK_FLOW,
                           clean_scenario, main, mask_expiry, run_demo, write_index)
from sentinel.oracle.tables import MarginRules
from sentinel.orchestrator.clock import ManualClock
from sentinel.oracle import evaluate_assertions
from sentinel.oracle.sources import GroundTruthReader
from sentinel.pipeline.assertions import OCR_CONFIDENCE, FlowOracle
from sentinel.pipeline.config import SentinelConfig, materialize
from sentinel.orchestrator.simrig import SimulatedRig
from sentinel.pipeline.orchestration import RigScript, ShardSink, shard_setup
from sentinel.pipeline.run import planned_for, run_pipeline
from sentinel.pipeline.sim import SimApp, SimBuild
from sentinel.records import CheckpointOutcome
from sentinel.reviewer import ModelResponse, ReviewerConfig, ReviewerJob, TransportError, Usage
from sentinel.store import Store
from sentinel.taxonomy import load

S03 = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s03"
S06 = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s06"
S07 = f"flow/dialog.order-confirmation/{STOCK_FLOW}/s07"
POSITIONS = "flow/monitor.activity.position-statement/flow.trade.active-trader.read-back/cp-positions"
CHART = "charts.flexible-grid.chart-cell"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Each scenario once per module (the scenarios are deterministic, test_demo checks that)."""
    root = tmp_path_factory.mktemp("integration")
    cache: dict[str, tuple[Path, list]] = {}

    def get(name: str):
        if name not in cache:
            cache[name] = (root / name, run_demo(name, root))
        return cache[name]
    get.root = root
    return get


@pytest.fixture(scope="module")
def cfg() -> SentinelConfig:
    return SentinelConfig.load(EXAMPLE)


def stored(out: Path, run_id: str):
    with Store(out / "sentinel.db") as store:
        return store.outcomes(run_id), store.findings(run_id), store.step_outcomes(run_id), \
            store.conn.execute("SELECT count(*) FROM artifacts").fetchone()[0]


def legal(outcomes: list[CheckpointOutcome]) -> None:
    t = load()
    for o in outcomes:
        t.check_checkpoint(o.outcome, o.reason, o.gate)
        records.from_dict(CheckpointOutcome, o.to_dict())


def alerts(out: Path) -> list[dict]:
    return json.loads((out / "alerts.json").read_text())


def orchestrator(out: Path) -> list[dict]:
    return json.loads((out / "orchestrator.json").read_text())


# -- the Oracle (2.8, 4.7) ---------------------------------------------------------------------------------

def _check(cfg, build: SimBuild, step: str, flow: str = STOCK_FLOW):
    fo = FlowOracle.load(cfg, datetime(2026, 11, 10, 15, 31, tzinfo=timezone.utc))
    cp = planned_for(cfg, cfg.flow_step(flow, step)["checkpoint_id"])
    cap = SimApp(build).capture(cp, "candidate", controls=cfg.control_ids(cp.surface_id))
    reg = SimApp.registry(cp, cap.screen, cap.info, cfg.regions_doc(cp.checkpoint_id, cp.surface_id, "flow", None))
    return fo.check(flow, step, cp.data_mode, cap, reg, {lm.id: lm.expected for lm in reg.landmarks}, "candidate")


def test_every_example_flow_checkpoint_passes_its_assertions_on_the_base_build(cfg):
    for flow in (STOCK_FLOW, "flow.trade.active-trader.read-back", "flow.canary.ticket-stock"):
        for st in cfg.flow_docs[flow]["steps"]:
            if st.get("type") == "checkpoint" and st.get("expect"):
                chk = _check(cfg, SimBuild("b"), st["id"], flow)
                assert [r.status for r in chk.verdict.results] == ["passed"] * len(st["expect"]), (flow, st["id"])


def test_the_oracle_reads_what_synth_drew_and_catches_a_transposed_cost(cfg):
    chk = _check(cfg, SimBuild("c", frozenset({"confirm-cost-transposed"})), "s07")
    assert chk.values["rgn.confirm.cost"] == "15,205.00" and chk.values["rgn.confirm.price"] == "150.25"
    assert chk.verdict.failed == ("cost_math",)
    fields = chk.verdict.facts_fields()
    assert fields["assertion_ids"] == ("cost_math",) and "15,205.00" in fields["assertion_detail"]
    surcharge = _check(cfg, SimBuild("d", frozenset({"bp-effect-house-surcharge"})), "s07")
    assert surcharge.verdict.failed == ("R02",) and surcharge.verdict.results[5].label == "bracket check"


def test_a_format_only_present_over_a_column_is_judged_row_by_row_by_the_oracle(cfg):
    """`ps_qty` (present, format int) over the seven Qty rows: the pipeline hands the whole column to the Oracle,
    which passes it row by row and fails it on the first row that is not an int."""
    chk = _check(cfg, SimBuild("b"), "cp-positions", "flow.trade.active-trader.read-back")
    assert chk.values["rgn.ps.qty"].count("\n") == 6 and chk.verdict.results[0].status == "passed"
    assert not hasattr(FlowOracle, "_per_row")                 # no per-row loop left in the pipeline
    fo = FlowOracle.load(cfg, datetime(2026, 11, 10, tzinfo=timezone.utc))
    step = cfg.flow_step("flow.trade.active-trader.read-back", "cp-positions")
    reader = GroundTruthReader({"rgn.ps.qty": "100\nabc\n1"}, confidence=OCR_CONFIDENCE)
    (bad,) = evaluate_assertions({"expect": [step["expect"][0]]}, {"ocr": reader}, {}, cfg.fixture, fo.rules,
                                 context=fo.context("flow.trade.active-trader.read-back", step, "masked_live"))
    assert bad.status == "failed" and "'abc'" in bad.detail


def test_margin_rules_are_the_2_8_3_brackets_and_a_config_without_them_is_invalid(tmp_path, cfg):
    rules = MarginRules.load(EXAMPLE / "fixtures" / "margin_rules.yaml")
    assert not rules.pinned and rules.label == "bracket check"
    eq = rules.bracket("equity", "margin")
    assert (eq.low, eq.high) == (Decimal("0.50"), Decimal("1.00"))
    root = materialize(EXAMPLE, clean_scenario(date(2026, 11, 10)).overlay, tmp_path / "cfg")
    (root / "fixtures" / "margin_rules.yaml").unlink()
    r = run_pipeline(root, clean_scenario(date(2026, 11, 10)), tmp_path / "out")
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    assert "margin_rules.yaml" in r.lint_errors[0]


# -- the Orchestrator glue (5.2) ----------------------------------------------------------------------------

def test_a_shard_context_from_the_config_passes_every_gate_on_the_simulated_rig(cfg):
    from sentinel.orchestrator.machine import Orchestrator
    cps = [planned_for(cfg, S07)]
    ctx, sim = shard_setup(cfg, run_id="run-x", run_type="pr", shard_id="run-x/1", shard_no=1, build_id="b-1",
                           workspace_id="ws.tier-a.active-trader", script=RigScript())
    assert ctx.allowlist.entries == ("D-20481133", "D-20481134") and ctx.fixture_account == "D-20481134"
    assert ctx.profile.active_trader_panels == ("at-main", "at-grid")
    clock = ManualClock()
    rig = SimulatedRig(sim, clock=clock)
    sink = ShardSink([c.checkpoint_id for c in cps])
    seen: list[str | None] = []
    report = Orchestrator(rig, ctx, sink, clock=clock).run(lambda o: (seen.append(rig.read_title_build_id()),
                                                                      sink.done.add(S07)))
    assert report.verdict.status == "complete" and report.proof.line == "PROVEN: no orders sent"
    assert seen == ["b-1"]                      # the build re-read sees the build the launched gate accepted
    assert [r.gate for r in report.results][:8] == ["provisioned", "launched", "authenticated", "paper_mode_asserted",
                                                    "settings_attested", "workspace_loaded", "fixture_verified",
                                                    "canary_flow_passed"]


def test_the_shard_sink_holds_only_its_own_unreached_checkpoints():
    sink = ShardSink(["a", "b", "c"])
    sink.done.add("a")
    assert sink.fill_unreached("run", "env_error", "live_identity", gate="paper_mode_asserted") == ["b", "c"]
    assert sink.fill_unreached("run", "harness_error", "x") == [] and sink.filled["b"].gate == "paper_mode_asserted"


def test_clean_runs_pass_every_gate_page_nobody_and_prove_no_orders_sent(runs):
    out, (r,) = runs("clean-pr")
    assert alerts(out) == [] and r.proof == "PROVEN: no orders sent"
    (run,) = orchestrator(out)
    assert {s["verdict"] for s in run["shards"]} == {"complete"} and len(run["shards"]) == len(r.shards) == 4
    assert all([g["control"] for g in s["gates"]][:8] == ["preflight", "launch", "login", "paper_mode", "attestation",
                                                          "workspace", "fixture", "canary_flow"] for s in run["shards"])
    assert all(any(g["control"] == "build_reread" for g in s["gates"]) for s in run["shards"])
    assert (out / "heartbeat" / "shard-1.json").exists()
    assert "paper mode asserted 4 times" in (out / "pr-comment.md").read_text()


# -- the five scenarios -------------------------------------------------------------------------------------

def test_wrong_number_is_assertion_failed_cost_math_with_both_builds_values(runs):
    out, (r,) = runs("wrong-number")
    outs, finds, _, _ = stored(out, r.run_id)
    legal(outs)
    (s07,) = [o for o in outs if o.checkpoint_id == S07]
    assert (s07.outcome, s07.reason, s07.assertion_ids) == ("assertion_failed", "assertion:cost_math", ["cost_math"])
    assert {o.outcome for o in outs if o.checkpoint_id != S07} == {"identical"}
    (f,) = [f for f in finds if f.checkpoint_id == S07]
    assert f.kind == "assertion_failed" and f.region_id == "rgn.confirm.cost"
    raw = f.evidence["raw_values"]
    assert (raw["expected"], raw["read"], raw["source"]) == ("$15,025.00", "$15,205.00", "ocr")
    assert raw["candidate.rgn.confirm.cost.ocr"].startswith("'15,205.00'")
    assert raw["base.rgn.confirm.cost.ocr"].startswith("'15,025.00'")
    text = (out / "pr-comment.md").read_text()
    assert "assertion_failed (assertion: cost_math)" in text
    assert "candidate: rgn.confirm.qty '100' (ocr), rgn.confirm.price '150.25' (ocr), rgn.confirm.cost '15,205.00'" in text
    assert "base: rgn.confirm.qty '100' (ocr), rgn.confirm.price '150.25' (ocr), rgn.confirm.cost '15,025.00'" in text
    assert "Assertion cost_math: expected $15,025.00, read $15,205.00 by ocr." in text
    assert r.commit_status["state"] == "failure" and r.status == "complete"


def test_paper_mode_mismatch_aborts_safety_before_any_capture(runs):
    out, (r,) = runs("paper-mode-mismatch")
    outs, finds, steps, n_artifacts = stored(out, r.run_id)
    legal(outs)
    assert (r.status, r.reason) == ("aborted_safety", "live_identity")
    assert outs and {(o.outcome, o.reason, o.gate) for o in outs} == {("env_error", "live_identity",
                                                                       "paper_mode_asserted")}
    assert n_artifacts == 0 and not (out / "calibration").exists() and r.reviewer_calls == []
    assert not [p for p in (out / "artifacts").rglob("*") if p.is_file() and p.name != "manifest.json"]
    with Store(out / "sentinel.db") as store:
        run = store.run_outcome(r.run_id)
    assert (run.status, run.reason, run.last_gate) == ("aborted_safety", "live_identity", "paper_mode_asserted")
    text = (out / "pr-comment.md").read_text()
    assert "**Abort**" in text and "Trigger: live_identity" in text and "Gate: paper_mode_asserted" in text
    assert "No run starts on rigs rig-01, rig-02, rig-03 and rig-04 until the incident closes." in text
    assert "Incident: S1" in text and "**Unexpected (" not in text and r.commit_status["state"] == "error"
    s1 = [a for a in alerts(out) if a["incident"] == "S1"]
    assert s1 and all(a["priority"] == "P1" and a["rule"] == "safety:aborted_safety" and "harness_on_call" in a["paged"]
                      for a in s1)
    assert {a["action"] for a in orchestrator(out)[0]["persisted_actions"]} >= {"block_login"}
    assert LIVE_ACCOUNT not in json.dumps(alerts(out)) + text      # the account id itself is never echoed


def test_attempted_send_is_dropped_by_the_gateway_and_proven_harmless(runs):
    out, (r,) = runs("attempted-send")
    outs, finds, steps, _ = stored(out, r.run_id)
    legal(outs)
    assert (r.status, r.reason) == ("aborted_safety", "attempted_send")
    by = {o.checkpoint_id: o for o in outs}
    assert (by[S07].outcome, by[S07].reason, by[S07].gate) == ("env_error", "attempted_send", "running")
    assert by[S03].outcome == by[S06].outcome == "identical"           # captured before the abort
    (blocked,) = [s for s in steps if s.status == "blocked_by_gateway"]
    assert (blocked.flow_id, blocked.step_id) == (STOCK_FLOW, "open-confirm+enter")
    # the gateway event goes through the store (8.3 step_events gate_decision, gate_reason, gate_class)
    assert blocked.gateway_event == {"decision": "refused", "reason": "guarded_key_refused", "class": "safety"}
    with Store(out / "sentinel.db") as store:
        rows = store.conn.execute("SELECT gate_decision, gate_reason, gate_class FROM step_events WHERE run_id = ? "
                                  "AND gate_decision <> 'none'", (r.run_id,)).fetchall()
        assert [tuple(x) for x in rows] == [("refused", "guarded_key_refused", "safety")]
        assert store.gateway_drops(r.run_id) == 1
    (run,) = orchestrator(out)
    (event,) = run["gateway_events"]                 # orchestrator.json keeps its copy
    assert event["gateway_event"] == blocked.gateway_event
    shard = next(s for s in run["shards"] if s["verdict"] == "aborted_safety")
    assert shard["proof"] == "PROVEN: no orders sent" and "recovery_login" in shard["kill_switch"]
    assert [a for a in run["persisted_actions"] if a["action"] == "quarantine_flow"][0]["flow_id"] == STOCK_FLOW
    text = (out / "pr-comment.md").read_text()
    assert "Trigger: attempted_send" in text and "Post-run proof: PROVEN: no orders sent" in text
    # the run's stored proofs give the Safety row each shard's buying power line (6.6 step 5, 9.4)
    assert ("gateway 1 drop, Cancel All not needed, proof PROVEN: no orders sent; shard run-2026-11-10-pr-4853-01/1: "
            "buying power 2,500.00 to 2,500.00, drift 0.00, ok;") in text
    summary = (out / "summary.txt").read_text()
    assert "WORKAROUND" not in text + summary and "proof PROVEN: no orders sent" in summary
    assert "reason code\n  (guarded_key_refused)" in summary and "run health counts it as 1 gateway drop" in summary
    (page,) = alerts(out)
    assert page["incident"] == "S2" and "flow_owner" in page["paged"] and page["safety_table"]


def test_an_order_that_got_out_is_orders_sent_and_the_run_stays_aborted_safety(tmp_path, monkeypatch):
    """The fifth proof verdict end to end (6.6): an order the gateway never saw (the server accepted it) is found by
    one shard's teardown proof, the kill switch cancels it, the run is aborted_safety (undeclared_order_row) and the
    report prints that shard's `Proof.line` as the run's proof, the most severe of the four shards'."""
    import sentinel.pipeline.run as pipeline_run

    class LeakyRig(SimulatedRig):
        leaked = 0

        def teardown(self):
            if LeakyRig.leaked == 0:
                LeakyRig.leaked += 1
                self._send()
            return super().teardown()

    monkeypatch.setattr(pipeline_run, "SimulatedRig", LeakyRig)
    r = run_pipeline(EXAMPLE, clean_scenario(date(2026, 11, 10)), tmp_path)
    line = "ORDERS SENT: undeclared rows (sim-order-1); Cancel All verified"
    assert (r.status, r.reason, r.proof) == ("aborted_safety", "undeclared_order_row", line)
    assert sorted(s["proof"] for s in r.shards) == [line] + ["PROVEN: no orders sent"] * 3
    with Store(tmp_path / "sentinel.db") as store:
        run = store.run_outcome(r.run_id)
    assert (run.status, run.reason, run.last_gate) == ("aborted_safety", "undeclared_order_row", "teardown")
    text = (tmp_path / "pr-comment.md").read_text()
    assert "Trigger: undeclared_order_row" in text and f"Post-run proof: {line}" in text
    assert "An order reached the account: login " in text and "Incident: S1" in text
    safety_row = next(row for row in text.splitlines() if row.startswith("| Safety |"))
    assert f"Cancel All verified, proof {line}; shard " in safety_row and "; an order reached the account" in safety_row
    assert "UNPROVEN" not in text and r.commit_status["state"] == "error"
    *_, page = [a for a in alerts(tmp_path) if a["run_reason"] == "undeclared_order_row"]   # the kill switch's proof
    assert page["incident"] == "S1" and "login_owner" in page["paged"] and page["detail"] == line


def test_app_crash_fails_app_alive_skips_the_ladder_and_resumes_after_a_relaunch(runs):
    out, (r,) = runs("app-crash")
    outs, finds, _, _ = stored(out, r.run_id)
    legal(outs)
    by = {o.checkpoint_id: o for o in outs}
    frozen = by[FREEZE_AT]
    assert (frozen.outcome, frozen.reason, frozen.detail) == ("assertion_failed", "assertion:app_alive", "hang")
    ladder = [o for o in outs if o.test_kind == "sweep" and o.surface_id == AT]
    order = sorted(ladder, key=lambda o: (-o.size_step["w"], -o.size_step["h"]))
    after = order[[o.checkpoint_id for o in order].index(FREEZE_AT) + 1:]
    assert len(after) == 7 and {(o.outcome, o.reason) for o in after} == {("not_run", "app_crash")}
    assert {o.outcome for o in order[:order.index(frozen)]} == {"identical"}
    (crash,) = [f for f in finds if f.kind == "app_crash"]
    assert crash.checkpoint_id == FREEZE_AT and crash.tier == 1
    shard = next(s for s in orchestrator(out)[0]["shards"] if FREEZE_AT in json.dumps(s) or s["rig"] == "rig-04")
    controls = [g["control"] for g in shard["gates"]]
    assert controls.count("launch") == 2 and controls.count("login") == 2 and controls.count("paper_mode") == 2
    assert "watchdog" in controls and shard["verdict"] == "complete"
    resumed = [o for o in outs if o.test_kind == "census" and o.checkpoint_id.startswith("census/trade.")]
    assert resumed and {o.outcome for o in resumed} == {"identical"}     # the units after the ladder ran
    assert r.status == "complete" and r.commit_status["state"] == "failure"
    text = (out / "pr-comment.md").read_text()
    assert "assertion_failed (assertion: app_alive)" in text
    assert "What moved: thinkorswim hang (screenshot_staleness). Fails at height 480 for width 640" in text


def test_config_invalid_aborts_harness_at_preflight_g1(runs):
    out, (r,) = runs("config-invalid")
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    assert any("L-RGN-03" in e for e in r.lint_errors) and r.proof == "PROVEN: no session established"
    with Store(out / "sentinel.db") as store:
        run = store.run_outcome(r.run_id)
        assert (run.last_gate, store.outcomes(r.run_id)) == ("provisioned", [])
        (proof,) = store.post_run_proofs(r.run_id)               # the run-level row of a run with no shard (SD-61)
        assert (proof.shard_id, proof.login_alias, proof.verdict) == (None, None, "proven_no_session")
    (page,) = alerts(out)
    assert (page["rule"], page["priority"], page["paged"]) == ("aborted_harness", "P1", ["harness_on_call"])
    text = (out / "pr-comment.md").read_text()
    assert "**Abort**" in text and "`config_invalid` (rules L-RGN-03)" in text and r.commit_status["state"] == "error"
    assert "- Not re-queued: the same config would fail again; fix it first" in text and "Re-queued once" not in text
    assert "Scope not computed: the run stopped at provisioned before it planned any checkpoint." in text
    assert "| Tier |" not in text and "| Fingerprint | not read |" in text
    assert "| Wall clock / budget | 1:00 / 45:00" in text
    assert "PR comment posted (simulated: written to pr-comment.md)" in text and "unknown" not in text


def test_expired_mask_runs_the_day_after_the_first_mask_expiry_and_completes(runs):
    """4.1, 4.8: the expired mask is an expired record at run start, not `config_invalid`; the run completes with the
    record in run health and a `mask_expired` finding."""
    out, (r,) = runs("expired-mask")
    day = date.fromordinal(mask_expiry(EXAMPLE).toordinal() + 1)
    assert r.run_id == f"run-{day.isoformat()}-pr-4823-01"
    assert r.status == "complete" and not r.lint_errors
    assert [f.kind for f in r.findings if f.kind.endswith("_expired")] == ["mask_expired"]
    text = (out / "pr-comment.md").read_text()
    assert "Expired records" in text and "mask expired: msk.ps.last-update-01" in text
    assert "mask expired: msk.ps.last-update-01" in (out / "summary.txt").read_text()


# -- sentinel demo all: one line per scenario and out/index.html ---------------------------------------------

def test_the_index_links_every_scenario_output_and_says_what_to_check(runs):
    done = {n: runs(n)[1] for n in ("clean-pr", "wrong-number", "paper-mode-mismatch")}
    page = write_index(runs.root, done).read_text()
    assert set(INDEX) == set(SCENARIOS)
    for name in done:
        for f in INDEX_FILES:
            assert f'href="{name}/{f}"' in page and (runs.root / name / f).exists()
        assert INDEX[name][0].split(":")[0][:30] in page
    assert "<script" not in page and "http" not in page.split("<body>")[1]     # self-contained
    assert "aborted_safety (live_identity)" in page


def test_cli_lines_and_the_live_reviewer_needs_a_key(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["clean-pr", "--out", str(tmp_path), "--reviewer", "live"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err and not (tmp_path / "clean-pr").exists()
    assert main(["config-invalid", "--out", str(tmp_path)]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("config-invalid: aborted_harness (config_invalid), check error -> ")


# -- the Reviewer adapter (3.12, 3.15): ModelReviewer over a fake transport ----------------------------------

class FakeTransport:
    """Answers every item with `matches_manifest`, or fails every call; records the requests."""

    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, []

    def create_message(self, params: dict) -> ModelResponse:
        self.calls.append(params)
        if self.fail:
            raise TransportError("server", "unavailable", 503)
        text = params["messages"][-1]["content"][0]["text"]
        data = json.loads(text.split("<item_data>\n", 1)[1].split("\n</item_data>", 1)[0])
        entry = (data.get("manifest_entry") or {}).get("entry_id")
        answer = {"schema_version": "reviewer-1", "task": "manifest_match", "verdict": "matches_manifest",
                  "confidence": 0.95, "matched_manifest_entry_id": entry, "observed_changes": [
                      {"change_type": "text_change", "pointer": {"image": "candidate", "x": 0, "y": 0, "w": 1, "h": 1},
                       "explained_by_manifest": True, "text": "header text"}],
                  "description": "The header shows the period.", "visibility": "not_applicable",
                  "uncertainty_reasons": ["none"]}
        return ModelResponse("msg_1", "claude-opus-5-5", "end_turn", json.dumps(answer), Usage(3000, 300, 0, 0),
                             "req_1")

    def retrieve_model(self, model_id: str) -> bool:
        return True


def _live(tmp_path, transport) -> tuple:
    job = ReviewerJob(ReviewerConfig(sync_concurrency=1), transport, "pr")
    r = run_pipeline(EXAMPLE, clean_scenario(date(2026, 11, 10)), tmp_path, live=job)
    with Store(tmp_path / "sentinel.db") as store:
        return r, [o for o in store.outcomes(r.run_id) if o.surface_id == CHART]


def test_the_live_adapter_reviews_with_crops_from_the_artifact_store(tmp_path):
    t = FakeTransport()
    r, chart = _live(tmp_path, t)
    # four chart cells, four items; the verdict cache (3.13) answers the two whose crops repeat another's
    assert len(r.reviewer_calls) == 4 and len(t.calls) == 2
    assert {c["model_id"] for c in r.reviewer_calls} == {"claude-opus-5-5"}
    assert {(o.outcome, o.classification.by, o.classification.model_id) for o in chart} == \
        {("intended", "reviewer", "claude-opus-5-5")}
    images = [b for b in t.calls[0]["messages"][-1]["content"] if b.get("type") == "image"]
    assert len(images) == 3                                     # base, candidate and heatmap crops at 1:1
    manifest = json.loads((tmp_path / "artifacts" / "manifest.json").read_text())
    assert any(e["name"].endswith("/review/candidate.png") and e["kind"] == "capture"
               for e in manifest.get("entries", manifest.get("artifacts", [])))


def test_an_unreachable_live_reviewer_leaves_the_item_to_a_human(tmp_path):
    r, chart = _live(tmp_path, FakeTransport(fail=True))
    assert {(o.outcome, o.reason) for o in chart} == {("needs_human", "reviewer_unavailable")}
    assert r.reviewer_calls[0]["answer"] is None and r.reviewer_calls[0]["unavailable"].startswith("pending")


def test_sentinel_review_lists_the_items_and_live_needs_a_key(tmp_path, capsys, monkeypatch):
    from sentinel.cli import main as cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli(["review", "--live", "clean-pr", "--out", str(tmp_path)]) == 2
    items = json.loads((tmp_path / "review-clean-pr" / "review-items.json").read_text())
    assert len(items) == 4 and {i["task"] for i in items} == {"manifest_match"}
    assert all(i["crop_sizes"]["candidate"][0][0] > 0 for i in items)
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err and not (tmp_path / "review-clean-pr" / "review-live.json").exists()


def test_the_lint_reads_margin_rules_against_its_schema(tmp_path):
    from sentinel.lint import lint
    root = materialize(EXAMPLE, {}, tmp_path / "cfg")
    assert not lint(root, phase=1, today=date(2026, 11, 10)).errors
    p = root / "fixtures" / "margin_rules.yaml"
    p.write_text(p.read_text().replace("asset: equity, account: margin", "asset: stock, account: margin"))
    errors = lint(root, phase=1, today=date(2026, 11, 10)).errors
    assert [(v.rule, v.file) for v in errors] == [("L-FIX-00", "fixtures/margin_rules.yaml")]


def test_the_reviewer_block_is_read_with_from_mapping(tmp_path):
    from sentinel.pipeline.live import load_reviewer_config
    from sentinel.reviewer import ConfigError
    assert load_reviewer_config(None) == ReviewerConfig()
    good = tmp_path / "reviewer.yaml"
    good.write_text("reviewer:\n  effort: medium\n  run_ceiling_usd: 20.0\n")
    cfg = load_reviewer_config(good)
    assert (cfg.effort, cfg.run_ceiling_usd, cfg.model_id) == ("medium", 20.0, "claude-opus-5-5")
    bad = tmp_path / "bad.yaml"
    bad.write_text("reviewer:\n  temperature: 0\n")
    with pytest.raises(ConfigError):
        load_reviewer_config(bad)
