"""Wave-1 review fixes in the secrets area (review-w1 `secrets:*`): the egress gate (6.10), the redaction of what
the Reviewer is sent (3.6, 6.10), the per-set artifact layout and quarantine (8.4) and the plaintext scan (6.9).
Every test fails on the code before its fix; the network is a mock transport throughout."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path

import httpx2

from sentinel.demo import EXAMPLE, clean_scenario
from sentinel.pipeline.run import run_pipeline
from sentinel.reviewer import AnthropicTransport, ReviewerConfig, ReviewerJob
from sentinel.store import Store

TODAY = date(2026, 11, 10)
KEY = {"ANTHROPIC_API_KEY": "sk-test"}


def _config_without(tmp_path: Path, *rel: str) -> Path:
    cfg = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, cfg)
    for r in rel:
        (cfg / r).unlink()
    return cfg


def _recording(seen: list) -> httpx2.MockTransport:
    def handler(request):
        seen.append(request.url.host)
        return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "x"}})
    return httpx2.MockTransport(handler)


# == secrets:0: a config without an egress allowlist refuses every host ========================================

def test_secrets_0_a_run_without_an_allowlist_refuses_and_logs_every_reviewer_request(tmp_path):
    """6.10, L-XREF-19 choice 1: with no `policy/egress_allowlist.yaml` the harness HTTP client refuses every host;
    the live Reviewer still gets the run's gate, each request is refused before connecting and logged `refused`."""
    cfg = _config_without(tmp_path, "policy/egress_allowlist.yaml")
    gates, sent = [], []

    def factory(gate):
        gates.append(gate)
        t = AnthropicTransport.from_env(allow_network=True, env=KEY, gate=gate, http_transport=_recording(sent))
        return ReviewerJob(ReviewerConfig(sync_concurrency=1), t, "pr")

    sc = clean_scenario(TODAY)
    r = run_pipeline(cfg, sc, tmp_path / "out", today=TODAY, live_factory=factory)
    assert gates and gates[0] is not None and r.reviewer_calls
    assert sent == []                                           # nothing reached the network
    db = sqlite3.connect(tmp_path / "out" / "sentinel.db")
    rows = db.execute("SELECT destination, status, refusal_reason FROM egress_log WHERE run_id = ?",
                      (sc.run_id,)).fetchall()
    assert rows and {(d, s) for d, s, _ in rows} == {("api.anthropic.com", "refused")}
    assert all("not allowlisted" in why for _, _, why in rows)
    with Store(tmp_path / "out" / "sentinel.db") as store:
        refused = [(f.kind, f.severity) for f in store.findings(sc.run_id) if f.detail == "egress_refused"]
    assert refused == [("harness_error", "S3")]


def test_secrets_0_the_nightly_reconciliation_without_an_allowlist_flags_any_sent_row(tmp_path):
    """6.10: with no allowlist the Nightly reconciliation still runs, against the empty allowlist, so a `sent` row is
    to a host outside it (S1) instead of the check being skipped."""
    from datetime import datetime, timezone

    from sentinel.pipeline.run import nightly_reconcile
    from sentinel.records import EgressRow
    cfg = _config_without(tmp_path, "policy/egress_allowlist.yaml")
    now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    with Store(tmp_path / "sentinel.db") as store:
        store.log_egress(EgressRow(destination="api.anthropic.com", purpose="model_check", status="sent", bytes=10,
                                   sent_at="2026-11-10T08:00:00+00:00"))
        result = nightly_reconcile(store, cfg, now)
    assert result is not None and not result.passed and result.incident == "S1"
