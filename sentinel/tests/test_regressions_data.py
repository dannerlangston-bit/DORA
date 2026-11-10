"""Regressions for the data-layer review of store, records, taxonomy, lint and report (S-01 to S-24), plus the
9.7 ticket scope of an inherited mark_intended decision.

Each test failed before its fix. The finding id and spec section are in each test's comment.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from sentinel.lint import Repo, lint, lint_repo
from sentinel.lint.rules import credential_hits
from sentinel.records import (CheckpointOutcome, Classification, Decision, DiffSummary, RunOutcome, Signature,
                              TaxonomyError)
from sentinel.report.model import ReportContext, build_report, commit_status
from sentinel.store import (RunNotComplete, RunPlan, ScopeEntry, ShardPlan, Store, StoreRefused, apply_inheritance)
from sentinel.store.artifacts import ArtifactStore, RedactionRefused, StoredArtifact
from sentinel.taxonomy import load

REFUSED = (StoreRefused, TaxonomyError)
RUN = "run-01"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"                          # tier 1
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"    # tier 2
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
BASE = 0x0123456789ABCDEF
REGION = "rgn.ladder.bid-column"
DONE = RunOutcome(RUN, "pr", "complete", "reported")
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)


def ph(bits=()):
    v = BASE
    for b in bits:
        v ^= 1 << b
    return f"{v:016x}"


def scope():
    return (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
            ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen"),
            ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"))


def plan(run_id=RUN, run_type="pr", shards=(), pr_author=None, pair_id=None):
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                   budget_sec=2700, scope=scope(), shards=shards, pr_author=pr_author, pair_id=pair_id)


def out(cid, outcome="identical", run_id=RUN, **kw):
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=outcome, workspace_id=WS,
                             persona="active_trader", data_mode="frozen", **kw)


def record(store, did, action, verdict, phash=None, cid=SWEEP, category="collateral", by="alice", run_id=RUN,
           decided_at="2026-11-09T15:05:32-05:00", ticket=None, **kw):
    if ticket is None and action in ("confirm_bug", "mark_intended", "fixed_in_build"):
        ticket = "TOS-1"
    d = Decision(did, action, verdict, Signature(cid, REGION, phash or ph(), category), decided_by=by,
                 ticket=ticket, reason="because", decided_at=decided_at)
    return store.record_decision(d, run_id=run_id, **kw)


def finish_all(store, run_id=RUN, outcome="identical"):
    for cid in (CENSUS, SWEEP, FLOW):
        if store.get_outcome(run_id, cid) is None:
            store.write_checkpoint(out(cid, outcome, run_id=run_id))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "run.db")
    s.begin_run(plan())
    yield s
    s.close()


# -- decisions and the signature lookup (4.14, 8.3, 9.7) ------------------------------------------------------------

def test_s01_approve_baseline_from_a_pr_run_is_refused(store):
    # S-01, 4.14 L-DEC-05, 9.8: approve_baseline from a PR run is refused.
    with pytest.raises(REFUSED):
        record(store, "dec-000001", "approve_baseline", "intended", baseline_id=1)


def test_s01_approve_baseline_from_an_unfinished_run_is_refused(tmp_path):
    # S-01, L-DEC-05: approve_baseline from a run whose status is not complete is refused.
    with Store(tmp_path / "run.db") as s:
        s.begin_run(plan(run_type="merge"))
        with pytest.raises(REFUSED):
            record(s, "dec-000001", "approve_baseline", "intended", baseline_id=1)


def test_s01_approve_baseline_by_the_author_on_tier_1_is_refused_and_otherwise_written(tmp_path):
    # S-01, L-DEC-05: never by the author of the change on a tier-1 checkpoint; a complete merge run is fine.
    with Store(tmp_path / "run.db") as s:
        s.begin_run(plan(run_type="merge", pr_author="carol"))
        finish_all(s)
        s.end_run(RunOutcome(RUN, "merge", "complete", "reported"))
        with pytest.raises(REFUSED):
            record(s, "dec-000001", "approve_baseline", "intended", cid=CENSUS, by="carol", baseline_id=1)
        assert record(s, "dec-000002", "approve_baseline", "intended", cid=CENSUS, baseline_id=1).status == "active"


def test_s02_request_mask_is_inert_before_its_mask_merges(store):
    # S-02, 8.3 action table: request_mask is "noise, inert until mask_ref is set"; 4.14 "nothing before".
    record(store, "dec-000001", "request_mask", "noise")
    lk = store.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"))
    assert apply_inheritance(out(SWEEP, "collateral"), lk).outcome == "collateral"


def test_s03_waiver_needs_an_expiry(store):
    # S-03, 4.14 L-DEC-07, 9.7 waiver row: a waiver carries expires_at at most 30 days out.
    with pytest.raises(REFUSED):
        record(store, "dec-000001", "mark_intended", "intended", category="invariant_violation")


def test_s03_waiver_expiry_beyond_30_days_is_refused(store):
    with pytest.raises(REFUSED):
        record(store, "dec-000002", "mark_intended", "intended", category="assertion_failed",
               expires_at="2027-06-01T00:00:00+00:00")
    ok = record(store, "dec-000003", "mark_intended", "intended", category="assertion_failed",
                expires_at="2026-12-09T00:00:00+00:00")
    assert ok.status == "active"


def test_s04_fixed_in_build_must_name_a_newer_build(store):
    # S-04, 4.14 L-DEC-08: fixed_in_build names a build newer than build_id; its own build is not newer.
    facts = dict(channel="branch", pipeline_artifact_url="https://ci/1", installer_sha256="9" * 64)
    store.register_build("branch-1998.3.0-7be42d0", source_commit="7be42d0",
                         first_seen_at="2026-11-09T10:00:00+00:00", **facts)
    with pytest.raises(REFUSED):
        record(store, "dec-000001", "fixed_in_build", "confirmed_bug", build_id="branch-1998.3.0-7be42d0",
               fixed_in_build_id="branch-1998.3.0-7be42d0")
    store.register_build("branch-1998.3.1-8aa0b11", source_commit="8aa0b11",
                         first_seen_at="2026-11-10T10:00:00+00:00", **facts)
    d = record(store, "dec-000002", "fixed_in_build", "confirmed_bug", build_id="branch-1998.3.0-7be42d0",
               fixed_in_build_id="branch-1998.3.1-8aa0b11")
    assert d.action == "fixed_in_build"


def test_s05_two_supersessions_within_30_days_freeze_the_signature(store):
    # S-05, 9.7 "Two humans, one signature": two supersessions within 30 days freeze it; nothing is inherited.
    record(store, "dec-000001", "noise_signature", "noise", decided_at="2026-11-01T00:00:00+00:00")
    record(store, "dec-000002", "noise_signature", "noise", supersedes="dec-000001",
           decided_at="2026-11-05T00:00:00+00:00")
    record(store, "dec-000003", "noise_signature", "noise", supersedes="dec-000002",
           decided_at="2026-11-09T00:00:00+00:00")
    lk = store.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"), at="2026-11-10T00:00:00+00:00")
    assert lk.frozen and lk.inherited is None
    assert apply_inheritance(out(SWEEP, "collateral"), lk).outcome == "collateral"


def test_s06_inheritance_never_overrides_a_higher_precedence_outcome(store):
    # S-06, 1.10 precedence and not_settled ("No diff"); 8.3: inheritance sets noise/intended on a matching diff.
    record(store, "dec-000001", "noise_signature", "noise")
    lk = store.lookup_decisions(Signature(SWEEP, REGION, ph(), "collateral"))
    unsettled = out(SWEEP, "not_settled", reason="unstable", side="candidate")
    assert apply_inheritance(unsettled, lk) == unsettled
    assert apply_inheritance(out(SWEEP, "collateral"), lk).outcome == "noise"


# -- the 9.7 mark_intended ticket scope -----------------------------------------------------------------------------

def test_mark_intended_is_inherited_only_within_its_ticket_scope(tmp_path):
    # 9.7 table: mark_intended applies to "the build pair it was made on and later pairs whose manifest carries the
    # same ticket"; 4.14 inheritance rules. A run whose manifest carries neither the ticket nor the entry, on
    # another build pair, does not inherit it.
    with Store(tmp_path / "run.db") as s:
        s.begin_run(plan())
        record(s, "dec-000001", "mark_intended", "intended", ticket="TOS-48120")
        s.begin_run(plan(run_id="run-02"))
        sig = Signature(SWEEP, REGION, ph(), "collateral")

        def inherit(**scope_):
            return apply_inheritance(out(SWEEP, "collateral", run_id="run-02"),
                                     s.lookup_decisions(sig, run_id="run-02", **scope_)).outcome

        lk = s.lookup_decisions(sig, run_id="run-02", manifest_tickets=["TOS-1"])
        assert lk.inherited is None and [m.decision.decision_id for m in lk.out_of_scope] == ["dec-000001"]
        assert inherit(manifest_tickets=["TOS-1"]) == "collateral"
        assert inherit() == "collateral"
        assert inherit(manifest_tickets=["TOS-48120"]) == "intended"
        # the build pair it was made on: the same run
        assert apply_inheritance(out(SWEEP, "collateral"), s.lookup_decisions(sig, run_id=RUN)).outcome == "intended"


def test_mark_intended_scope_by_manifest_entry_and_build_pair(tmp_path):
    from sentinel.records import Finding
    with Store(tmp_path / "run.db") as s:
        s.conn.execute("INSERT INTO builds (build_id, channel, source_commit, pipeline_artifact_url, installer_sha256, "
                       "first_seen_at) VALUES ('c', 'branch', 'c1', 'u', ?, 't'), ('b', 'main', 'b1', 'u', ?, 't')",
                       ("1" * 64, "2" * 64))
        s.conn.execute("INSERT INTO build_pairs (pair_id, candidate_build_id, base_build_id, pair_kind, created_at) "
                       "VALUES (7, 'c', 'b', 'pr', 't')")
        s.begin_run(plan(pair_id=7))
        s.write_finding(Finding("F-1", RUN, "collateral", "padding", RUN, checkpoint_id=SWEEP, phash=ph(),
                                category="collateral", manifest_entry_ref="tos-48120-1"))
        record(s, "dec-000001", "mark_intended", "intended", ticket="TOS-48120", finding_id="F-1")
        s.begin_run(plan(run_id="run-02", pair_id=7))          # a re-run on the same build pair
        s.begin_run(plan(run_id="run-03"))                     # another pair
        sig = Signature(SWEEP, REGION, ph(), "collateral")
        assert s.lookup_decisions(sig, run_id="run-02").inherited.decision_id == "dec-000001"
        assert s.lookup_decisions(sig, run_id="run-03").inherited is None
        assert s.lookup_decisions(sig, run_id="run-03", manifest_entries=["tos-48120-1"]).inherited is not None


def test_waiver_is_not_ticket_scoped(store):
    # 9.7 table: a waiver (mark_intended on an invariant or assertion) applies to every run while active.
    record(store, "dec-000001", "mark_intended", "intended", category="invariant_violation",
           expires_at="2026-12-01T00:00:00+00:00")
    lk = store.lookup_decisions(Signature(SWEEP, REGION, ph(), "invariant_violation"), run_id="run-other")
    assert lk.inherited is not None


# -- run completion (1.10, 8.2, 4.13 L-OUT-07) ----------------------------------------------------------------------

def test_s07_complete_refused_with_a_fingerprint_mismatch(store):
    # S-07, 1.10 per run: a not_comparable (fingerprint_mismatch) checkpoint makes the run complete_with_env_errors.
    store.write_checkpoint(out(CENSUS, "not_comparable", reason="fingerprint_mismatch", detail="font_smoothing"))
    finish_all(store)
    with pytest.raises(RunNotComplete):
        store.end_run(DONE)
    with pytest.raises(sqlite3.IntegrityError, match="fingerprint"):        # and the schema says it again
        store.conn.execute("UPDATE runs SET lifecycle = 'finished', outcome = 'complete' WHERE run_id = ?", (RUN,))
    assert store.end_run(RunOutcome(RUN, "pr", "complete_with_env_errors", "reported")).status \
        == "complete_with_env_errors"


def _imported_shard(store):
    store.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    store.conn.execute("INSERT INTO shards (shard_id, run_id, rig_id, sides, scope_count, state, imported_at) "
                       "VALUES (?, ?, 'rig-a1', 'both', 3, 'imported', '2026-11-09T00:00:00+00:00')", (f"{RUN}/1", RUN))


def test_s08_complete_refused_with_an_unproven_post_run_proof(store):
    # S-08, 1.10 per run: complete needs the post-run proof PROVEN; 6.6, 4.13 run_outcome.post_run_proof_ref.
    _imported_shard(store)
    store.conn.execute("INSERT INTO post_run_proofs (shard_id, run_id, login_alias, verdict, reason, send_policy_ref, "
                       "counts_json, cancel_all, paper_mode_assertions, paper_mode_mismatches, gateway_events, "
                       "gateway_drops, produced_at) VALUES (?, ?, 'paper-01', 'unproven', 'session_unavailable', "
                       "'dec-1', '{}', 'not_needed', 1, 0, 0, 0, '2026-11-09T00:00:00+00:00')", (f"{RUN}/1", RUN))
    finish_all(store)
    with pytest.raises(RunNotComplete):
        store.end_run(DONE)


def test_s09_complete_refused_when_a_shard_aborted(tmp_path):
    # S-09, 8.2: the run outcome is the worst of the shards' outcomes (undeclared_order_row found at teardown).
    with Store(tmp_path / "run.db") as s:
        s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        s.begin_run(plan(shards=(ShardPlan(f"{RUN}/1", "rig-a1"),)))
        finish_all(s)
        s.end_shard(f"{RUN}/1", "aborted_safety", "undeclared_order_row")
        s.mark_shard_imported(f"{RUN}/1")
        with pytest.raises((RunNotComplete, StoreRefused)):
            s.end_run(DONE)
        with pytest.raises(StoreRefused):
            s.end_run(RunOutcome(RUN, "pr", "aborted_budget", "teardown", reason="wall_clock"))
        assert s.end_run(RunOutcome(RUN, "pr", "aborted_safety", "teardown", reason="undeclared_order_row")).status \
            == "aborted_safety"


def test_s10_aborted_run_cannot_finish_with_silent_scoped_checkpoints(store):
    # S-10, L-OUT-07; 8.2 "so no checkpoint is left silent": a finished run is final, so the fill comes first.
    store.write_checkpoint(out(CENSUS))
    abort = RunOutcome(RUN, "pr", "aborted_safety", "paper_mode_asserted", reason="paper_mode_mismatch")
    with pytest.raises(REFUSED):
        store.end_run(abort)
    store.fill_unreached(RUN, "env_error", "paper_mode_mismatch", gate="paper_mode_asserted")
    assert store.end_run(abort).status == "aborted_safety"


# -- outcome writes and artifact rows (4.13, 8.2 to 8.4) ------------------------------------------------------------

def test_s11_artifact_row_needs_verified_bytes_on_disk(store, tmp_path):
    # S-11, 8.2, 8.3 artifacts: "a row exists only for verified bytes on disk".
    ghost = StoredArtifact(sha256="c" * 64, name="cand.png", kind="capture", media_type="image/png", bytes=10,
                           set_path=f"runs/{RUN}", path=str(tmp_path / "objects" / "cc" / ("c" * 64 + ".png")),
                           redaction_version="r1")
    with pytest.raises(StoreRefused):
        store.write_checkpoint(out(CENSUS, artifacts={"candidate": "c" * 64}), artifacts=[ghost])


def test_s12_image_artifact_row_needs_a_redaction_version(store, tmp_path):
    # S-12, 8.4, 8.9 redaction_version_missing: an image row needs its redaction version.
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    art = StoredArtifact(sha256=sha, name="cand.png", kind="capture", media_type="image/png", bytes=12,
                         set_path=f"runs/{RUN}", path=str(p), redaction_version=None)
    with pytest.raises(StoreRefused):
        store.write_checkpoint(out(CENSUS, artifacts={"candidate": sha}), artifacts=[art])


def _image_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["WEBP", "TIFF"])
def test_s13_image_declared_as_text_still_needs_redaction(tmp_path, fmt):
    # S-13, 8.4, 6.10, 8.9: an image declared as text is still an image.
    arts = ArtifactStore(tmp_path / "set", "runs/run-01")
    with pytest.raises(RedactionRefused):
        arts.put(_image_bytes(fmt), name="frame.log", kind="log_slice", media_type="text/plain")
    assert arts.put(b"2026-11-10 12:00:00 INFO started\n", name="app.log", kind="log_slice",
                    media_type="text/plain").redaction_version is None


def test_s14_reviewer_classification_never_on_an_invariant_violation(store):
    # S-14, 4.13 L-OUT-03: classification.by reviewer never with invariant_violation or assertion_failed.
    with pytest.raises(REFUSED):
        store.write_checkpoint(out(SWEEP, "invariant_violation", reason="INV-01", invariant_ids=["INV-01"],
                                   classification=Classification("collateral", "reviewer", confidence=0.9,
                                                                 model_id="m")))


def test_s15_noise_by_decision_needs_an_existing_noise_decision(store):
    # S-15, 4.13 required fields: noise needs "decision:<id> and a decision_ref to an approved noise signature".
    with pytest.raises(REFUSED):
        store.write_checkpoint(out(SWEEP, "noise", reason="decision:dec-000777"))
    record(store, "dec-000001", "confirm_bug", "confirmed_bug")
    with pytest.raises(REFUSED):                          # a confirmed_bug decision is not a noise decision
        store.write_checkpoint(out(SWEEP, "noise", reason="decision:dec-000001"))
    store.write_checkpoint(out(SWEEP, "collateral", reason="decision:dec-000001", decision_ref="dec-000001"))


def test_s16_rescued_capture_is_never_identical(store):
    # S-16, 1.10 identical, F18, F32: an agent-rescued capture is needs_human (rescued_capture) even when clean.
    for outcome in ("identical", "noise", "intended"):
        with pytest.raises(REFUSED):
            reason = "below_floor" if outcome == "noise" else None
            store.write_checkpoint(out(SWEEP, outcome, reason=reason, rescued=True))
    store.write_checkpoint(out(SWEEP, "needs_human", reason="rescued_capture", rescued=True))


# -- taxonomy (1.10, 4.13, 12.7) ------------------------------------------------------------------------------------

def test_s17_aborted_harness_preflight_reason_is_an_h_item_only():
    # S-17, 1.10 aborted_harness: "preflight.<item> for a failed H item of 12.7".
    t = load()
    with pytest.raises(TaxonomyError):
        t.check_run("aborted_harness", "preflight.R1")
    t.check_run("aborted_harness", "preflight.H10")


def test_s17_harness_error_preflight_reason_is_an_h_item_only():
    with pytest.raises(TaxonomyError):
        load().check_checkpoint("harness_error", "preflight.R7")


@pytest.mark.parametrize("item", ["N2", "N5", "N7", "G1", "G5", "H3"])
def test_s17_env_error_preflight_gate_is_never_an_h_n2_n5_n7_g1_or_g5_item(item):
    # S-17, 12.7 outcomes by group.
    with pytest.raises(TaxonomyError):
        load().check_checkpoint("env_error", "gate", gate=f"preflight.{item}")


@pytest.mark.parametrize("item", ["R1", "B3", "G2", "G4", "N1", "N3", "N4", "N6"])
def test_s17_env_error_preflight_gates_of_12_7(item):
    load().check_checkpoint("env_error", "gate", gate=f"preflight.{item}")


def test_s18_app_alive_cause_never_in_the_reason():
    # S-18, 4.13 assertion_failed: the app_alive cause "goes in detail and never in reason"; F70.
    for reason in ("assertion:app_alive:jvm_crash", "assertion:app_alive.hang", "assertion:qty equals"):
        with pytest.raises(TaxonomyError):
            out(CENSUS, "assertion_failed", reason=reason)
    for reason in ("assertion:app_alive", "assertion:soak.working_set", "assertion:ticket_empty"):
        out(CENSUS, "assertion_failed", reason=reason)


# -- lint (4.8, 4.9, 4.11, 4.15) ------------------------------------------------------------------------------------

def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    assert lint(root, today=TODAY).ok
    return root


def test_s19_snake_case_secret_key_is_a_credential(tmp_path):
    # S-19, 4.15 L-XREF-07, 4.9 L-FIX-04: `db_password: <value>` is a password followed by a value.
    root = _tree(tmp_path)
    (root / "NOTES.md").write_text("rig-01 login\ndb_password: Tr0ub4dor\n")
    assert "L-XREF-07" in lint(root, today=TODAY).rules()


def test_s20_quoted_json_password_key_is_a_credential(tmp_path):
    # S-20, L-XREF-07: in JSON the key's closing quote sits before the colon.
    root = _tree(tmp_path)
    (root / "profiles" / "extra.json").write_text('{"login": "qa", "password": "hunter2"}\n')
    assert "L-XREF-07" in lint(root, today=TODAY).rules()


def test_s19_s20_credential_pattern_examples():
    assert credential_hits("client_secret: hunter2")
    assert credential_hits('{"password": "hunter2"}')
    assert not credential_hits("login_secret_ref: vault://rigs/rig-01#password")


def test_s21_identity_field_redaction_box_over_a_money_cell():
    # S-21, 4.8 mask budget: "any money_cell region 0 percent"; identity_field exempts the panel share only.
    repo = Repo.load(EXAMPLE)
    red = repo.docs["redaction/anchors.yaml"].data
    box = next(r for r in red["regions"] if r["id"] == "rgn.redact.ps-account-number")
    box["rect"] = {"dx": 0, "dy": 44, "w": 60, "h": 120}           # exactly over rgn.ps.qty
    assert "L-MSK-03" in lint_repo(repo, today=TODAY).rules()


def test_s22_canary_flow_clicking_a_confirm_and_send_control_under_another_id():
    # S-22, 4.11 L-MUT-04: "a canary_flow never contains a confirm-and-send target"; L-FLW-07's id or OCR text.
    repo = Repo.load(EXAMPLE)
    reg = repo.docs["registry/surfaces/trade/trade.order-entry.stock.yaml"].data
    reg["controls"].append({"id": "place-order", "template": "registry/templates/trade/confirm-and-send.png",
                            "ocr_text": "Confirm and Send"})
    fl = repo.docs["flows/tier-1/flow.canary.ticket-stock.yaml"].data
    i = next(k for k, s in enumerate(fl["steps"]) if s["id"] == "cp-ready")
    fl["steps"].insert(i + 1, {"id": "c05", "type": "click_named", "target": "trade.order-entry.stock.place-order"})
    assert "L-MUT-04" in lint_repo(repo, today=TODAY).rules()


def test_s23_negative_rect_size_is_refused():
    # S-23, 4.8 regions schema: rect {dx, dy, w, h} is an offset and a size; L-RGN-02, L-MSK-03.
    repo = Repo.load(EXAMPLE)
    d = repo.docs["regions/census/monitor.activity.position-statement/ws.tier-a.active-trader.yaml"].data
    m = next(x for x in d["regions"] if x["id"] == "rgn.ps.last-update")
    m["rect"] = {"dx": 60, "dy": 44, "w": -60, "h": 120}
    report = lint_repo(repo, today=TODAY)
    assert not report.ok and "L-RGN-00" in report.rules()


# -- report (9.2, 9.7) ----------------------------------------------------------------------------------------------

def _reopened_case():
    run_id = "run-2026-11-10-pr-0001"
    run = RunOutcome(run_id, "pr", "complete", "reported")
    o = CheckpointOutcome(run_id=run_id, checkpoint_id=CENSUS, outcome="collateral", workspace_id=WS,
                          persona="active_trader", data_mode="frozen", tier=1, decision_ref="dec-000812",
                          reason="decision:dec-000812", primitive="FlexibleGrid", manifest_entry_ref="tos-1-1",
                          diff=DiffSummary(whole_pixel_fraction=0.02, whole_ssim=0.95))
    d = Decision("dec-000812", "fixed_in_build", "confirmed_bug",
                 Signature(CENSUS, REGION, "c3a5e1f0b4d29876", "collateral"),
                 decided_by="rkim", ticket="TOS-48377", status="reopened", reason="fixed in 1998.3")
    return run, o, d


def test_s24_reopened_decision_does_not_suppress_the_returning_bug():
    # S-24, 9.7 confirm_bug row: "if it returns, status reopened ... and the finding returns to Unexpected".
    run, o, d = _reopened_case()
    ctx = ReportContext(pr=1, candidate_build="branch-1998.3.0-7be42d0", base_build="main-1998.3.0-2c91ae4")
    report = build_report(run, [o], [], [d], {}, {}, None, ctx)
    assert report.n_unexpected == 1 and report.n_suppressed == 0


def test_s24_reopened_decision_fails_the_check():
    # S-24, 9.2 failure: a tier-1 Unexpected item without a decision.
    run, o, d = _reopened_case()
    assert commit_status(run, [o], decisions=[d]).state == "failure"
