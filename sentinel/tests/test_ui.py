"""The report UI (9.5, 8.7, 9.1): every page with provenance, the checkpoint page's 1:1 crops, flicker and component
boxes, the 9.2 triage order with SLA clocks and proposals, the artifact endpoint's served, removed, quarantined and
evidence-missing answers, and actions refused behind the read-only banner while the Reporter service is down."""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone

import pytest
from PIL import Image

from sentinel.records import CheckpointOutcome, Component, Decision, DiffSummary, Finding, Signature
from sentinel.report.actions import ActionContext, RoleDirectory
from sentinel.service import InlineWriter, WsgiTransport, make_service
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.store.artifacts import ArtifactStore, Redaction
from sentinel.store.mirror import load_config_mirror
from sentinel.ui import HttpServiceClient, ReaderPool, header_identity, make_app
from sentinel.ui import pages

RUN, MERGE = "run-01", "run-m1"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"                                   # tier 1
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"     # tier 2
TIER3 = "census/charts.flexible-grid.chart-cell/ws.tier-a.swing-trader"              # tier 3
TS = "2026-11-10T15:00:00+00:00"
NOW = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
PH = "0123456789abcdef"
ONCALL = {"roles": [{"role": "system_owner", "person": "sam", "deputy": "sid"}],
          "flow_owners": [{"tier": 2, "person": "fo2", "deputy": "fo2d"}],
          "rotations": [{"rotation": "triage", "person": "tess", "from": "2026-11-09", "to": "2026-11-15"}],
          "triage_roster": ["tess"]}
REGIONS = {"schema_version": 1, "checkpoint_id": FLOW, "landmarks": [{"id": "lm.title", "template": "x.png"}],
           "regions": [{"id": "rgn.confirm.symbol", "type": "diff", "landmark": "lm.title",
                        "panel": "order_confirmation", "rect": {"dx": 144, "dy": 48, "w": 110, "h": 20}}]}


def png(color, size=(12, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def plan(run_id, run_type="pr", scope=None):
    scope = scope or (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
                      ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"),
                      ScopeEntry(TIER3, "ws.tier-a.swing-trader", "swing_trader", 3, "frozen"))
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=scope, shards=(ShardPlan(f"{run_id}/1", "rig-01"),), started_at=TS,
                   reviewer_version_sha256="e" * 64)


def find(s, fid, cid, kind, tier, run=RUN, category=None, region="rgn.x", phash=PH, created=TS):
    s.write_finding(Finding(fid, run, kind, f"{kind} on {cid}", run, checkpoint_id=cid, tier=tier, region_id=region,
                            phash=phash, category=category, created_at=created))


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "artifacts"
    s = Store(tmp_path / "db" / "central.db")
    s.register_build("m0", channel="branch", source_commit="m0", pipeline_artifact_url="u", installer_sha256="m0",
                     first_seen_at="2026-10-01T00:00:00+00:00")
    s.register_build("b1", channel="branch", source_commit="b1", pipeline_artifact_url="u", installer_sha256="b1",
                     first_seen_at="2026-11-01T00:00:00+00:00")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="vm-img-7", allowlist_entry=1)
    pair = s.register_pair("b1", "m0", "pr")
    s.begin_run(RunPlan(**{**plan(RUN).__dict__, "pair_id": pair}))
    load_config_mirror(s, "c0ffee", {FLOW: REGIONS})
    arts = ArtifactStore.at(root, f"runs/{RUN}")
    stored = {name: arts.put(png(c), name=f"{name}.png", kind="capture" if name != "heatmap" else "heatmap",
                             media_type="image/png", redaction=Redaction("r1"))
              for name, c in (("base", (1, 2, 3)), ("candidate", (4, 5, 6)), ("heatmap", (7, 8, 9)))}
    diff = DiffSummary(whole_pixel_fraction=0.01, components=[
        Component(area=200, bbox=(150, 60, 20, 10), region_id="rgn.confirm.symbol", bbox_region=(6, 12, 20, 10))])
    o = CheckpointOutcome(run_id=RUN, checkpoint_id=FLOW, outcome="collateral", workspace_id=WS,
                          persona="active_trader", data_mode="frozen", tier=2, diff=diff, settle_ms=420,
                          artifacts={k: v.sha256 for k, v in stored.items()})
    f = Finding("F-flow", RUN, "collateral", "the symbol <b>moved</b>", RUN, checkpoint_id=FLOW, tier=2,
                region_id="rgn.confirm.symbol", phash=PH, category="collateral", created_at=TS)
    s.write_checkpoint(o, findings=[f], artifacts=list(stored.values()), shard_id=f"{RUN}/1")
    find(s, "F-t1-inv", CENSUS, "invariant_violation", 1, category="invariant_violation")
    find(s, "F-t1-crash", CENSUS, "app_crash", 1, region="rgn.app")
    find(s, "F-t3", TIER3, "needs_human", 3, category="needs_human", created="2026-11-10T14:00:00+00:00")
    yield s, root, stored
    s.close()


def app(world, service=None, **kw):
    s, root, _ = world
    return make_app(ReaderPool(s.path), header_identity(), service=service, artifact_roots=[root], clock=lambda: NOW,
                    **kw)


def get(application, path, login="tess"):
    headers = {"X-Forwarded-User": login} if login else {}
    return WsgiTransport(application).request("GET", "http://ui" + path, headers=headers)


def post(application, path, form, login="tess"):
    body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    return WsgiTransport(application).request("POST", "http://ui" + path, body=body, headers={
        "X-Forwarded-User": login, "Content-Type": "application/x-www-form-urlencoded"})


def service_client(world):
    s, root, _ = world
    svc = make_service(InlineWriter(s), None, None, artifact_root=root,
                       actions=ActionContext(RoleDirectory.from_oncall(ONCALL), clock=lambda: TS))
    return HttpServiceClient("http://svc", WsgiTransport(svc))


def text(r) -> str:
    return r.body.decode("utf-8")


# -- every page, with provenance (9.5) -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/runs", "/runs?type=pr", f"/runs/{RUN}", f"/runs/{RUN}/coverage", "/coverage",
                                  f"/cp/{FLOW}", "/cp/F-flow", "/f/F-flow", "/triage", "/decisions", "/baselines",
                                  "/canary", "/metrics", "/docs/manifest-template", "/act/confirm_bug?f=F-flow",
                                  f"/runs/compare?a={RUN}&b={RUN}"])
def test_every_page_answers_with_provenance(world, path):
    r = get(app(world), path)
    assert r.status == 200, text(r)[:400]
    assert 'class="provenance"' in text(r)
    if path != "/docs/manifest-template":
        page = text(r)
        assert "harness commit" in page and "d00d" in page and "vm-img-7" in page and "b1 vs m0" in page


def test_the_ui_is_behind_sso_and_unknown_items_are_404(world):
    assert get(app(world), "/runs", login=None).status == 401
    assert get(app(world), "/runs/nope").status == 404
    assert get(app(world), "/f/nope").status == 404
    assert get(app(world), "/act/delete_everything").status == 404


def test_screen_and_model_text_is_escaped(world):
    page = text(get(app(world), "/f/F-flow"))
    assert "the symbol &lt;b&gt;moved&lt;/b&gt;" in page and "<b>moved</b>" not in page


# -- the checkpoint page (9.1, 9.5) ----------------------------------------------------------------------------------

def test_the_checkpoint_page_has_1_to_1_crops_flicker_at_2_hz_and_the_boxes(world):
    _, _, stored = world
    page = text(get(app(world), "/cp/F-flow"))
    cand = stored["candidate"].sha256
    assert f'src="/a/{cand}" width="12"' in page and 'class="px"' in page             # 1:1, pixelated
    assert 'class="flicker" data-period-ms="500"' in page                              # 2 Hz
    assert 'class="box component" style="left:150px;top:60px;width:20px;height:10px"' in page
    assert 'class="box region diff" style="left:144px;top:48px;width:110px;height:20px"' in page
    assert f"/a/{stored['heatmap'].sha256}" in page
    assert "420 ms" in page and "History over 30 runs" in page and "Live decision lookup" in page


def test_the_live_decision_lookup_shows_what_a_new_diff_would_inherit(world):
    s, _, _ = world
    s.record_decision(Decision("dec-000001", "noise_signature", "noise", Signature(FLOW, "rgn.confirm.symbol", PH,
                                                                                   "collateral"),
                               decided_by="tess", reason="clock", decided_at=TS), run_id=RUN)
    page = text(get(app(world), "/f/F-flow"))
    assert "dec-000001 (noise)" in page


# -- the triage queue (9.2, 9.5, 9.12) -------------------------------------------------------------------------------

def test_the_triage_queue_ranks_as_9_2_with_sla_clocks_assignees_and_proposals(world):
    s, _, _ = world
    find(s, "F-decided", CENSUS, "collateral", 1, category="collateral", region="rgn.y")
    s.record_decision(Decision("dec-000001", "confirm_bug", "confirmed_bug", Signature(CENSUS, "rgn.y", PH,
                                                                                       "collateral"),
                               decided_by="tess", reason="bug", decided_at=TS, finding_ref="F-decided"), run_id=RUN)
    fid = s.query("SELECT finding_id FROM findings WHERE natural_key = 'F-t3'")[0][0]
    s.insert("triage_assignments", {"finding_id": fid, "assignee": "tess", "assigned_by": "tess",
                                    "assigned_at": TS, "sla_due_at": "2026-11-11T15:00:00+00:00"})
    page = text(get(app(world), "/triage"))
    order = [page.index(f">{x}<") for x in ("F-t1-crash", "F-t1-inv", "F-flow", "F-t3")]
    assert order == sorted(order)                                     # tier, then the 9.2 kind order
    assert ">F-decided<" not in page                                  # decided: not in the queue
    assert "due 2026-11-11T15:00:00+00:00" in page and ">tess<" in page
    assert f"since {TS}" in page                                      # no SLA due time yet: the clock's start
    hooked = text(get(app(world, sla=lambda kind, start, tier: f"{kind}-due"), "/triage"))
    assert "due finding-due" in hooked


def test_a_proposal_shows_in_the_triage_queue_with_its_clock_and_buttons(world):
    s, _, _ = world
    fid = s.query("SELECT finding_id FROM findings WHERE natural_key = 'F-flow'")[0][0]
    s.insert("proposals", {"finding_id": fid, "run_id": RUN, "action": "mark_intended", "proposed_by": "pat",
                           "proposed_at": TS, "ticket_ref": "TOS-5", "reason": "redesign"})
    page = text(get(app(world, service=service_client(world)), "/triage"))
    assert "Proposals awaiting a second person" in page and ">pat<" in page and "4 business hours" in page
    assert 'href="/act/confirm_proposal?proposal=1"' in page


def test_lists_paginate(world, monkeypatch):
    monkeypatch.setattr(pages, "PAGE_SIZE", 2)
    first = text(get(app(world), "/triage"))
    assert "next 2" in first and ">F-flow<" not in first
    second = text(get(app(world), "/triage?page=2"))
    assert ">F-flow<" in second and "previous 2" in second


# -- the artifact endpoint (8.7) -------------------------------------------------------------------------------------

def test_a_stored_png_is_served_with_its_media_type_and_immutable_headers(world):
    _, _, stored = world
    r = get(app(world), f"/a/{stored['candidate'].sha256}")
    assert r.status == 200 and r.headers["Content-Type"] == "image/png"
    assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert hashlib.sha256(r.body).hexdigest() == stored["candidate"].sha256


def test_a_hash_retention_removed_answers_what_it_was_and_when(world):
    s, _, stored = world
    sha = stored["base"].sha256
    s.insert("artifact_sets", {"sha256": sha, "set_path": f"runs/{RUN}", "created_at": TS},
             upsert="(sha256, set_path) DO NOTHING")
    s.update("artifact_sets", {"sha256": sha}, {"deleted_at": "2027-05-09T02:00:00+00:00"})
    r = get(app(world), f"/a/{sha}")
    assert r.status == 410
    assert "Removed by retention" in text(r) and "2027-05-09" in text(r) and "capture" in text(r)


def test_a_quarantined_set_is_not_served(world):
    s, _, stored = world
    sha = stored["base"].sha256
    s.insert("artifact_sets", {"sha256": sha, "set_path": f"runs/{RUN}", "created_at": TS,
                               "quarantined_at": TS}, upsert="(sha256, set_path) DO UPDATE SET quarantined_at = "
                                                             "excluded.quarantined_at")
    assert get(app(world), f"/a/{sha}").status == 403


def test_missing_evidence_pages_name_the_finding_that_records_it(world):
    s, root, stored = world
    sha = stored["heatmap"].sha256
    s.update("artifacts", {"sha256": sha}, {"audit_state": "missing"})
    s.write_finding(Finding(f"fnd-{RUN}-artifact-missing", RUN, "artifact_missing", "missing", RUN,
                            checkpoint_id=FLOW, tier=2, evidence={"artifacts": [sha]}))
    r = get(app(world), f"/a/{sha}")
    assert r.status == 404 and "Evidence missing" in text(r) and f"fnd-{RUN}-artifact-missing" in text(r)
    cand = stored["candidate"].sha256
    for p in (root / "runs" / RUN / "objects" / cand[:2]).glob(cand + "*"):
        p.unlink()                                                     # bytes gone, row still ok
    r = get(app(world), f"/a/{cand}")
    assert r.status == 404 and "no stored copy" in text(r) and "no finding records it yet" in text(r)


def test_an_outcome_naming_an_unindexed_hash_gets_the_evidence_missing_page(world):
    s, _, _ = world
    ghost = "9" * 64
    s.insert("outcomes", {"run_id": RUN, "checkpoint_id": TIER3, "outcome": "identical",
                          "cand_capture_sha256": ghost, "recorded_at": TS})
    r = get(app(world), f"/a/{ghost}")
    assert r.status == 404 and "has no artifacts row" in text(r)
    assert get(app(world), f"/a/{'8' * 64}").status == 404


# -- actions and the read-only banner (8.2, 9.1) ------------------------------------------------------------------

def test_with_the_service_down_every_page_shows_the_banner_and_actions_are_refused(world):
    down = app(world)                                                  # no service at all
    for path in ("/runs", f"/runs/{RUN}", "/triage", "/f/F-flow"):
        assert 'class="banner"' in text(get(down, path))
    form = text(get(down, "/act/confirm_bug?f=F-flow"))
    assert "<button disabled>" in form
    r = post(down, "/act/confirm_bug", {"f": "F-flow", "reason": "bug"})
    assert r.status == 503 and "read-only" in text(r)
    s, _, _ = world
    assert s.query("SELECT count(*) FROM decisions")[0][0] == 0         # nothing queued, nothing written

    class Unhealthy:
        def health(self):
            return False

        def act(self, action, fields):
            raise AssertionError("never called while unhealthy")

    assert post(app(world, service=Unhealthy()), "/act/confirm_bug", {"f": "F-flow"}).status == 503


def test_an_action_posts_with_the_sso_identity_and_shows_the_result_or_the_clause(world):
    s, _, _ = world
    ui = app(world, service=service_client(world))
    assert 'class="banner"' not in text(get(ui, "/runs"))
    page = text(get(ui, "/act/confirm_bug?f=F-flow"))
    assert 'name="f" value="F-flow"' in page and "<button>" in page
    refused = post(ui, "/act/noise_signature", {"f": "F-flow", "reason": "clock"}, login="zed")
    assert refused.status == 403 and "9.6 Mark noise" in text(refused)
    done = post(ui, "/act/confirm_bug", {"f": "F-flow", "reason": "bug", "login": "sam"}, login="tess")
    assert done.status == 200 and "dec-000001" in text(done)
    assert s.get_decision("dec-000001").decided_by == "tess"            # the SSO login, never the form's


def test_the_metrics_page_changes_window_and_exports_csv(world):
    r = get(app(world), "/metrics?days=7&format=csv")
    assert r.headers["Content-Type"].startswith("text/csv")
    lines = text(r).splitlines()
    assert lines[0] == "metric,key,value" and any(re.match(r"outcomes,collateral,1", x) for x in lines)
    assert "window 7 days" in text(get(app(world), "/metrics?days=7"))


def test_the_coverage_matrix_reads_the_cells_with_a_tier_filter(world):
    s, _, _ = world
    s.begin_run(plan(MERGE, "merge"))
    for surface, persona, tier, state, reason in (("trade.ladder", "active_trader", 1, "covered", None),
                                                  ("charts.grid", "swing_trader", 3, "not_run", "budget"),
                                                  ("trade.new", "active_trader", 1, "not_registered", None)):
        s.insert("coverage_cells", {"run_id": MERGE, "surface_id": surface, "persona": persona,
                                    "entitlement": "margin", "tier": tier, "state": state,
                                    "not_run_reason": reason, "worst_outcome": "identical" if state == "covered"
                                    else None})
    page = text(get(app(world), f"/runs/{MERGE}/coverage"))
    assert "covered (identical)" in page and "not_run (budget)" in page
    assert '<span class="red">' in page                                  # in manifest, not registered
    tier1 = text(get(app(world), f"/runs/{MERGE}/coverage?tier=1"))
    assert "charts.grid" not in tier1 and "trade.ladder" in tier1
    assert MERGE in text(get(app(world), "/coverage"))


def test_a_finding_action_applies_to_the_whole_cluster_from_the_form(world):
    s, _, _ = world
    find(s, "F-c1", FLOW, "collateral", 2, category="collateral", region="rgn.confirm.symbol")
    find(s, "F-c2", FLOW, "collateral", 2, category="collateral", region="rgn.c2", phash="fedcba9876543210")
    s.update("findings", {"natural_key": "F-c1"}, {"cluster_id": "cl-9"})
    s.update("findings", {"natural_key": "F-c2"}, {"cluster_id": "cl-9"})
    ui = app(world, service=service_client(world))
    assert 'name="cluster"' in text(get(ui, "/act/confirm_bug?f=F-c1"))
    done = post(ui, "/act/confirm_bug", {"f": "F-c1", "reason": "bug", "cluster": "1"})
    assert done.status == 200
    assert s.query("SELECT count(*) FROM decisions WHERE cluster_id = 'cl-9'")[0][0] == 2
