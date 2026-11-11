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


# == secrets:1: `sentinel review --live` goes through the run's egress gate ======================================

def _no_network(monkeypatch) -> list:
    """Record every request the SDK's network transport would send, and answer none of them."""
    seen: list = []

    def handle(self, request):
        seen.append(request.url.host)
        raise httpx2.ConnectError("no network in tests", request=request)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr("anthropic._base_client.time.sleep", lambda s: None)
    return seen


def test_secrets_1_review_live_sends_through_the_runs_gate_and_logs_each_request(tmp_path, monkeypatch):
    """6.10: `sentinel review --live` reviews the items inside the run, through the run's gate: an endpoint off the
    allowlist is refused before connecting, each refusal is an `egress_log` row naming the run and checkpoint, and
    the refusal's `harness_error` finding lands on the run."""
    from sentinel.cli import main as cli
    seen = _no_network(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = tmp_path / "rc.yaml"
    rc.write_text("reviewer:\n  endpoint_base_url: https://exfil.attacker.example\n  sync_concurrency: 1\n")
    assert cli(["review", "--live", "clean-pr", "--out", str(tmp_path), "--reviewer-config", str(rc)]) == 0
    assert seen == []
    out = tmp_path / "review-clean-pr"
    run_id = clean_scenario(TODAY).run_id
    db = sqlite3.connect(out / "sentinel.db")
    rows = db.execute("SELECT run_id, checkpoint_id, destination, status, redaction_version FROM egress_log").fetchall()
    assert rows and {(r[0], r[2], r[3]) for r in rows} == {(run_id, "exfil.attacker.example", "refused")}
    assert all(r[1] and r[4] for r in rows)                    # the item's checkpoint and redaction version
    with Store(out / "sentinel.db") as store:
        assert [f.kind for f in store.findings(run_id) if f.detail == "egress_refused"] == ["harness_error"]
    assert (out / "review-live.json").exists()


def test_secrets_1_a_live_transport_without_a_gate_refuses_every_request(monkeypatch, caplog):
    """6.10, egress choice 6: a client built without the run's gate is never ungated; it refuses every request before
    connecting, and the refusal is logged."""
    import logging

    from sentinel.reviewer import TransportError
    seen = _no_network(monkeypatch)
    t = AnthropicTransport.from_env(allow_network=True, env=KEY)
    params = {"model": "claude-opus-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    with caplog.at_level(logging.ERROR, logger="sentinel.egress"):
        try:
            t.create_message(params)
        except TransportError as e:
            assert e.kind == "client" and "egress refused" in str(e)
        else:
            raise AssertionError("an ungated request was not refused")
    assert seen == [] and "refused to api.anthropic.com" in caplog.text


# == secrets:2: the sweep looks for the test login's display name =============================================

def _name_frame(name: str):
    """A dark frame with the account holder's two-word name drawn where no anchored redaction box sits, and its
    OCR."""
    import numpy as np

    from sentinel.capture import SyntheticOcr
    from sentinel.diff.regions import Rect
    img = np.full((120, 400, 3), 18, dtype=np.uint8)
    first, last = name.split()
    img[42:50, 30:70:3] = 230                                  # glyph-like strokes, not a filled box
    img[42:50, 76:160:3] = 230
    return img, SyntheticOcr([(first, Rect(30, 40, 40, 12)), (last, Rect(76, 40, 84, 12))], (400, 120))


def test_secrets_2_the_runs_sweep_finds_the_test_logins_display_name(tmp_path, monkeypatch):
    """3.6, 6.10 stage 2: the run's sweep matches the test login's display name (from the vault's login record; the
    4.9 fixture has no name field), so a name outside the anchors is blacked out and is a `redaction_miss`."""
    import sentinel.pipeline.run as pr
    from sentinel.capture import RedactionList, redact_pair
    sweeps = []
    real = pr.redact_pair

    def redact(base, cand, anchors, ocr, theme, *, sweep=None, context=None, params=None):
        sweeps.append(sweep)
        return real(base, cand, anchors, ocr, theme, sweep=sweep, context=context, params=params)
    monkeypatch.setattr(pr, "redact_pair", redact)
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, today=TODAY)
    assert r.status == "complete" and sweeps
    name = sweeps[0].display_name
    assert name and len(name.split()) == 2                   # the vault stand-in's name reaches the sweep
    img, ocr = _name_frame(name)
    res = redact_pair(img, img.copy(), RedactionList(), ocr, (18, 18, 18), sweep=sweeps[0])
    assert {h.kind for h in res.hits} == {"display_name"}
    assert [f.kind for f in res.findings] == ["redaction_miss"]
    assert (res.candidate[40:52, 30:160] == 18).all()


def test_secrets_2_a_run_with_no_display_name_for_the_sweep_does_not_start(tmp_path, monkeypatch):
    """6.10 "always": without a display name the sweep cannot be built, and the run ends `aborted_harness (reason:
    config_invalid)` before any capture instead of sweeping without it."""
    import sentinel.pipeline.run as pr
    monkeypatch.setattr(pr, "login_display_name", lambda rig: None)
    r = run_pipeline(EXAMPLE, clean_scenario(TODAY), tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    assert not any(o.artifacts for o in r.outcomes)
    with Store(tmp_path / "sentinel.db") as store:
        assert "display name" in " ".join(store.run_summary(r.run_id)["lint_errors"])


# == secrets:3: the Reviewer item's text is redacted like its pixels ===========================================

def test_secrets_3_an_identifier_blacked_out_in_pixels_is_not_sent_as_text(tmp_path, monkeypatch):
    """6.10 stage 3, 6.12 S1: the redaction-miss scenario draws an account id on the Order Confirmation; the stage
    fills it, and the Reviewer item built for that checkpoint carries it neither in `ocr_text` nor in the prompt."""
    import sentinel.pipeline.run as pr
    from sentinel.demo import DEFAULT_TODAY, PR_SCENARIOS
    from sentinel.pipeline.live import ItemBuilder
    from sentinel.pipeline.reviewer import ReviewRequest
    from sentinel.reviewer.prompt import item_text
    saved = {}

    class Spy(ItemBuilder):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            saved["b"] = self
    monkeypatch.setattr(pr, "ItemBuilder", Spy)
    pr.run_pipeline(EXAMPLE, PR_SCENARIOS["redaction-miss"](DEFAULT_TODAY), tmp_path, DEFAULT_TODAY)
    b = saved["b"]
    cid = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
    ev = b.evals[cid]
    assert [f.kind for f in ev.redaction.findings] == ["redaction_miss"]
    assert any(w.text == "D-20481134" for w in ev.cand.model.words)          # drawn, and filled in the pixels
    req = ReviewRequest("describe_collateral", (cid,), ev.cp.surface_id, None, ev.cp.tier, ev.cp.data_mode,
                        ev.cp.workspace_id, {"changed_px": 10}, (), (0, 0, 1, 1),
                        edge={"primitive": "p", "entry_id": "e"})
    item = b(req)
    assert item.ocr_text and "D-20481134" not in item.ocr_text
    assert "D-20481134" not in item_text(item, [])


def test_secrets_3_split_identifiers_and_the_title_are_scrubbed():
    """6.10: a phone number split into words, a word under a filled box, and a display name in the window title
    are not sent; other words are."""
    from types import SimpleNamespace as NS

    from sentinel.capture import Sweep
    from sentinel.diff.regions import Rect as RRect
    from sentinel.invariants import Rect, WordBox
    from sentinel.pipeline.live import REDACTED, ItemBuilder

    def word(text, x):
        return WordBox(text, Rect(x, 100, 30, 10), 95.0)
    words = [word("Call", 10), word("555", 50), word("867", 90), word("5309", 130), word("now", 170),
             word("hidden", 210), word("Buy", 250)]
    ev = NS(cand=NS(model=NS(words=words), screen=NS(components=[
        NS(kind="titlebar", text="thinkorswim - Wren Halvorsen-Tate - Main", bbox=(0, 0, 400, 20))])),
        redaction=NS(boxes=[NS(rect=RRect(205, 95, 40, 20))]))
    b = ItemBuilder.__new__(ItemBuilder)
    b.matchers = [m for _, m in Sweep.for_login(("D-20481133",), "Wren Halvorsen-Tate").matchers()]
    assert b._visible_words(ev, (0, 0, 400, 400)) == ["Call", "now", "Buy"]
    assert b._title(ev) == f"thinkorswim - {REDACTED} - Main"
    ev.redaction = NS(boxes=[NS(rect=RRect(300, 2, 80, 12))])                # a filled box on the title bar
    assert b._title(ev) == REDACTED


# == secrets:4: every run's artifacts are their own set, and quarantine moves only that set ======================

def _object_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file() and p.suffix == ".png"}


def test_secrets_4_one_runs_quarantine_leaves_the_other_runs_artifacts_alone(tmp_path, monkeypatch):
    """8.4: each run's set lives in its own directory and quarantine/<run_id> outside all of them, so a leak in one
    run moves only that run's objects; an earlier run's objects stay where its rows say, and later runs store."""
    import sentinel.pipeline.run as pr
    from sentinel.store import ArtifactStore
    one, two = clean_scenario(TODAY), clean_scenario(TODAY, seq=2)
    r1 = run_pipeline(EXAMPLE, one, tmp_path, today=TODAY)
    assert r1.status == "complete"
    before = _object_files(tmp_path / "artifacts")
    assert before
    monkeypatch.setattr(pr, "SIM_SECRET", f"{two.run_id}/1")              # a value run 2's records carry
    r2 = run_pipeline(EXAMPLE, two, tmp_path, today=TODAY)
    assert (r2.status, r2.reason) == ("aborted_harness", "secret_leaked")
    assert before <= _object_files(tmp_path / "artifacts") - _object_files(tmp_path / "artifacts" / "quarantine")
    with Store(tmp_path / "sentinel.db") as store:
        shas = {o.artifacts["candidate"] for o in store.outcomes(one.run_id) if o.artifacts}
    first = ArtifactStore.at(tmp_path / "artifacts", f"runs/{one.run_id}")
    assert shas and first.quarantined is None and all(first.verify(s) for s in shas)
    later = ArtifactStore.at(tmp_path / "artifacts", "runs/run-2026-11-10-pr-4823-03")
    later.put(b"{}", name="x.json", kind="request", media_type="application/json")   # not blocked by run 2's marker


def test_secrets_4_quarantine_refuses_a_quarantine_root_inside_the_set(tmp_path):
    """8.4: quarantine/ is a sibling of the sets; a quarantine root inside the set's directory is refused, nothing
    moved."""
    import pytest

    from sentinel.store import ArtifactStore
    from sentinel.store.artifacts import ArtifactError
    arts = ArtifactStore(tmp_path / "artifacts", "runs/r1")
    a = arts.put(b"{}", name="x.json", kind="request", media_type="application/json")
    with pytest.raises(ArtifactError, match="outside every set"):
        arts.quarantine("r1", tmp_path / "artifacts" / "quarantine")
    assert arts.quarantined is None and arts.verify(a.sha256)


# == secrets:8: a flagged results batch stops uploads at once ===================================================

def test_secrets_8_the_flagged_batch_and_every_later_one_are_not_uploaded(tmp_path, monkeypatch):
    """6.9: the Orchestrator scans each results batch before upload, "so a leak mid-run stops uploads at once": the
    flagged checkpoint and every checkpoint after it are written only as `harness_error (secret_leaked)`, with no
    findings, artifacts or steps, and the shards are not marked imported."""
    import sentinel.pipeline.run as pr
    from sentinel.store.secret_scan import SecretHit, leak_result
    seen = []
    real = pr.check_upload

    def check(scanner, data, *, location):
        seen.append(location)
        if len(seen) == 3:                                    # the third batch holds a plaintext
            return leak_result([SecretHit("vault://x#password", location, "utf-8")])
        return real(scanner, data, location=location)
    monkeypatch.setattr(pr, "check_upload", check)
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "secret_leaked") and len(seen) == 3
    uploaded = {loc.removeprefix("results batch ") for loc in seen[:2]}
    with Store(tmp_path / "sentinel.db") as store:
        outs = store.outcomes(sc.run_id)
        withheld = [o for o in outs if o.checkpoint_id not in uploaded]
        assert withheld and all((o.outcome, o.reason, o.artifacts) == ("harness_error", "secret_leaked", None)
                                for o in withheld)
        assert {o.checkpoint_id for o in outs if o.artifacts} == uploaded
        cps = {o.checkpoint_id for o in withheld}
        assert not [f for f in store.findings(sc.run_id) if f.checkpoint_id in cps]
        assert not store.query("SELECT 1 FROM shards WHERE run_id = ? AND state = 'imported'", (sc.run_id,))
    assert (tmp_path / "artifacts" / "quarantine" / sc.run_id).is_dir()   # 6.9: the run's objects are quarantined


# == secrets:6: the teardown scan covers the summary, the report's inputs and results.jsonl =========================

def _published(out: Path, secret: str) -> list[str]:
    return sorted(p.name for p in out.glob("*") if p.is_file() and p.suffix in (".md", ".html", ".json")
                  and secret.encode() in p.read_bytes())


def test_secrets_6_a_plaintext_only_in_the_report_inputs_withholds_the_report(tmp_path):
    """6.9: teardown scans the report HTML before `reported`; a plaintext that only the report would carry (here the
    detector banner) ends the run `aborted_harness (secret_leaked)` and nothing is published."""
    import dataclasses

    import sentinel.pipeline.run as pr
    sc = dataclasses.replace(clean_scenario(TODAY), detector_banner=f"Detector note: {pr.SIM_SECRET}")
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "secret_leaked")
    assert not (tmp_path / "report.html").exists() and not (tmp_path / "pr-comment.md").exists()
    assert _published(tmp_path, pr.SIM_SECRET) == []


def test_secrets_6_a_plaintext_in_the_run_summary_is_found(tmp_path):
    """6.9: `runs.summary_json` and its results.jsonl line are scanned too: a plaintext in a summary input (the
    canary note) ends the run `aborted_harness (secret_leaked)` with the report withheld."""
    import dataclasses

    import sentinel.pipeline.run as pr
    sc = dataclasses.replace(clean_scenario(TODAY), canary={"note": f"canary {pr.SIM_SECRET}"})
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "secret_leaked")
    assert not (tmp_path / "report.html").exists()
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_summary(sc.run_id)["secret_scan"]["report_withheld"] is True


def test_secrets_6_the_rendered_report_is_scanned_before_it_is_published(tmp_path, monkeypatch):
    """6.9: the rendered report files are scanned too; text that only they hold (here a renderer that adds it) is
    withheld: the comment and report page are removed and the summary records the scan."""
    import sentinel.pipeline.run as pr
    real = pr.render_report_markdown
    monkeypatch.setattr(pr, "render_report_markdown", lambda rep: real(rep) + f"\n{pr.SIM_SECRET}\n")
    sc = clean_scenario(TODAY)
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    assert not (tmp_path / "pr-comment.md").exists() and not (tmp_path / "report.html").exists()
    assert r.commit_status["state"] == "error" and _published(tmp_path, pr.SIM_SECRET) == []
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_summary(sc.run_id)["secret_scan"]["report_withheld"] is True


def _nightly(out: Path, run_id: str = "run-2026-11-10-nightly-01"):
    from sentinel.demo import BASE_ID, _main_build
    from sentinel.pipeline.run import NightlyScenario, run_nightly
    from sentinel.pipeline.sim import SimBuild
    return run_nightly(EXAMPLE, NightlyScenario("nightly", run_id, SimBuild(BASE_ID), overlay=dict([_main_build()]),
                                                mutants=("M01",)), out)


# == secrets:7: the Nightly run's teardown scans too ==========================================================

def test_secrets_7_a_plaintext_in_a_nightly_run_withholds_its_report(tmp_path, monkeypatch):
    """6.9: every run's teardown scans before `reported`, the Nightly's too: a plaintext its records carry ends it
    `aborted_harness (secret_leaked)` at teardown, its report and canary results are not published, and its
    artifact set is quarantined."""
    import sentinel.pipeline.run as pr
    run_id = "run-2026-11-10-nightly-01"
    monkeypatch.setattr(pr, "SIM_SECRET", f"{run_id}/1")                 # a value the Nightly's records carry
    n = _nightly(tmp_path, run_id)
    assert (n.status, n.reason) == ("aborted_harness", "secret_leaked")
    assert not list(tmp_path.glob("nightly-report-*.html")) and not list(tmp_path.glob("canary-results-*.json"))
    assert (tmp_path / "artifacts" / "quarantine" / run_id).is_dir()
    with Store(tmp_path / "sentinel.db") as store:
        assert store.run_summary(run_id)["secret_scan"]["report_withheld"] is True
        assert store.query("SELECT outcome FROM runs WHERE run_id = ?", (run_id,))[0][0] == "aborted_harness"


def test_secrets_7_a_clean_nightly_run_is_scanned_and_reports(tmp_path, monkeypatch):
    """6.9: the clean Nightly is scanned (its canary results through the upload check) and still reports."""
    import sentinel.pipeline.run as pr
    seen = []
    real = pr.check_upload

    def check(scanner, data, *, location):
        seen.append(location)
        return real(scanner, data, location=location)
    monkeypatch.setattr(pr, "check_upload", check)
    n = _nightly(tmp_path)
    assert n.status == "complete" and "canary results" in seen
    assert list(tmp_path.glob("nightly-report-*.html")) and list(tmp_path.glob("canary-results-*.json"))


# == secrets:9: a missing redaction list is config_invalid, never an empty stage 1 ===================================

def test_secrets_9_the_lint_refuses_a_config_without_its_redaction_list(tmp_path):
    """6.10 stage 1, 4.1: `redaction/anchors.yaml` is required; without it (or without an identity-field region in
    it) the lint reports an error, so the run-start lint aborts the run `config_invalid`."""
    from sentinel.lint import lint
    cfg = _config_without(tmp_path, "redaction/anchors.yaml")
    errors = [(v.rule, v.file) for v in lint(cfg, phase=1, today=TODAY).errors]
    assert ("L-MSK-03", "redaction/anchors.yaml") in errors
    r = run_pipeline(cfg, clean_scenario(TODAY), tmp_path / "out", today=TODAY)
    assert (r.status, r.reason) == ("aborted_harness", "config_invalid")
    shutil.copy(EXAMPLE / "redaction" / "anchors.yaml", cfg / "redaction" / "anchors.yaml")
    p = cfg / "redaction" / "anchors.yaml"
    p.write_text(p.read_text().replace("identity_field: true", "identity_field: false"))
    msgs = [v.message for v in lint(cfg, phase=1, today=TODAY).errors if v.file == "redaction/anchors.yaml"]
    assert any("identity_field" in m for m in msgs)


def test_secrets_9_a_run_never_falls_back_to_an_empty_redaction_list(tmp_path):
    """6.10: past the lint, the run itself refuses to build its redaction stage without the file."""
    from datetime import datetime, timezone

    import pytest

    import sentinel.pipeline.run as pr
    from sentinel.capture import RedactionError
    from sentinel.demo import BASE_ID
    from sentinel.pipeline.config import SentinelConfig
    from sentinel.pipeline.sim import SimBuild
    cfg = SentinelConfig.load(_config_without(tmp_path, "redaction/anchors.yaml"))
    with pytest.raises(RedactionError, match="anchors.yaml"):
        pr._Run(cfg, "run-x", "pr", SimBuild(BASE_ID), SimBuild(BASE_ID), datetime.now(timezone.utc), None, None,
                lambda s: "undeclared")


# == secrets:10: a text artifact that starts with "BM" is text ===============================================

def test_secrets_10_text_starting_with_bm_is_scanned_and_a_real_bmp_is_still_an_image(tmp_path):
    """6.9, 8.4: a CSV export or log slice whose first bytes are "BM" (a BMY quote) is a text artifact: the put hook
    scans it and the teardown scan reads it; a real BMP file is still recognised as an image."""
    import io

    import pytest
    from PIL import Image

    from sentinel.store import ArtifactStore
    from sentinel.store.artifacts import Redaction, SecretLeaked, has_image_magic, is_visual
    from sentinel.store.secret_scan import SecretScanner, scan_run
    secrets = {"vault://x#password": "hunter2-Pa55"}
    data = b"BMY,100,52.10\nnote,hunter2-Pa55\n"
    arts = ArtifactStore(tmp_path / "set", "runs/r1", scanner=SecretScanner(secrets))
    for header in (None, Redaction("redaction-1-x")):
        with pytest.raises(SecretLeaked):
            arts.put(data, name="export.csv", kind="export", media_type="text/csv", redaction=header)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "log_slice.txt").write_bytes(b"BMY quote refresh\nlogin typed hunter2-Pa55\n")
    res = scan_run(sqlite3.connect(":memory:"), ArtifactStore(tmp_path / "s2", "runs/r2"), [reports], secrets,
                   run_id="r2")
    assert res.run_reason == "secret_leaked" and res.evidence["files"] == 1
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="BMP")
    assert has_image_magic(buf.getvalue()[:32]) and is_visual(buf.getvalue(), "export", "application/octet-stream")
    assert not has_image_magic(data[:32])


# == secrets:11: a pair redacted without its base stores no base ===============================================

def test_secrets_11_an_unsettled_base_is_not_stored_as_a_copy_of_the_candidate(tmp_path):
    """8.4 `side`, 3.6: when the redaction stage ran on the candidate alone (the base did not settle), no artifact
    labelled `base` is stored; before, the redacted candidate was stored under side `base`."""
    import dataclasses

    from sentinel.pipeline.sim import SimBuild
    targets = frozenset(f"census/charts.flexible-grid.chart-cell/ws.tier-a.{w}"
                        for w in ("swing-trader", "beginner", "options-trader", "futures-trader"))
    sc = clean_scenario(TODAY)
    sc = dataclasses.replace(sc, base=SimBuild(sc.base.build_id, sc.base.changes, unsettled=targets))
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    hit = [o for o in r.outcomes if o.checkpoint_id in targets]
    assert hit and {(o.outcome, o.reason) for o in hit} == {("needs_baseline", "base_not_settled")}
    assert all(o.artifacts and o.artifacts.get("candidate") and "base" not in o.artifacts for o in hit)


def test_secrets_6_scan_run_reads_a_ledger_tail_and_in_memory_texts(tmp_path):
    """6.9 choice 1: a shared results.jsonl is scanned from the offset where this run began (an earlier run's lines
    are not this run's), and text the run has not written yet (the report's inputs) is scanned from memory."""
    from sentinel.store import ArtifactStore
    from sentinel.store.secret_scan import FileTail, scan_run
    secrets = {"vault://x#password": "hunter2-Pa55"}
    ledger = tmp_path / "results.jsonl"
    ledger.write_text('{"rows": ["earlier run: hunter2-Pa55"]}\n')
    start = ledger.stat().st_size
    with open(ledger, "a") as f:
        f.write('{"rows": ["this run"]}\n')
    arts = ArtifactStore(tmp_path / "set", "runs/r2")
    db = sqlite3.connect(":memory:")
    assert scan_run(db, arts, [FileTail(ledger, start)], secrets, run_id="r2").passed
    with open(ledger, "a") as f:
        f.write('{"rows": ["this run: hunter2-Pa55"]}\n')
    assert scan_run(db, arts, [FileTail(ledger, start)], secrets, run_id="r2").run_reason == "secret_leaked"
    res = scan_run(db, arts, [], secrets, run_id="r2", texts={"report inputs": b"banner hunter2-Pa55"})
    assert res.run_reason == "secret_leaked" and res.evidence["hits"][0]["location"] == "text:report inputs"
