"""The plaintext secret scan (6.9, 8.4, 8.9): the teardown scan over SQLite rows, the artifact set and the report
files, the forms it searches, the mid-run upload check, and the unavailable case. Synthetic data only."""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from sentinel.records import StepEvent
from sentinel.store import ArtifactStore, RunPlan, ScopeEntry, ShardPlan, Store, connect_reader
from sentinel.store import secret_scan
from sentinel.store.secret_scan import SecretScanner, check_upload, scan_run

RUN = "run-01"
SHARD = f"{RUN}/1"
WS = "ws.tier-a.active-trader"
CID = f"census/trade.active-trader.ladder/{WS}"
TS = "2026-11-09T14:00:00+00:00"
MARKER = "canary-Hx7#q2Zp"            # the secret-scan canary's marker string (6.9 Proof)


def plan(run_id=RUN):
    return RunPlan(run_id=run_id, run_type="nightly", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=(ScopeEntry(CID, WS, "active_trader", 1, "frozen"),),
                   shards=(ShardPlan(f"{run_id}/1", "rig-a1"),))


def event(run_id=RUN, seq=1, text="hello"):
    return StepEvent(run_id=run_id, shard_id=f"{run_id}/1", side="candidate", seq=seq, source="orchestrator",
                     wall_at=TS, mono_ms=100 * seq, mode="scripted", action="type", target_text=text)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "run.db")
    s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(plan())
    yield s
    s.close()


@pytest.fixture
def arts(tmp_path):
    return ArtifactStore(tmp_path / "artifacts" / "runs" / RUN, f"runs/{RUN}")


def test_the_canary_marker_in_a_row_a_log_line_and_a_transcript_is_found_three_times(store, arts, tmp_path):
    store.write_events([event(seq=1), event(seq=2, text=f"typed {MARKER} by mistake")])
    arts.put(f"12:00:01 login ok\n12:00:02 pw={MARKER}\n".encode(), name="logs/slice.txt", kind="log_slice",
             media_type="text/plain")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "agent-1.jsonl").write_text(json.dumps({"role": "user", "text": f"the password is {MARKER}"}))
    secret = bytearray(MARKER.encode())
    res = scan_run(store, arts, [transcripts], {"login.password": secret}, run_id=RUN)
    assert (res.passed, res.run_status, res.run_reason) == (False, "aborted_harness", "secret_leaked")
    assert res.unreached.outcome == "harness_error" and res.unreached.reason == "secret_leaked"
    assert set(res.actions) == {"withhold_report", "rotate_credential"} and res.incident == "S1"
    places = sorted({h["location"] for h in res.evidence["hits"]})
    assert len(places) == 3
    assert places[0].startswith("db:step_events[") and places[0].endswith("].target_text")
    log = arts.entries[0]["sha256"]
    assert places[1] == f"file:{arts.path_of(log)}" and places[2] == f"file:{transcripts / 'agent-1.jsonl'}"
    assert [(f.kind, f.severity) for f in res.findings] == [("secret_leaked", "S1")]
    assert "3 place(s)" in res.findings[0].description
    assert MARKER not in json.dumps([res.evidence, res.detail, [f.description + (f.detail or "") for f in res.findings]])
    assert secret == bytearray(len(MARKER))                               # the buffer is zeroed when the scan ends


def test_a_clean_run_passes_and_other_runs_rows_are_not_this_runs(store, arts, tmp_path):
    store.write_events([event(seq=1)])
    store.register_rig("rig-b1", login_alias="paper-02", owner="qa", snapshot_id="snap-1", allowlist_entry=2)
    other = plan("run-02")
    store.begin_run(RunPlan(**{**other.__dict__, "shards": (ShardPlan("run-02/1", "rig-b1"),)}))
    store.write_events([event("run-02", 1, f"{MARKER}")])
    arts.put(b"nothing to see\n", name="logs/a.txt", kind="log_slice", media_type="text/plain")
    res = scan_run(store, arts, [], {"login.password": MARKER}, run_id=RUN)
    assert res.passed and res.run is None and res.evidence["rows"] >= 2 and res.evidence["files"] >= 1
    assert scan_run(store, arts, [], {}, run_id=RUN).passed                  # no secret used: nothing to find


def test_a_read_connection_serves_as_the_database(store, arts):
    store.write_events([event(seq=1, text=MARKER)])
    reader = connect_reader(store.path)
    try:
        res = scan_run(reader, arts, [], {"login.password": MARKER}, run_id=RUN)
    finally:
        reader.close()
    assert res.run_reason == "secret_leaked"


@pytest.mark.parametrize("payload, form", [
    (MARKER.encode("utf-16-le"), "utf-16-le"),
    (b"\xfe\xff" + MARKER.encode("utf-16-be"), "utf-16-be"),
    (json.dumps({"v": 'p"w\\' + MARKER}).encode(), "json"),
    (f"<td>{MARKER}&amp;</td>".encode(), "utf-8"),
])
def test_encodings_and_escapes_are_searched(payload, form, tmp_path):
    secret = 'p"w\\' + MARKER if form == "json" else MARKER
    f = tmp_path / "blob.bin"
    f.write_bytes(payload)
    hits = SecretScanner({"login.password": secret}).scan_file(f, "file:blob")
    assert form in {h.form for h in hits}


def test_html_and_unicode_escapes_are_searched():
    secret = "pässw<rd>"
    s = SecretScanner({"login.password": secret})
    assert {h.form for h in s.scan_bytes(b"<p>p&#228;ssw&lt;rd&gt;</p> p\\u00e4ssw<rd>", "x")} == {"json"}
    assert {h.form for h in s.scan_bytes("pässw&lt;rd&gt;".encode(), "x")} == {"html"}
    assert {h.form for h in s.scan_text(json.dumps(secret), "db")} == {"json"}


def test_a_hit_across_a_chunk_boundary_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "CHUNK", 16)
    f = tmp_path / "big.log"
    f.write_bytes(b"x" * 13 + MARKER.encode() + b"y" * 40)
    assert SecretScanner({"login.password": MARKER}).scan_file(f, "file:big")


def test_images_are_left_to_the_redaction_stage(arts, store):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    (arts.objects / "ff").mkdir()
    (arts.objects / "ff" / "img.png").write_bytes(buf.getvalue() + MARKER.encode())
    assert scan_run(store, arts, [], {"login.password": MARKER}, run_id=RUN).passed


def test_a_scan_that_cannot_run_or_finish_is_secret_scan_unavailable(store, arts, tmp_path):
    res = scan_run(store, arts, [], None, run_id=RUN)
    assert (res.run_status, res.run_reason, res.actions) == ("aborted_harness", "secret_scan_unavailable",
                                                             ("withhold_report",))
    gone = scan_run(store, arts, [tmp_path / "no-such-report.html"], {"login.password": MARKER}, run_id=RUN)
    assert gone.run_reason == "secret_scan_unavailable" and "FileNotFoundError" in gone.detail


def test_a_hit_quarantines_the_set_when_a_quarantine_root_is_given(store, arts, tmp_path):
    arts.put(f"pw={MARKER}".encode(), name="logs/leak.txt", kind="log_slice", media_type="text/plain")
    res = scan_run(store, arts, [], {"login.password": MARKER}, run_id=RUN, quarantine_root=tmp_path / "quarantine")
    assert res.evidence["quarantined_to"] == str(tmp_path / "quarantine" / RUN)
    assert arts.quarantined is not None and list((tmp_path / "quarantine" / RUN / "objects").rglob("*.txt"))


def test_the_upload_check_and_the_put_hook_stop_a_leak_mid_run():
    s = SecretScanner({"login.password": MARKER, "vault.token": "tok-998877"})
    assert check_upload(s, b'{"rows": []}\n', location="results batch 3") is None
    res = check_upload(s, f'{{"text": "{MARKER}"}}\n'.encode(), location="results batch 4")
    assert res.run_reason == "secret_leaked" and res.evidence["hits"][0]["location"] == "results batch 4"
    assert s.refs_in(b"tok-998877 and " + MARKER.encode("utf-16-le")) == ("login.password", "vault.token")


def test_secrets_are_named_and_never_empty():
    with pytest.raises(ValueError, match="empty"):
        SecretScanner({"login.password": ""})
    with pytest.raises(ValueError, match="vault reference"):
        SecretScanner({"": "x"})
