"""The harness egress policy (6.10, 8.3 egress_log, 4.15 L-XREF-19): the signed allowlist, the gate's refused,
sent and failed rows and findings, the Nightly reconciliation, and the lint rule. No network: `send` is a stub."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from sentinel.egress import (AllowlistError, EgressAllowlist, EgressGate, EgressRefused, FirewallEntry,
                             allowlist_problems, reconcile)
from sentinel.lint import lint, lint_repo
from sentinel.lint.repo import Doc, Repo
from sentinel.records import EgressRow
from sentinel.store import RunPlan, ScopeEntry, Store

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
RUN = "run-01"
CID = "census/trade.active-trader.ladder/ws.tier-a.active-trader"
MODEL_HOST = "api.anthropic.com"
DOC = {"schema_version": 1, "signed_by": "sec-ann", "signed_at": "2026-11-01T15:00:00Z", "signature": "pgp:abc123",
       "hosts": [{"host": MODEL_HOST, "purposes": ["review", "batch_review", "model_check"]},
                 {"host": "slack.com", "purposes": ["slack"]},
                 {"host": "jira.example.internal", "port": 8443, "purposes": ["jira"]}]}
ALLOW = EgressAllowlist.from_dict(DOC)


class Response:
    status_code = 200
    headers = {"request-id": "req_42"}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.begin_run(RunPlan(RUN, "pr", "c0ffee", "a" * 64, "b" * 64, "d00d", "auth/v1.md", 2700,
                        scope=(ScopeEntry(CID, "ws.tier-a.active-trader", "active_trader", 1, "frozen"),)))
    yield s
    s.close()


def rows(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM egress_log ORDER BY egress_id")]


# -- the allowlist (4.16) ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host, port, purposes, message", [
    ("*.anthropic.com", 443, ["review"], "exact host"),
    ("https://api.anthropic.com", 443, ["review"], "exact host"),
    ("api.anthropic.com:443", 443, ["review"], "exact host"),
    ("api.anthropic.com/v1", 443, ["review"], "exact host"),
    ("api.anthropic.[a-z]+", 443, ["review"], "exact host"),
    ("api.anthropic.com", 443, ["upload"], "egress_log purposes"),
    ("api.anthropic.com", 443, [], "purposes"),
    ("api.anthropic.com", 70000, ["review"], "TCP port"),
])
def test_the_allowlist_names_exact_hosts_with_known_purposes(host, port, purposes, message):
    doc = copy.deepcopy(DOC)
    doc["hosts"].append({"host": host, "port": port, "purposes": purposes})
    assert any(message in m for _, m in allowlist_problems(doc))
    with pytest.raises(AllowlistError, match=message):
        EgressAllowlist.from_dict(doc)


def test_the_allowlist_is_signed_and_lists_a_host_once():
    doc = copy.deepcopy(DOC)
    doc["signature"] = " "
    doc["hosts"].append({"host": "API.anthropic.com.", "purposes": ["review"]})
    problems = allowlist_problems(doc)
    assert ("signature", "the allowlist carries signature: it is signed by the security contact (6.10)") in problems
    assert any("listed twice" in m for _, m in problems)
    assert allowlist_problems(DOC) == [] and allowlist_problems([]) == [("", "the allowlist is a mapping (4.16)")]


def test_refusal_reasons():
    assert ALLOW.refusal("API.Anthropic.com.", "review") is None
    assert ALLOW.refusal("collector.invalid", "review") == "destination not allowlisted: collector.invalid"
    assert ALLOW.refusal("jira.example.internal", "jira") == "port 443 not allowlisted for jira.example.internal"
    assert ALLOW.refusal("jira.example.internal", "jira", 8443) is None
    assert ALLOW.refusal("slack.com", "review") == "purpose review not allowlisted for slack.com"
    assert EgressAllowlist.load(EXAMPLE / "policy" / "egress_allowlist.yaml").refusal(MODEL_HOST, "review") is None


# -- the gate ------------------------------------------------------------------------------------------------------

def test_a_host_off_the_allowlist_is_refused_before_connecting_with_a_row_and_one_finding(store):
    gate = EgressGate(ALLOW, store, run_id=RUN, shard_id=f"{RUN}/1")
    sent = []
    for _ in range(2):
        with pytest.raises(EgressRefused) as e:
            gate.request("collector.invalid", lambda: sent.append(1), purpose="review", nbytes=2048)
        assert e.value.reason == "destination not allowlisted: collector.invalid"
    assert sent == []                                                       # nothing was sent
    got = rows(store)
    assert [(r["status"], r["destination"], r["purpose"], r["bytes"], r["run_id"]) for r in got] == \
        [("refused", "collector.invalid", "review", 2048, RUN)] * 2
    assert got[0]["refusal_reason"] == "destination not allowlisted: collector.invalid"
    findings = store.findings(RUN)
    assert [(f.kind, f.severity, f.detail) for f in findings] == [("harness_error", "S3", "egress_refused")]
    assert gate.findings == findings


def test_an_allowed_request_writes_one_sent_row_with_the_context_fields(store):
    gate = EgressGate(ALLOW, store, run_id=RUN, shard_id=f"{RUN}/1")
    with gate.context(checkpoint_id=CID, artifact_sha256="c" * 64, redaction_version="redaction-1-abc"):
        with gate.default_context(purpose="review", model_id="claude-opus-5-5"):
            r = gate.request(MODEL_HOST, Response, nbytes=900, image=True, status_of=lambda x: x.status_code,
                             request_id_of=lambda x: x.headers["request-id"])
    assert isinstance(r, Response)
    (row,) = rows(store)
    assert (row["status"], row["response_status"], row["request_id"], row["bytes"], row["purpose"]) == \
        ("sent", 200, "req_42", 900, "review")
    assert (row["checkpoint_id"], row["artifact_sha256"], row["redaction_version"], row["model_id"]) == \
        (CID, "c" * 64, "redaction-1-abc", "claude-opus-5-5")


def test_an_outer_purpose_wins_over_a_transport_default(store):
    gate = EgressGate(ALLOW, store, run_id=RUN)
    with gate.context(purpose="model_check"), gate.default_context(purpose="review"):
        gate.request(MODEL_HOST, Response)
    assert rows(store)[0]["purpose"] == "model_check"
    with pytest.raises(ValueError, match="purpose"):
        gate.request(MODEL_HOST, Response)
    with pytest.raises(ValueError, match="unknown egress row fields"):
        with gate.context(colour="red"):
            pass


def test_a_request_without_a_response_is_failed(store):
    gate = EgressGate(ALLOW, store, run_id=RUN)

    def down():
        raise ConnectionError("reset")
    with pytest.raises(ConnectionError):
        gate.request(MODEL_HOST, down, purpose="review", nbytes=10)
    assert [(r["status"], r["response_status"]) for r in rows(store)] == [("failed", None)]


@pytest.mark.parametrize("host, purpose, fields, reason", [
    (MODEL_HOST, "review", {"image": True}, "redaction version missing"),
    ("slack.com", "slack", {"image": True, "redaction_version": "redaction-1-abc"},
     "images never go to the slack host"),
])
def test_images_need_a_redaction_version_and_never_go_to_slack_or_the_pr_host(store, host, purpose, fields, reason):
    gate = EgressGate(ALLOW, store, run_id=RUN)
    with pytest.raises(EgressRefused, match=reason):
        gate.request(host, Response, purpose=purpose, **fields)
    assert rows(store)[0]["status"] == "refused"


def test_a_gate_without_a_run_keeps_its_finding_and_logs_the_row():
    class Sink:
        def __init__(self):
            self.rows, self.findings = [], []

        def log_egress(self, row):
            self.rows.append(row)
            return len(self.rows)

        def write_finding(self, finding, *, synthetic=False):
            self.findings.append(finding)
    sink = Sink()
    gate = EgressGate(ALLOW, sink)
    with pytest.raises(EgressRefused):
        gate.request("hooks.slack.example", Response, purpose="digest")
    assert sink.rows[0].run_id is None and sink.rows[0].status == "refused" and sink.findings == []
    assert [f.kind for f in gate.findings] == ["harness_error"]


# -- Nightly reconciliation ----------------------------------------------------------------------------------------

def row(host, at, status="sent", run="run-01"):
    return EgressRow(destination=host, purpose="review", status=status, bytes=1, sent_at=at, run_id=run,
                     refusal_reason="x" if status == "refused" else None)


def fw(host, at, action="permitted", harness=True, run="run-01"):
    return FirewallEntry(at=at, host=host, action=action, harness=harness, run_id=run, process="python.exe")


def test_reconciliation_clean_day():
    rec = reconcile([row(MODEL_HOST, "2026-11-10T03:00:00+00:00")], ALLOW,
                    [fw(MODEL_HOST, "2026-11-10T03:00:30+00:00"), fw("login.example", "2026-11-10T03:01:00Z",
                                                                     harness=False)])
    assert rec.incident is None and rec.runs == () and rec.verdict is None and rec.control().passed


def test_a_sent_row_to_an_unapproved_host_and_an_unlogged_connection_are_s1():
    rows_ = [row(MODEL_HOST, "2026-11-10T03:00:00+00:00"), row("paste.example", "2026-11-10T03:05:00+00:00"),
             row(MODEL_HOST, "2026-11-10T04:00:00+00:00", status="refused", run="run-03")]
    log = [fw(MODEL_HOST, "2026-11-10T03:00:59+00:00"),                     # within the window: logged
           fw(MODEL_HOST, "2026-11-10T03:02:00+00:00", run="run-02"),       # no row for run-02
           fw(MODEL_HOST, "2026-11-10T04:00:00+00:00", run="run-03"),       # only a refused row: unlogged
           fw("pypi.org", "2026-11-10T05:00:00+00:00", action="blocked")]
    rec = reconcile(rows_, ALLOW, log)
    assert [r.destination for r in rec.unapproved_sent] == ["paste.example"]
    assert [e.run_id for e in rec.unlogged] == ["run-02", "run-03"] and [e.host for e in rec.blocked] == ["pypi.org"]
    assert rec.runs == ("run-01", "run-02", "run-03") and rec.incident == "S1"
    assert (rec.verdict.status, rec.verdict.reason) == ("aborted_harness", "egress_unlogged")
    assert sorted((f.severity, f.detail) for f in rec.findings) == [
        ("S1", "egress_unapproved_host"), ("S1", "egress_unlogged"), ("S1", "egress_unlogged"),
        ("S3", "firewall_block")]
    ctl = rec.control()
    assert not ctl.passed and ctl.incident == "S1" and ctl.has("withhold_report")
    assert ctl.alert.rule.startswith("safety")


def test_a_firewall_block_alone_is_s3():
    rec = reconcile([], ALLOW, [fw("pypi.org", "2026-11-10T05:00:00+00:00", action="blocked")])
    assert rec.incident == "S3" and rec.runs == () and not rec.control().passed
    with pytest.raises(ValueError):
        FirewallEntry(at="2026-11-10T05:00:00Z", host="x", action="dropped", harness=True)


# -- L-XREF-19 -----------------------------------------------------------------------------------------------------

def xref19(repo):
    return [v for v in lint_repo(repo, today=TODAY).violations if v.rule == "L-XREF-19"]


def test_the_example_allowlist_is_clean_and_the_rule_is_no_longer_pending():
    report = lint(EXAMPLE, today=TODAY)
    assert "L-XREF-19" not in report.skipped and "L-XREF-19" not in report.rules()


def test_l_xref_19_reports_patterns_purposes_and_the_signer():
    repo = Repo.load(EXAMPLE)
    doc = repo.docs["policy/egress_allowlist.yaml"].data
    doc["hosts"].append({"host": "*.example.internal", "purposes": ["upload"]})
    repo.docs["ops/oncall.yaml"] = Doc("ops/oncall.yaml", "oncall", {"roles": [
        {"role": "security_contact", "person": "sec-ann", "deputy": "sec-bob"}]})
    msgs = [v.message for v in xref19(repo)]
    assert any("exact host name" in m for m in msgs) and any("'upload'" in m for m in msgs)
    assert any("is not the security contact" in m for m in msgs)


def test_l_xref_19_ties_the_model_host_to_the_ruling_and_covers_the_reporting_hosts():
    repo = Repo.load(EXAMPLE)
    ruling = {"status": "ruled", "endpoint": "https://claude.example.internal"}
    repo.docs["policy/computer_use_ruling.yaml"] = Doc("policy/computer_use_ruling.yaml", "computer_use_ruling", ruling)
    repo.docs["policy/reporting.yaml"] = Doc("policy/reporting.yaml", "reporting",
                                             {"jira": {"product_project": "TOS", "harness_project": "SEN"},
                                              "slack": {"on_call": "#a", "triage": "#b", "release": "#c",
                                                        "safety": "#sentinel-safety"}})
    doc = repo.docs["policy/egress_allowlist.yaml"].data
    doc["hosts"] = [h for h in doc["hosts"] if "slack" not in h["purposes"]]
    msgs = [v.message for v in xref19(repo)]
    assert any("claude.example.internal of policy/computer_use_ruling.yaml is not on the allowlist" in m for m in msgs)
    assert any("api.anthropic.com carries a model purpose but is not the ruling's endpoint" in m for m in msgs)
    assert any("posts to slack" in m for m in msgs) and not any("posts to jira" in m for m in msgs)
    repo.docs["policy/computer_use_ruling.yaml"].data["endpoint"] = MODEL_HOST
    assert not any("endpoint" in v.message for v in xref19(repo))
