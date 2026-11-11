"""The Reporter service's local API (5.1, 5.12, 5.13, 8.2, 8.9), its dead-man timer (5.14, 10.2) and the rig-side
shipper, driven in process through `WsgiTransport`."""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from sentinel.orchestrator.queue import PrBuild, RunFinished, RunReachedRunning
from sentinel.records import CheckpointOutcome, Finding
from sentinel.report.actions import ActionContext, RoleDirectory
from sentinel.service import (DeadmanTimer, InlineWriter, Shipper, ShipError, Upload, WsgiTransport, make_service)
from sentinel.service.app import Request, Router, respond
from sentinel.service.shipper import Response
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.store.store import StoreError

RUN, NIGHT = "run-01", "night-01"
SHARD = f"{RUN}/1"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"                                   # tier 1
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"     # tier 2
TS = "2026-11-10T15:00:00+00:00"
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def png(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def plan(run_id=RUN, run_type="pr", shards=(SHARD,)):
    scope = (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
             ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"))
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=scope, shards=tuple(ShardPlan(s, "rig-01") for s in shards), started_at=TS)


class FakeQueue:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)
        return [type(event)]


class FakeImporter:
    def __init__(self):
        self.calls = []

    def import_shard(self, store, incoming_dir, run_id, shard_id):
        self.calls.append((incoming_dir, run_id, shard_id))
        store.mark_shard_imported(shard_id)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "central.db")
    s.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    s.begin_run(plan())
    yield s
    s.close()


@pytest.fixture
def env(store, tmp_path):
    q, imp = FakeQueue(), FakeImporter()
    oncall = {"roles": [{"role": "system_owner", "person": "sam", "deputy": "sid"}],
              "flow_owners": [{"tier": 2, "person": "fo2", "deputy": "fo2d"}],
              "rotations": [{"rotation": "triage", "person": "tess", "from": "2026-11-09", "to": "2026-11-15"}],
              "triage_roster": ["tess"]}
    actions = ActionContext(RoleDirectory.from_oncall(oncall), clock=lambda: TS)
    svc = make_service(InlineWriter(store), imp, q, artifact_root=tmp_path / "art", actions=actions,
                       clock=lambda: T0)
    return svc, Shipper("http://control", WsgiTransport(svc), delays=(0, 0)), q, imp


def call(svc, method, path, doc=None, headers=None, body=b""):
    if doc is not None:
        body = json.dumps(doc).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    return WsgiTransport(svc).request(method, "http://control" + path, headers=headers, body=body)


def outcome(cid=CENSUS, result="identical", **kw):
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=result, workspace_id=WS, persona="active_trader",
                             data_mode="frozen", **kw)


def capture(data, cid=CENSUS, redaction="r1"):
    return Upload(sha(data), "capture", "image/png", lambda: data, checkpoint_id=cid, side="candidate",
                  redaction_version=redaction)


# -- the toolkit -----------------------------------------------------------------------------------------------------

def test_the_router_answers_404_and_405():
    r = Router()
    r.add("GET", "/x/{id}", lambda req: respond(200, {"id": req.params["id"]}))
    r.add("GET", "/p/{rest:path}/end", lambda req: respond(200, {"rest": req.params["rest"]}))
    t = WsgiTransport(r.wsgi)
    assert t.request("GET", "http://h/x/7").json() == {"id": "7"}
    assert t.request("GET", "http://h/p/run-1%2F2/end").json() == {"rest": "run-1/2"}
    assert t.request("POST", "http://h/x/7").status == 405
    assert t.request("GET", "http://h/nothing").status == 404
    assert Request({"QUERY_STRING": "a=1&b=2", "wsgi.input": io.BytesIO()}).query == {"a": "1", "b": "2"}


def test_health_follows_the_writer(env):
    svc = env[0]
    assert call(svc, "GET", "/v1/health").json() == {"ok": True}

    class Down:
        def submit(self, fn):
            raise StoreError("writer stopped")

    down = make_service(Down(), None, None, artifact_root=svc.artifact_root)
    assert call(down, "GET", "/v1/health").status == 503


# -- the scope manifest (5.12) ---------------------------------------------------------------------------------------

def test_the_scope_manifest_is_checked_kept_and_marks_the_run_running(env, store):
    svc, ship, q, _ = env
    svc.timer.run_started(RUN, "pr", T0 + timedelta(minutes=45))
    entries = [(CENSUS, "active_trader"), (FLOW, "active_trader")]
    assert ship.upload_scope(RUN, SHARD, entries).json()["status"] == "stored"
    assert ship.upload_scope(RUN, SHARD, entries).json()["status"] == "duplicate"
    assert svc.timer.watches[RUN].reached_running
    assert q.events == [RunReachedRunning(RUN)]
    kept = store.run_summary(RUN)["scope_manifests"][SHARD]
    assert kept["checkpoints"] == 2 and kept["sha256"].startswith("sha256:")
    other = call(svc, "POST", f"/v1/runs/{RUN}/scope", {"shard_id": SHARD, "checkpoints": [
        {"checkpoint_id": CENSUS, "persona": "active_trader"}]})
    assert other.status == 409                                                  # another manifest for the shard
    store.begin_run(plan("run-02", shards=("run-02/1",)))
    wrong = call(svc, "POST", "/v1/runs/run-02/scope", {"shard_id": "run-02/1", "checkpoints": [
        {"checkpoint_id": CENSUS, "persona": "swing_trader"}]})
    assert wrong.status == 409 and wrong.json()["unknown"] == [CENSUS]          # not as scoped (persona)
    assert call(svc, "POST", "/v1/runs/run-02/scope", {"shard_id": "run-09/1", "checkpoints": []}).status == 404
    assert call(svc, "POST", "/v1/runs/run-02/scope", {"checkpoints": []}).status == 400


# -- results and artifacts (5.1, 8.2, 8.9) -----------------------------------------------------------------------------

def test_a_checkpoint_batch_is_written_once_with_its_verified_artifacts(env, store):
    svc, ship, _, _ = env
    data = png()
    up = capture(data)
    o = outcome(artifacts={"candidate": up.sha256})
    f = Finding("F-1", RUN, "collateral", "moved", RUN, checkpoint_id=CENSUS, tier=1, region_id="rgn.ladder",
                phash="0123456789abcdef", category="collateral")
    assert ship.ship_checkpoint(RUN, SHARD, o, findings=[f], artifacts=[up]).status == 200
    assert store.get_outcome(RUN, CENSUS).outcome == "identical"
    assert store.get_finding("F-1") is not None
    row = store.query("SELECT media_type, redaction_version, set_path FROM artifacts WHERE sha256 = ?",
                      (up.sha256,))[0]
    assert tuple(row) == ("image/png", "r1", f"runs/{RUN}")
    assert (svc.artifact_root / "runs" / RUN / "objects" / up.sha256[:2]).is_dir()
    assert ship.ship_checkpoint(RUN, SHARD, o, findings=[f], artifacts=[up]).status == 200   # the same: a no-op
    with pytest.raises(ShipError) as e:
        ship.ship_checkpoint(RUN, SHARD, outcome(result="collateral", artifacts={"candidate": up.sha256}),
                             artifacts=[up])
    assert e.value.response.status == 409                                       # ImportConflict (8.2)
    missing = call(svc, "POST", f"/v1/runs/{RUN}/results", {"shard_id": SHARD, "outcome": outcome(FLOW).to_dict(),
                                                            "artifacts": ["f" * 64]})
    assert missing.status == 422 and "PUT it first" in missing.json()["refused"]
    bad = call(svc, "POST", f"/v1/runs/{RUN}/results", {"shard_id": SHARD, "outcome": {"run_id": RUN}})
    assert bad.status == 422


def test_an_upload_hash_mismatch_is_re_requested_three_times_then_the_shard_fails(env, store):
    svc, _, _, _ = env
    good, wrong = png(), png((1, 2, 3))
    headers = {"Content-Type": "image/png", "X-Sentinel-Kind": "capture", "X-Sentinel-Redaction-Version": "r1",
               "X-Sentinel-Run-Id": RUN, "X-Sentinel-Shard-Id": SHARD}
    for attempt in (1, 2, 3):
        r = call(svc, "PUT", f"/v1/artifacts/{sha(good)}", headers=headers, body=wrong)
        assert r.status == 422 and r.json() == {**r.json(), "re_request": True, "attempt": attempt}
    r = call(svc, "PUT", f"/v1/artifacts/{sha(good)}", headers=headers, body=wrong)
    assert r.json()["re_request"] is False and r.json()["shard"] == "import_failed"
    assert store.query("SELECT state FROM shards WHERE shard_id = ?", (SHARD,))[0][0] == "import_failed"
    run = store.run_outcome(RUN)
    assert (run.status, run.reason) == ("aborted_harness", "artifact_persist_failed")
    assert {o.outcome for o in store.outcomes(RUN)} == {"harness_error"}
    assert store.run_summary(RUN)["abort"]["detail"] == "upload_hash_mismatch"
    assert call(svc, "PUT", f"/v1/artifacts/{sha(good)}", headers=headers, body=good).status == 409


def test_a_picture_without_a_redaction_version_is_refused_and_its_checkpoint_is_harness_error(env, store):
    svc, ship, _, _ = env
    data = png()
    with pytest.raises(ShipError) as e:
        ship.put_artifact(RUN, SHARD, capture(data, cid=FLOW, redaction=None))
    assert e.value.response.json()["error"] == "redaction_version_missing"
    o = store.get_outcome(RUN, FLOW)
    assert (o.outcome, o.reason) == ("harness_error", "redaction_version_missing")
    assert store.get_outcome(RUN, CENSUS) is None
    r = call(svc, "PUT", f"/v1/artifacts/{sha(data)}", headers={"Content-Type": "image/png"}, body=data)
    assert r.status == 400                                                       # the 8.2 headers are required


# -- the shard hand-off (8.2) ------------------------------------------------------------------------------------------

def test_a_shipped_shard_is_imported_after_its_last_object_and_every_put_is_idempotent(env, store):
    svc, ship, _, imp = env
    a, b = png((1, 1, 1)), b'{"rows": 2}'
    objs = [capture(a), Upload(sha(b), "ledger", "application/json", lambda: b, name="results.jsonl")]
    r = call(svc, "POST", f"/v1/shards/{SHARD}/ship", {"run_id": RUN, "objects": [o.manifest_entry() for o in objs]})
    assert sorted(r.json()["need"]) == sorted(o.sha256 for o in objs) and not r.json()["imported"]
    assert store.query("SELECT state FROM shards WHERE shard_id = ?", (SHARD,))[0][0] == "shipping"
    first = ship.put_artifact(RUN, SHARD, objs[0])
    assert first.status == 200 and first.json()["need"] == [objs[1].sha256]
    assert ship.put_artifact(RUN, SHARD, objs[0]).json() == first.json()          # the same answer again
    assert imp.calls == []
    last = ship.put_artifact(RUN, SHARD, objs[1])
    assert last.json()["imported"] is True and len(imp.calls) == 1
    assert imp.calls[0][1:] == (RUN, SHARD) and imp.calls[0][0] == svc.incoming_root / SHARD
    assert ship.ship_shard(RUN, SHARD, objs).json()["imported"] is True and len(imp.calls) == 1
    changed = call(svc, "POST", f"/v1/shards/{SHARD}/ship", {"run_id": RUN, "objects": []})
    assert changed.status == 409
    assert call(svc, "POST", "/v1/shards/run-01/9/ship", {"run_id": RUN, "objects": []}).status == 404


def test_ship_shard_puts_only_what_the_service_needs(env, store):
    svc, ship, _, imp = env
    a = png((5, 5, 5))
    reads = []
    up = Upload(sha(a), "capture", "image/png", lambda: reads.append(1) or a, redaction_version="r1")
    assert ship.ship_shard(RUN, SHARD, [up]).json()["imported"] is True
    assert reads == [1] and len(imp.calls) == 1


# -- the dead-man timer (5.14, 10.2) -----------------------------------------------------------------------------------

def test_the_dead_man_closes_a_silent_running_run_with_harness_error_from_the_scope_manifest(env, store):
    svc, ship, _, _ = env
    ship.run_started(RUN, "pr", (T0 + timedelta(minutes=45)).isoformat(), {SHARD: "rig-01"})
    ship.upload_scope(RUN, SHARD, [(CENSUS, "active_trader"), (FLOW, "active_trader")])
    ship.ship_checkpoint(RUN, SHARD, outcome())
    ship.heartbeat("rig-01", (T0 + timedelta(minutes=59, seconds=30)).isoformat())
    assert svc.tick(T0 + timedelta(minutes=59)) == []                           # PR budget plus 15 minutes
    fired = svc.tick(T0 + timedelta(minutes=60))
    assert [(f.outcome, f.reason, f.filled, f.lost) for f in fired] == [("aborted_harness", "dead_man", (FLOW,), ())]
    o = store.get_outcome(RUN, FLOW)
    assert (o.outcome, o.reason) == ("harness_error", "dead_man")
    assert store.get_outcome(RUN, CENSUS).outcome == "identical"                # what arrived stays
    summary = store.run_summary(RUN)
    assert summary["abort"]["deadline"] == "2026-11-10T16:00:00+00:00"
    assert summary["abort"]["last_heartbeat"] == "2026-11-10T15:59:30+00:00"
    assert summary["alerts"][-1]["rule"] == "dead_man" and summary["alerts"][-1]["priority"] == "P2"
    assert svc.tick(T0 + timedelta(minutes=61)) == []                           # fired once


def test_an_unreachable_rig_is_named_and_a_completed_run_is_left_alone(env, store):
    svc, ship, _, _ = env
    ship.run_started(RUN, "merge", T0.isoformat(), {SHARD: "rig-01"})
    ship.upload_scope(RUN, SHARD, [(CENSUS, "active_trader"), (FLOW, "active_trader")])
    ship.heartbeat("rig-01", T0.isoformat())
    fired = svc.tick(T0 + timedelta(minutes=30))
    assert fired[0].lost == (SHARD,)
    assert store.run_summary(RUN)["abort"]["detail"] == "rig unreachable, run data intact on rig"
    assert store.run_summary(RUN)["alerts"][-1]["priority"] == "P1"            # merge runs page at P1
    store.begin_run(plan("run-02", shards=("run-02/1",)))
    ship.run_started("run-02", "pr", T0.isoformat(), {"run-02/1": "rig-01"})
    ship.run_completed("run-02", "complete")
    assert svc.tick(T0 + timedelta(hours=2)) == []
    assert env[2].events[-1] == RunFinished("run-02", "complete")


def test_a_nightly_run_that_never_reached_running_takes_start_failed(env, store):
    svc, ship, _, _ = env
    store.begin_run(plan(NIGHT, run_type="nightly", shards=()))
    ship.run_started(NIGHT, "nightly", "2026-11-11T05:00:00+00:00", {})
    fired = svc.tick(datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc))          # 06:00 New York
    assert [(f.outcome, f.reason) for f in fired] == [("aborted_harness", "start_failed")]


def test_the_timer_restores_its_watches_from_the_store(env, store):
    svc, ship, _, _ = env
    ship.upload_scope(RUN, SHARD, [(CENSUS, "active_trader"), (FLOW, "active_trader")])
    timer = DeadmanTimer()
    [w] = timer.restore(store)
    assert w.reached_running and w.shards == {SHARD: "rig-01"}
    assert w.deadline == T0 + timedelta(seconds=2700) + timedelta(minutes=15)


# -- the webhook and the actions -----------------------------------------------------------------------------------

def test_the_webhook_feeds_the_run_queue(env):
    svc, _, q, _ = env
    r = call(svc, "POST", "/v1/webhook", {"kind": "pr", "build_id": "b9", "commit": "abc", "pr": 7,
                                          "merge_base": "m0", "triggered_at": TS})
    assert r.status == 200, r.body
    assert isinstance(q.events[-1], PrBuild)
    assert call(svc, "POST", "/v1/webhook", {"kind": "nope"}).status == 400


def test_an_action_posts_through_the_writer_and_a_refusal_names_its_clause(env, store):
    svc = env[0]
    store.write_finding(Finding("F-2", RUN, "collateral", "moved", RUN, checkpoint_id=FLOW, tier=2,
                                region_id="rgn.confirm.symbol", phash="0123456789abcdef", category="collateral"))
    no = call(svc, "POST", "/v1/actions/noise_signature", {"login": "zed", "finding": "F-2", "reason": "clock"})
    assert no.status == 403 and no.json()["clause"] == "9.6 Mark noise"
    ok = call(svc, "POST", "/v1/actions/confirm_bug", {"login": "tess", "finding": "F-2", "reason": "bug"})
    assert ok.status == 200 and ok.json()["decisions"] == ["dec-000001"]
    [payload_id] = ok.json()["payloads"]
    assert any((svc.artifact_root / "outbox" / "objects").rglob(payload_id.split(":")[1] + "*"))
    dup = call(svc, "POST", "/v1/actions/noise_signature", {"login": "tess", "finding": "F-2", "reason": "x"})
    assert dup.status == 409 and dup.json()["active"] == ["dec-000001"]        # 9.7 one signature, one decision
    assert call(svc, "POST", "/v1/actions/confirm_bug", {"finding": "F-2"}).status == 400


# -- the shipper's retries ------------------------------------------------------------------------------------------

def test_the_shipper_retries_transport_failures_and_stops_on_a_refusal():
    class Flaky:
        def __init__(self, fails, then):
            self.fails, self.then, self.calls = fails, then, 0

        def request(self, method, url, *, headers=None, body=b""):
            self.calls += 1
            if self.calls <= self.fails:
                raise ConnectionError("reset")
            return self.then

    slept = []
    t = Flaky(2, Response(200, b"{}"))
    Shipper("http://c", t, delays=(1, 2, 5), sleep=slept.append).heartbeat("rig-01", TS)
    assert t.calls == 3 and slept == [1, 2]
    with pytest.raises(ShipError, match="gave up"):
        Shipper("http://c", Flaky(9, None), delays=(0,), sleep=slept.append).heartbeat("rig-01", TS)
    refused = Flaky(0, Response(409, b'{"refused": "x"}'))
    with pytest.raises(ShipError):
        Shipper("http://c", refused, delays=(0, 0)).heartbeat("rig-01", TS)
    assert refused.calls == 1


def test_the_shipper_resends_on_a_hash_mismatch_re_request(env, store):
    svc, ship, _, _ = env
    good = png((9, 9, 9))
    reads = [png((8, 8, 8)), good]
    up = Upload(sha(good), "capture", "image/png", lambda: reads.pop(0), redaction_version="r1")
    assert ship.put_artifact(RUN, SHARD, up).json()["stored"] is True
    assert reads == []
