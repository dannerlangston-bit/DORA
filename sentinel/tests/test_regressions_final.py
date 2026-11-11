"""Final-pass regressions: one safety page per teardown incident (6.6, 9.10), the checkpoint count of every
`not_run` reason under Coverage (2.12, 9.2), a readable login label in place of the vault path (6.9, 9.2), and one
`post_run_proofs` row per shard proof, read back for the report (6.6, 8.3)."""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from sentinel.demo import AT, BASE_ID, EXAMPLE, _main_build, clean_scenario, run_demo
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.killswitch import ProofInputs, post_run_proof, proof_line
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.rig import OrderBook, OrderRow
from sentinel.orchestrator.simrig import MemorySink, SimScenario, SimulatedRig, default_context
from sentinel.pipeline.orchestration import login_alias, login_label
from sentinel.pipeline.run import NightlyScenario, run_nightly, run_pipeline
from sentinel.pipeline.sim import SimBuild
from sentinel.records import CheckpointOutcome, DeclaredSend, RunOutcome
from sentinel.report import render_pr_comment
from sentinel.report.model import login_phrase, not_run_line, orders_sent_note
from sentinel.store import ImportConflict, PostRunProof, RunPlan, ShardPlan, Store, StoreRefused
from sentinel.store.store import PROOF_VERDICTS

from test_report_sample import RUN_ID, SPEC_9, sample

RUN = "run-2026-11-10-pr-9001-01"


def _order_slips_in(rig: SimulatedRig):
    """The shard's work: one checkpoint, then an order the gateway never saw reaches the Working grid."""
    def work(o: Orchestrator) -> None:
        o.sink.record("c1", "passed")
        rig._send()
    return work


def _teardown_incident(scenario: SimScenario | None = None):
    rig = SimulatedRig(scenario, clock=ManualClock())
    return Orchestrator(rig, default_context(), MemorySink(["c1", "c2"]), clock=rig.clock).run(_order_slips_in(rig))


# == 1. one page per incident, sent after the kill switch sequence =========================================

def test_an_undeclared_order_at_teardown_pages_once_with_the_final_cancel_all_state():
    rep = _teardown_incident()
    (page,) = [r for r in rep.results if r.alert is not None]
    assert page.detail == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All verified" == rep.proof.line
    assert page.alert.rule == "safety:aborted_safety" and "login_owner" in page.alert.paged and page.incident == "S1"
    assert not any("Cancel All not needed" in (r.detail or "") for r in rep.results)
    # sent after the sequence: the paging result is the last one recorded, after the kill switch ran
    assert rep.results[-1] is page and rep.kill_switch is not None and rep.kill_switch.proof is rep.proof
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "undeclared_order_row")


def test_the_one_page_carries_cancel_all_failed_when_the_control_is_not_located():
    rep = _teardown_incident(SimScenario(cancel_all_located=False))
    (page,) = rep.alerts
    assert page.rule == "safety:aborted_safety" and "login_owner" in page.paged
    (paging,) = [r for r in rep.results if r.alert is not None]
    assert paging.detail == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All failed" == rep.proof.line


def test_a_failed_recovery_login_still_sends_one_safety_page_naming_both_reads():
    rep = _teardown_incident(SimScenario(recovery_login_fails=1))
    (paging,) = [r for r in rep.results if r.alert is not None]
    assert paging.alert.rule == "safety:aborted_safety" and "login_owner" in paging.alert.paged
    assert paging.detail == ("ORDERS SENT: undeclared rows (sim-order-1) at teardown; after the kill switch sequence: "
                             "UNPROVEN: recovery_login_failed")
    assert {"block_login", "disable_rig"} <= set(paging.actions) and paging.incident == "S1"
    assert (rep.verdict.status, rep.verdict.reason) == ("aborted_safety", "undeclared_order_row")


def test_the_pipeline_writes_one_alert_for_an_order_found_at_teardown(tmp_path, monkeypatch):
    """End to end (alerts.json): the leaked order's shard pages once, with the verified Cancel All."""
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
    alerts = json.loads((tmp_path / "alerts.json").read_text())
    (page,) = [a for a in alerts if a["control"] != "fingerprint"]       # less 12.7 G5's on-call notice (10.7)
    assert page["detail"] == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All verified" == r.proof
    assert page["rule"] == "safety:aborted_safety" and page["incident"] == "S1" and "login_owner" in page["paged"]


# == 2. the checkpoint count of every not_run reason, under the Coverage table =============================

def _cp(cid: str, outcome: str, reason: str | None = None) -> CheckpointOutcome:
    return CheckpointOutcome(run_id="run-x", checkpoint_id=cid, outcome=outcome, reason=reason,
                             workspace_id="ws.tier-c.sweep-harness", persona="active_trader", data_mode="frozen")


def test_the_not_run_line_counts_checkpoints_per_reason_in_1_10_order():
    sweep = [_cp(f"sweep/{AT}/flexgrid/{w}x480", "not_run", "app_crash") for w in (320, 480, 640, 960, 1024, 1280, 1600)]
    flow = [_cp(f"flow/trade.order-entry.stock/flow.x/s0{i}", "not_run", "step_failed:s03") for i in (4, 5)]
    one = [_cp("census/trade.active-trader.ladder/ws.tier-a.active-trader", "not_run", "quarantine")]
    line = not_run_line(sweep + flow + one + [_cp(f"sweep/{AT}/flexgrid/320x360", "identical")])
    assert "".join(t.text for t in line) == (
        "Checkpoints not run: 1 quarantine (census checkpoint in quarantine), 2 step_failed (flow checkpoints after "
        "a failed step), 7 app_crash (sweep steps after the crash)")
    assert not_run_line([_cp(f"sweep/{AT}/flexgrid/320x360", "identical")]) is None


def test_app_crash_keeps_the_2_12_matrix_and_shows_the_seven_not_run_checkpoints(tmp_path):
    out, (r,) = tmp_path / "app-crash", run_demo("app-crash", tmp_path)
    text = (out / "pr-comment.md").read_text()
    coverage = text.split("**Coverage**\n", 1)[1].split("**Run health**", 1)[0]
    table, rest = coverage.split("\n\n", 1)
    assert table.splitlines()[2].startswith("| 1 | 5 | 4 | 4 (100 percent") and "0 app_crash" in table   # 2.12 cells
    assert rest.startswith("Checkpoints not run: 7 app_crash (sweep steps after the crash)\n\nScope: ")
    html = (out / "report.html").read_text()
    section = html.split('<section id="coverage">', 1)[1].split("</section>", 1)[0]
    assert "<p>Checkpoints not run: 7 app_crash (sweep steps after the crash)</p><p>Scope: " in section
    assert sum(1 for o in r.outcomes if (o.outcome, o.reason) == ("not_run", "app_crash")) == 7
    clean = run_demo("clean-pr", tmp_path)[0]
    assert "Checkpoints not run" not in clean.comment                       # omitted when nothing is not_run


def test_spec_9_2_template_carries_the_not_run_line():
    spec = SPEC_9.read_text(encoding="utf-8")
    template = spec.split("### 9.2", 1)[1].split("### 9.3", 1)[0]
    assert '{not_run_line: "Checkpoints not run: {n} {reason} ({which checkpoints}), ..." | omitted}' in template
    assert "Checkpoints not run: 7 app_crash (sweep steps after the crash)" in template


# == 3. a readable login label, never a vault path =========================================================

def test_the_reporter_never_prints_a_vault_reference_and_names_several_logins():
    s = sample()
    s["run"] = RunOutcome(run_id=RUN_ID, run_type="pr", status="complete_with_env_errors", reason="unproven_proof",
                          last_gate="reported")
    s["health"]["safety"].update(proof="UNPROVEN: recovery_login_failed",
                                 login="vault://sentinel/rigs/rig-01/tos-login")
    text = render_pr_comment(**s)
    assert "vault:" not in text and "next run on login (vault reference not printed) blocked" in text
    s["health"]["safety"]["login"] = ["rig-01 paper login", "rig-02 paper login"]
    text = render_pr_comment(**s)
    assert "next run on logins rig-01 paper login and rig-02 paper login blocked until a human verifies" in text
    assert login_phrase("rig-01 paper login") == "login rig-01 paper login"
    assert orders_sent_note(["a", "b"]).startswith("an order reached the account: logins a and b blocked and rigs")


def test_reports_summaries_and_alerts_name_the_login_by_its_label(tmp_path):
    out, (r,) = tmp_path / "paper-mode-mismatch", run_demo("paper-mode-mismatch", tmp_path)
    for name in ("pr-comment.md", "report.html", "summary.txt", "alerts.json", "orchestrator.json"):
        assert "vault" not in (out / name).read_text(), name
    assert ("next run on logins rig-01 paper login, rig-02 paper login, rig-03 paper login and rig-04 paper login "
            "blocked until a human verifies the account") in r.comment
    blocked = [a for a in json.loads((out / "orchestrator.json").read_text())[0]["persisted_actions"]
               if a["action"] == "block_login"]
    assert blocked and all(a["login"] == f"{a['rig']} paper login" for a in blocked)
    assert login_label("rig-01") == "rig-01 paper login" and login_alias("rig-01").startswith("vault://")


# == 4. one post_run_proofs row per shard proof, read back for the report ==================================

def _row(**kw) -> PostRunProof:
    base = {"shard_id": f"{RUN}/1", "run_id": RUN, "login_alias": login_alias("rig-01"), "verdict": "orders_sent",
            "send_policy_ref": "OPEN", "counts": {"undeclared": ["sim-order-1"]}, "cancel_all": "performed_verified",
            "paper_mode_assertions": 1, "paper_mode_mismatches": 0, "gateway_events": 12, "gateway_drops": 0,
            "produced_at": "2026-11-10T15:40:00Z"}
    return PostRunProof(**{**base, **kw})


def _store_with_run(tmp_path) -> Store:
    store = Store(tmp_path / "s.db")
    store.register_rig("rig-01", login_alias=login_alias("rig-01"), owner="asmith", snapshot_id="snap", allowlist_entry=1)
    store.begin_run(RunPlan(RUN, "pr", "cfg", "0" * 64, "0" * 64, "h", "AUTH", 2700, (),
                            (ShardPlan(f"{RUN}/1", "rig-01", scope_count=0),)))
    return store


def test_the_store_writes_and_reads_back_post_run_proofs_with_orders_sent(tmp_path):
    with _store_with_run(tmp_path) as store:
        store.write_proof(_row())
        store.write_proof(_row())                                          # the same row again: a no-op
        (back,) = store.post_run_proofs(RUN)
        assert back == _row(produced_at="2026-11-10T15:40:00+00:00") and back.verdict == "orders_sent"   # UTC (8.3)
        assert proof_line(back.verdict, back.reason, back.counts["undeclared"], back.cancel_all) == \
            "ORDERS SENT: undeclared rows (sim-order-1); Cancel All verified"
        with pytest.raises(ImportConflict):
            store.write_proof(_row(cancel_all="performed_unverified"))
        with pytest.raises(StoreRefused):
            store.write_proof(_row(shard_id="run-other/1"))
        assert "1 ORDERS SENT post-run proof(s)" in store.complete_blockers(RUN)
    with pytest.raises(StoreRefused):
        _row(counts={})                                                    # ORDERS SENT names its rows (6.6)
    with pytest.raises(StoreRefused):
        _row(verdict="unproven")                                           # UNPROVEN carries its reason


def test_every_proof_verdict_round_trips_through_its_8_3_row():
    book = OrderBook(position_rows=0, net_liq=2500.0)
    sent = OrderBook(working=(OrderRow("o-1", "working"),), position_rows=0, net_liq=2500.0)
    proofs = [post_run_proof(ProofInputs(book, book)),
              post_run_proof(ProofInputs(book, OrderBook(canceled=(OrderRow("o-1", "canceled", "CANCELED"),), position_rows=0, net_liq=2500.0),
                                         declared_sends=(DeclaredSend("run", "run/1", "flow.paper.send", "s03", "o-1",
                                                                      "2026-11-10T14:00:00+00:00"),))),
              post_run_proof(ProofInputs(None, None, session="never", tokens_issued=0)),
              post_run_proof(ProofInputs(book, None)),
              post_run_proof(ProofInputs(book, sent, cancel_all="failed"))]
    assert [p.verdict_code for p in proofs] == list(PROOF_VERDICTS)
    for p in proofs:
        assert proof_line(p.verdict_code, p.reason, p.counts["undeclared"], p.cancel_all_code) == p.line
    assert proofs[4].counts["rows"]["working orders"] == {"start": 0, "end": 1, "new": 1, "expected": 0,
                                                          "verdict": "UNDECLARED"}
    assert proofs[0].start_sha256 == proofs[0].end_sha256 != proofs[4].end_sha256 and proofs[2].start_sha256 is None


def test_the_pipeline_writes_one_proof_per_shard_and_the_report_reads_them_back(tmp_path, monkeypatch):
    import sentinel.pipeline.run as pipeline_run

    class LeakyRig(SimulatedRig):
        leaked = 0

        def teardown(self):
            if LeakyRig.leaked == 0:
                LeakyRig.leaked += 1
                self._send()
            return super().teardown()

    reads: list[str] = []
    real = Store.post_run_proofs

    def recorded(self, run_id):
        reads.append(run_id)
        return real(self, run_id)

    monkeypatch.setattr(pipeline_run, "SimulatedRig", LeakyRig)
    monkeypatch.setattr(Store, "post_run_proofs", recorded)
    r = run_pipeline(EXAMPLE, clean_scenario(date(2026, 11, 10)), tmp_path)
    assert reads and set(reads) == {r.run_id}                           # the report's proof comes from the store
    with Store(tmp_path / "sentinel.db") as store:
        rows = real(store, r.run_id)
        aliases = {x[0] for x in store.conn.execute("SELECT login_alias FROM logins")}
    assert [p.shard_id for p in rows] == [s["shard_id"] for s in r.shards] and len(rows) == 4
    (sent,) = [p for p in rows if p.verdict == "orders_sent"]
    assert sent.counts["undeclared"] == ["sim-order-1"] and sent.cancel_all == "performed_verified"
    assert {p.verdict for p in rows if p is not sent} == {"proven_no_sends"}
    # paper mode is asserted at the gate and again before every unit of work (6.4 Schedule, flow_boundary)
    assert all(p.login_alias in aliases and p.send_policy_ref == "OPEN" and p.paper_mode_assertions > 1
               and p.start_snapshot_sha256 and p.end_snapshot_sha256 for p in rows)
    line = proof_line(sent.verdict, sent.reason, sent.counts["undeclared"], sent.cancel_all)
    assert line == r.proof == "ORDERS SENT: undeclared rows (sim-order-1); Cancel All verified"
    assert f"Post-run proof: {line}" in r.comment and f"Cancel All verified, proof {line}; shard " in r.comment
    assert re.search(r"an order reached the account: login rig-0\d paper login blocked and rig disabled", r.comment)
    assert "vault" not in r.comment + (tmp_path / "report.html").read_text()


def test_the_nightly_shard_writes_its_proof(tmp_path):
    n = run_nightly(EXAMPLE, NightlyScenario("nightly", "run-2026-11-10-nightly-01", SimBuild(BASE_ID),
                                             overlay=dict([_main_build()]), mutants=("M01",)), tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        (p,) = store.post_run_proofs(n.run_id)
        shard = store.conn.execute("SELECT state, shard_outcome FROM shards WHERE shard_id = ?", (p.shard_id,)).fetchone()
    assert (p.verdict, p.cancel_all, tuple(shard)) == ("proven_no_sends", "not_needed", ("imported", "complete"))
    assert n.status == "complete"
