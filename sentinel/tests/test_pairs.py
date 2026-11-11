"""Build pairs at run time: the PR base, a missing base, frozen base-capture reuse (1.7, 3.1, 4.2, 7.4, 7.9, SD-02,
SD-68)."""
from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from PIL import Image

from sentinel.evaluation import BaseEvidence, b_input_reason
from sentinel.orchestrator import alerts
from sentinel.pairs import (MAX_BASE_DISTANCE, REUSABLE_OUTCOMES, BaseBuildMissing, BaseResolution, MainCommit,
                            ReuseKey, ReuseLedger, StoredCapture, base_missing_result, declared_manifest,
                            golden_seeded_at, live_versions, resolve_base, reuse_decision, reuse_fixture_patch,
                            reuse_key, reused_base, scope_base_reuse, stored_bases)
from sentinel.records import CheckpointOutcome, Fingerprint, RunOutcome
from sentinel.report import build_report, render_report_markdown
from sentinel.scope import Manifest, ManifestEntry
from sentinel.store import RunPlan, ScopeEntry, ShardPlan, Store
from sentinel.store.artifacts import ArtifactStore, Redaction
from sentinel.taxonomy import load

from test_report_sample import sample

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"
SWEEP = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
LIVE = f"census/monitor.positions/{WS}"                            # a masked_live checkpoint
UNSETTLED = f"census/monitor.activity/{WS}"
EPOCH = "sha256:" + "e" * 64
T0 = "2026-11-09T14:00:00+00:00"


def sha(n: int) -> str:
    return f"{n:040x}"


def build(build_id: str, commit: str, channel: str = "main", built_at: str = "2026-11-08T21:10:00-05:00",
          merge_base: str | None = None) -> dict:
    b = {"build_id": build_id, "channel": channel, "commit_sha": commit, "built_at": built_at}
    if merge_base:
        b["merge_base_sha"] = merge_base
    return {"schema_version": 1, "build": b}


def history(n: int, manifests: dict[int, tuple[str, ...]] | None = None) -> list[MainCommit]:
    """Main commits sha(100) newest to sha(100 - n + 1) oldest, first parent."""
    manifests = manifests or {}
    return [MainCommit(sha(100 - i), manifests.get(i, ())) for i in range(n)]


CAND = build("branch-1998.3.0-aaaaaaa", sha(1), channel="branch", merge_base=sha(100))


# -- the PR base (SD-02, 3.1, 4.2) ----------------------------------------------------------------------------

def test_the_merge_base_with_a_build_record_is_the_base_at_distance_zero():
    builds = {"main-1": build("main-1", sha(100)), "main-0": build("main-0", sha(99))}
    r = resolve_base(CAND, history(5, {0: ("branch-x",)}), builds)
    assert r == BaseResolution("main-1", sha(100), sha(100), 0, ())
    assert r.health_row() is None
    assert r.placeholder == f"Sentinel run started, base = main@{sha(100)[:7]}"


def test_a_merge_base_without_a_build_resolves_to_its_parent_and_declares_the_merge_base_pr():
    builds = {"main-0": build("main-0", sha(99))}
    r = resolve_base(CAND, history(5, {0: ("branch-pr-4810",), 1: ("branch-pr-4801",)}), builds)
    assert (r.base_build_id, r.base_commit, r.distance) == ("main-0", sha(99), 1)
    # the merge base's own PR is in the candidate and not in the base build; the parent's PR is in both
    assert r.declared_manifests == ("branch-pr-4810",)
    assert r.health_row() == ("Base", ("1 commits before the merge base, the nearest main ancestor with a build "
                                       "record (4.2); the manifests of the PRs merged in between are treated as "
                                       "declared"))


def test_the_intervening_manifests_are_listed_oldest_first_and_once():
    builds = {"main-old": build("main-old", sha(97))}
    r = resolve_base(CAND, history(6, {0: ("pr-c",), 1: ("pr-b", "pr-c"), 2: ("pr-a",), 3: ("pr-base",)}), builds)
    assert r.distance == 3 and r.declared_manifests == ("pr-a", "pr-b", "pr-c")


def test_the_walk_stops_twenty_commits_back():
    at_20 = {"main-20": build("main-20", sha(100 - MAX_BASE_DISTANCE))}
    assert resolve_base(CAND, history(30), at_20).distance == 20 == MAX_BASE_DISTANCE
    at_21 = {"main-21": build("main-21", sha(100 - 21))}
    with pytest.raises(BaseBuildMissing) as e:
        resolve_base(CAND, history(30), at_21)
    assert e.value.cause == "not_found" and e.value.merge_base_sha == sha(100)
    assert len(e.value.searched) == 21 and e.value.searched[0] == sha(100)
    assert "20 nearest main ancestors" in e.value.detail


def test_no_main_build_on_any_ancestor_is_base_build_missing():
    builds = {"branch-y": build("branch-y", sha(99), channel="branch", merge_base=sha(98)),
              "rc-1": build("rc-1", sha(98), channel="rc")}
    with pytest.raises(BaseBuildMissing, match="no main build record at merge base"):
        resolve_base(CAND, history(5), builds)


def test_a_merge_base_outside_the_history_resolves_only_at_distance_zero():
    builds = {"main-1": build("main-1", sha(100))}
    assert resolve_base(CAND, [], builds).distance == 0
    with pytest.raises(BaseBuildMissing):
        resolve_base(CAND, [sha(5), sha(4)], {"main-4": build("main-4", sha(4))})


def test_several_main_builds_of_one_commit_take_the_latest_and_plain_shas_are_accepted():
    builds = {"main-a": build("main-a", sha(99), built_at="2026-11-08T10:00:00-05:00"),
              "main-b": build("main-b", sha(99), built_at="2026-11-08T12:00:00-05:00")}
    r = resolve_base(CAND["build"], [sha(100), sha(99)], builds)
    assert (r.base_build_id, r.distance, r.declared_manifests) == ("main-b", 1, ())


def test_a_candidate_without_a_merge_base_is_refused():
    with pytest.raises(ValueError, match="merge_base_sha"):
        resolve_base(build("main-9", sha(9)), history(2), {})


def test_the_example_pr_build_resolves_to_its_merge_base_build():
    builds = {p.stem: yaml.safe_load(p.read_text()) for p in (EXAMPLE / "builds").glob("*.yaml")}
    cand = builds["branch-1998.3.0-5d3e812"]
    r = resolve_base(cand, [cand["build"]["merge_base_sha"]], builds)
    assert (r.base_build_id, r.distance) == (cand["build_pair"]["base"], 0)


def manifest(mid: str, *entries: str, empty_reason: str | None = None) -> Manifest:
    return Manifest(mid, mid, "main-1", "manual", "jdoe",
                    tuple(ManifestEntry(e, ("TOS-1",), declared_surfaces=(f"s.{e}",)) for e in entries),
                    empty_reason=empty_reason)


def test_the_intervening_manifests_join_the_pr_manifest_as_declared_and_not_as_obligations():
    pr = manifest("mf-pr", "e-pr")
    m = declared_manifest(pr, [manifest("mf-a", "e-a1", "e-a2"), manifest("mf-b", "e-b")])
    assert [e.entry_id for e in m.entries] == ["e-pr", "e-a1", "e-a2", "e-b"]
    assert [e.must_be_observed for e in m.entries] == [True, False, False, False]
    assert m.surfaces == {"s.e-pr", "s.e-a1", "s.e-a2", "s.e-b"} and m.manifest_id == "mf-pr"
    refactor = declared_manifest(manifest("mf-r", empty_reason="pure refactor"), [manifest("mf-a", "e-a1")])
    assert refactor.empty_reason is None and [e.entry_id for e in refactor.entries] == ["e-a1"]
    assert declared_manifest(pr, []) == pr
    assert declared_manifest(None, [manifest("mf-a", "e-a1")]) is None
    with pytest.raises(ValueError, match="e-pr"):
        declared_manifest(pr, [manifest("mf-c", "e-pr")])


# -- base_build_missing (1.7 T+0:00, 1.10, 3.1, 4.2) ---------------------------------------------------------------

def test_a_missing_base_build_aborts_the_run_with_a_rebuild_request_and_one_requeue():
    cause = BaseBuildMissing.not_found(sha(100), [sha(100)], 20)
    r = base_missing_result(cause, requeued_as="run-02")
    assert not r.passed and r.gate == "provisioned"
    assert (r.run.status, r.run.reason) == ("aborted_harness", "base_build_missing")
    assert (r.unreached.outcome, r.unreached.reason) == ("harness_error", "base_build_missing")
    assert r.alert == alerts.aborted_harness() and r.alert.priority == "P1" and "harness_on_call" in r.alert.paged
    assert r.evidence == {"abort": {"trigger": cause.detail, "gate": "provisioned", "requeued": "run-02"},
                          "cause": "not_found", "rebuild": sha(100), "requeue": True}


def test_the_requeued_run_is_not_requeued_again():
    r = base_missing_result(BaseBuildMissing.not_found(sha(100), [], 20), requeue_of="run-01")
    assert r.evidence["requeue"] is False and "requeued" not in r.evidence["abort"]
    with pytest.raises(ValueError, match="re-queued run"):
        base_missing_result(BaseBuildMissing.not_found(sha(100), [], 20))


@pytest.mark.parametrize("cause,text", [
    (BaseBuildMissing.install_failed("main-1998.2.0-4f1c9a2", "installer sha256 mismatch"),
     "base build main-1998.2.0-4f1c9a2 could not be installed: installer sha256 mismatch"),
    (BaseBuildMissing.capture_set_absent("main-1998.2.0-4f1c9a2"),
     "the capture set of base build main-1998.2.0-4f1c9a2 is absent")])
def test_an_uninstallable_base_or_an_absent_capture_set_is_base_build_missing_without_a_rebuild(cause, text):
    r = base_missing_result(cause, requeued_as="run-02")
    assert r.run.reason == "base_build_missing" and r.detail == text and r.evidence["rebuild"] is None


def test_the_abort_fills_every_scoped_checkpoint_before_any_capture_and_prints_the_abort_block(tmp_path):
    r = base_missing_result(BaseBuildMissing.not_found(sha(100), [sha(100)], 20), requeued_as="run-02")
    with Store(tmp_path / "central.db") as st:
        st.begin_run(RunPlan("run-01", "pr", "c", "t", "s", "h", "auth/v1", 2700,
                             (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
                              ScopeEntry(SWEEP, WS, "active_trader", 2, "frozen")), started_at=T0))
        assert st.fill_unreached("run-01", r.unreached.outcome, r.unreached.reason) == [CENSUS, SWEEP]
        st.end_run(RunOutcome("run-01", "pr", r.run.status, r.gate, reason=r.run.reason, started_at=T0),
                   summary={"abort": r.evidence["abort"]})
        run = st.run_outcome("run-01")
        assert (run.status, run.reason, run.last_gate) == ("aborted_harness", "base_build_missing", "provisioned")
        assert {(o.outcome, o.reason) for o in st.outcomes("run-01")} == {("harness_error", "base_build_missing")}
        summary = st.run_summary("run-01")
    s = sample()
    s["run"] = replace(s["run"], status=run.status, reason=run.reason, last_gate=run.last_gate)
    text = render_report_markdown(build_report(**s, summary=summary))
    assert "- Reason: `base_build_missing`" in text
    assert "- Last gate reached: provisioned" in text and "- Re-queued once: run-02" in text


def test_the_base_row_is_a_run_health_row():
    r = BaseResolution("main-0", sha(99), sha(100), 3, ("pr-a",))
    s = sample()
    text = render_report_markdown(build_report(**s, summary={"health_rows": [list(r.health_row())]}))
    assert "| Base | 3 commits before the merge base, the nearest main ancestor with a build record (4.2)" in text


# -- the reuse key (7.9, SD-68) -------------------------------------------------------------------------------------

FIXTURE = (EXAMPLE / "fixtures" / "fx.rig-01.paper.yaml").read_bytes()


def test_the_live_reuse_key_hashes_the_fixture_file_and_reads_the_golden_seed():
    k = reuse_key("sha256:" + "f" * 64, FIXTURE)
    doc = yaml.safe_load(FIXTURE)
    assert k.fingerprint_id == "sha256:" + "f" * 64 and k.epoch_id == doc["epoch"]["epoch_id"]
    assert k.fixture_sha256 == "sha256:" + hashlib.sha256(FIXTURE).hexdigest()
    assert k.golden_seeded_at == "2026-10-06T14:45:00+00:00"        # the latest golden position, in UTC
    reseeded = FIXTURE.replace(b"seeded_at: 2026-10-06T10:45:00-04:00", b"seeded_at: 2026-11-02T10:45:00-05:00")
    k2 = reuse_key(k.fingerprint_id, reseeded)
    assert k2.golden_seeded_at == "2026-11-02T15:45:00+00:00"
    assert k.differing(k2) == ["fixture_sha256", "golden_seeded_at"]


def test_the_golden_seed_ignores_the_empty_account_and_needs_offsets():
    doc = {"accounts": [{"account_id": "D-1", "role": "golden"}, {"account_id": "D-2", "role": "empty"}],
           "positions": [{"account_ref": "D-2", "seeded_at": "2026-12-01T10:00:00-05:00"},
                         {"account_ref": "D-1", "seeded_at": "2026-10-01T10:00:00-04:00"}]}
    assert golden_seeded_at(doc) == "2026-10-01T14:00:00+00:00"
    assert golden_seeded_at({"accounts": doc["accounts"], "positions": []}) == ""
    with pytest.raises(ValueError, match="UTC offset"):
        golden_seeded_at({"accounts": doc["accounts"], "positions": [{"account_ref": "D-1",
                                                                      "seeded_at": "2026-10-01T10:00:00"}]})
    with pytest.raises(ValueError, match="epoch_id"):
        reuse_key("sha256:x", b"accounts: []\n")


# -- reuse decisions (3.1, 7.4) -------------------------------------------------------------------------------------

KEY = ReuseKey("sha256:" + "f" * 64, EPOCH, "sha256:" + "a" * 64, "2026-10-06T14:45:00+00:00")


def cap(cid: str = CENSUS, run: str = "merge-1", key: ReuseKey | None = KEY, red: str = "redact-v3",
        region: str = "reg-7", at: str = T0) -> StoredCapture:
    parts = (key.fingerprint_id, key.epoch_id, key.fixture_sha256, key.golden_seeded_at) if key else (None,) * 4
    return StoredCapture(cid, run, "merge", "candidate", f"{run}/1", "c" * 64, at, *parts, red, region)


def decide(stored, **kw):
    kw.setdefault("redaction_version", "redact-v3")
    kw.setdefault("region_version", "reg-7")
    return reuse_decision(CENSUS, KEY, stored, **kw)


def test_an_identical_key_reuses_the_stored_capture_and_names_its_run():
    d = decide([cap()])
    assert d.action == "reuse" and d.source_run_id == "merge-1" and d.reused == cap()
    assert decide(cap()).action == "reuse"


@pytest.mark.parametrize("field,value", [("golden_seeded_at", "2026-09-01T14:45:00+00:00"),
                                         ("fixture_sha256", "sha256:" + "b" * 64), ("epoch_id", "sha256:" + "0" * 64),
                                         ("fingerprint_id", "sha256:" + "1" * 64)])
def test_any_key_part_that_differs_recaptures(field, value):
    d = decide([cap(key=replace(KEY, **{field: value}))])
    assert d.action == "recapture" and d.reused is None and d.why == f"stored capture of run merge-1 differs in {field}"


def test_a_changed_redaction_or_region_registry_version_recaptures():
    assert decide([cap(red="redact-v2")]).why.endswith("differs in redaction_version")
    assert decide([cap(region="reg-6")]).why.endswith("differs in region_registry_version")


def test_a_stored_capture_without_its_fixture_key_is_never_reused():
    d = decide([cap(key=None)])
    assert d.action == "recapture" and "fingerprint_id" in d.why and "golden_seeded_at" in d.why


def test_the_latest_matching_capture_is_reused_when_the_latest_differs():
    newer = cap(run="merge-2", key=replace(KEY, golden_seeded_at="2026-11-20T15:00:00+00:00"),
                at="2026-11-10T14:00:00+00:00")
    d = decide([newer, cap()])
    assert d.action == "reuse" and d.source_run_id == "merge-1"


def test_masked_live_never_reuses_and_an_expected_reuse_with_nothing_stored_is_reuse_missing():
    assert decide([cap()], data_mode="masked_live").action == "recapture"
    assert decide([], expected=True).action == "missing"
    assert decide(None).action == "recapture"
    with pytest.raises(ValueError, match="another checkpoint"):
        decide([cap(cid=SWEEP)])


def test_the_scope_reuse_keeps_scopes_per_rig_fingerprint_check():
    reuse = scope_base_reuse(KEY, {CENSUS: [cap()], SWEEP: [cap(SWEEP, key=replace(KEY, epoch_id="sha256:old"))]},
                             redaction_version="redact-v3", region_version="reg-7")
    assert reuse.source_for(CENSUS) == "merge-1"
    assert reuse.source_for(CENSUS, "sha256:" + "9" * 64) is None        # another rig's fingerprint
    assert reuse.source_for(SWEEP) is None


def test_the_live_versions_come_from_the_fingerprints_harness():
    fp = Fingerprint("pr-1", "base", "rig-01", T0, dict(FP_MATCH, build_id="main-B"))
    assert live_versions(fp) == live_versions(fp.to_dict()) == live_versions(fp.match) == ("redact-v3", "reg-7")
    with pytest.raises(ValueError, match="region_registry_version"):
        live_versions({"build_id": "main-B", "harness": {"redaction_version": "redact-v3"}})


def test_the_reusable_outcomes_are_taxonomy_outcomes():
    assert set(REUSABLE_OUTCOMES) <= set(load().checkpoint_outcomes)


# -- the store: a merge run's captures reused by a PR run (3.1, 7.4, 11.2) -----------------------------------------

def png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, format="PNG")
    return buf.getvalue()


FP_MATCH = {"display": {"w": 1920, "h": 1080, "dpi": 96}, "data_epoch": {"epoch_id": EPOCH, "data_mode": "none"},
            "harness": {"region_registry_version": "reg-7", "redaction_version": "redact-v3"}}


class Central:
    """A central store with main-A, main-B and a branch build, and helpers to write finished runs."""

    def __init__(self, tmp_path: Path):
        self.st = Store(tmp_path / "central.db")
        self.root = tmp_path / "artifacts"
        self.st.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
        for b, ch in (("main-A", "main"), ("main-B", "main"), ("branch-C", "branch")):
            self.st.register_build(b, channel=ch, source_commit=b, pipeline_artifact_url=f"art://{b}",
                                   installer_sha256="0" * 64)
        self.fp = Fingerprint("x", "candidate", "rig-01", T0, dict(FP_MATCH, build_id="main-B")).fingerprint_id
        self.key = replace(KEY, fingerprint_id=self.fp)

    def run(self, run_id: str, run_type: str, cand: str, base: str, rows: dict[str, tuple], *, at: str = T0,
            status: str = "complete", reason: str | None = None, key: ReuseKey | None = None,
            record_fp: bool = True) -> dict[str, str]:
        """One finished run; `rows` maps checkpoint id to (outcome, data_mode, base_source_run_id). Returns the
        candidate and base capture hashes per checkpoint."""
        pair = self.st.register_pair(cand, base, run_type)
        scope = tuple(ScopeEntry(c, WS, "active_trader", 1, mode) for c, (_, mode, _) in rows.items())
        shard = f"{run_id}/1"
        self.st.begin_run(RunPlan(run_id, run_type, "c", "t", "s", "h", "auth/v1", 2700, scope,
                                  (ShardPlan(shard, "rig-01"),), pair_id=pair, started_at=at))
        for side, build_id in (("candidate", cand), ("base", base)):
            self.st.write_fingerprint(Fingerprint(run_id, side, "rig-01", at, dict(FP_MATCH, build_id=build_id),
                                                  shard_id=shard))
        arts = ArtifactStore(self.root, f"runs/{run_id}")
        hashes = {}
        for i, (cid, (outcome, mode, source)) in enumerate(rows.items()):
            c = arts.put(png((i, len(run_id), 1)), name=f"{cid}.cand.png", kind="capture", media_type="image/png",
                         redaction=Redaction("redact-v3"), checkpoint_id=cid, side="candidate")
            b = arts.put(png((i, len(run_id), 2)), name=f"{cid}.base.png", kind="capture", media_type="image/png",
                         redaction=Redaction("redact-v3"), checkpoint_id=cid, side="base")
            hashes[cid] = (c.sha256, b.sha256)
            why = {"not_settled": "frames_unstable", "needs_human": "reviewer_uncertain"}.get(outcome)
            self.st.write_checkpoint(CheckpointOutcome(
                run_id=run_id, checkpoint_id=cid, outcome=outcome, reason=why, workspace_id=WS,
                persona="active_trader", data_mode=mode, artifacts={"candidate": c.sha256, "base": b.sha256},
                fingerprint_id=self.fp if record_fp else None, data_epoch_id=EPOCH, captured_at=at,
                base_source_run_id=source), artifacts=[c, b], shard_id=shard)
        self.st.mark_shard_imported(shard)
        summary = reuse_fixture_patch({shard: key or self.key})
        self.st.end_run(RunOutcome(run_id, run_type, status, "reported", reason=reason, started_at=at),
                        summary=summary)
        return hashes


def test_stored_bases_returns_the_merge_runs_frozen_captures_of_the_base_build(tmp_path):
    c = Central(tmp_path)
    h = c.run("merge-1", "merge", "main-B", "main-A",
              {CENSUS: ("identical", "frozen", None), SWEEP: ("needs_human", "frozen", None),
               LIVE: ("identical", "masked_live", None), UNSETTLED: ("not_settled", "frozen", None)})
    got = stored_bases(c.st, [CENSUS, SWEEP, LIVE, UNSETTLED, "census/x.y/" + WS], base_build_id="main-B")
    assert set(got) == {CENSUS, SWEEP}                      # masked_live and an unsettled capture are never bases
    [s] = got[CENSUS]
    assert (s.source_run_id, s.run_type, s.side, s.shard_id) == ("merge-1", "merge", "candidate", "merge-1/1")
    assert s.capture_sha256 == h[CENSUS][0] and s.key == c.key
    assert (s.redaction_version, s.region_version) == ("redact-v3", "reg-7")
    # the same run is the stored base of main-A on its base side
    assert stored_bases(c.st, [CENSUS], base_build_id="main-A")[CENSUS][0].capture_sha256 == h[CENSUS][1]
    assert stored_bases(c.st, [], base_build_id="main-B") == {}
    assert stored_bases(c.st, None, base_build_id="main-B") == {cid: got[cid] for cid in sorted(got)}
    c.st.close()


def test_stored_bases_puts_the_latest_capture_first_and_skips_reused_and_unfinished_ones(tmp_path):
    c = Central(tmp_path)
    c.run("merge-1", "merge", "main-B", "main-A", {CENSUS: ("identical", "frozen", None)})
    # a later PR run on main-B captured its base fresh: a newer stored base
    c.run("pr-1", "pr", "branch-C", "main-B", {CENSUS: ("identical", "frozen", None)}, at="2026-11-10T14:00:00+00:00")
    # a still later PR run reused merge-1's base: not a capture of its own
    c.run("pr-2", "pr", "branch-C", "main-B", {CENSUS: ("identical", "frozen", "merge-1")},
          at="2026-11-11T14:00:00+00:00")
    got = stored_bases(c.st, [CENSUS], base_build_id="main-B")[CENSUS]
    assert [(s.source_run_id, s.side) for s in got] == [("pr-1", "base"), ("merge-1", "candidate")]
    c.st.close()


def test_an_aborted_run_and_a_deleted_artifact_are_not_stored_bases(tmp_path):
    c = Central(tmp_path)
    c.run("merge-0", "merge", "main-B", "main-A", {CENSUS: ("identical", "frozen", None)},
          at="2026-11-10T14:00:00+00:00", status="aborted_budget", reason="wall_clock")
    h = c.run("merge-1", "merge", "main-B", "main-A", {CENSUS: ("identical", "frozen", None),
                                                       SWEEP: ("identical", "frozen", None)})
    c.st.conn.execute("UPDATE artifacts SET deleted_at = ? WHERE sha256 = ?", (T0, h[SWEEP][0]))
    got = stored_bases(c.st, [CENSUS, SWEEP], base_build_id="main-B")
    assert list(got) == [CENSUS] and [s.source_run_id for s in got[CENSUS]] == ["merge-1"]
    c.st.close()


def test_a_run_without_its_fixture_key_or_record_fingerprint_reads_what_it_has(tmp_path):
    c = Central(tmp_path)
    c.run("merge-1", "merge", "main-B", "main-A", {CENSUS: ("identical", "frozen", None)}, record_fp=False)
    [s] = stored_bases(c.st, [CENSUS], base_build_id="main-B")[CENSUS]
    assert s.fingerprint_id == c.fp                          # the shard's stored fingerprint for that side
    c.st.update_summary("merge-1", {"reuse_fixture": {}})
    [s] = stored_bases(c.st, [CENSUS], base_build_id="main-B")[CENSUS]
    assert s.key is None and s.fixture_sha256 is None
    c.st.close()


def test_a_merge_run_then_a_pr_run_reuses_its_captures_and_a_lost_artifact_is_reuse_missing(tmp_path):
    c = Central(tmp_path)
    h = c.run("merge-1", "merge", "main-B", "main-A", {CENSUS: ("identical", "frozen", None),
                                                       SWEEP: ("identical", "frozen", None)})
    stored = stored_bases(c.st, [CENSUS, SWEEP], base_build_id="main-B")
    plan = scope_base_reuse(c.key, stored, redaction_version="redact-v3", region_version="reg-7")
    assert {cid: plan.source_for(cid) for cid in (CENSUS, SWEEP)} == {CENSUS: "merge-1", SWEEP: "merge-1"}
    arts = ArtifactStore(c.root, "runs/pr-1")
    Path(arts.path_of(h[SWEEP][0])).unlink()                # the stored object of one checkpoint is gone
    ledger = ReuseLedger()
    loaded = {}
    for cid in (CENSUS, SWEEP):
        d = reuse_decision(cid, c.key, stored.get(cid), redaction_version="redact-v3", region_version="reg-7",
                           expected=plan.source_for(cid) is not None)
        loaded[cid] = reused_base(arts, d.reused)
        ledger.add(d, loaded[cid])
    assert loaded[CENSUS].data == arts.get(h[CENSUS][0]) and loaded[CENSUS].evidence() == BaseEvidence(captured=True)
    assert loaded[SWEEP].data is None and "ArtifactMissing" in loaded[SWEEP].missing
    b = b_input_reason(loaded[SWEEP].evidence(), new_surface=False)
    assert (b.b, b.reason) == ("absent", "reuse_missing")
    assert ledger.base_reused == {CENSUS: "merge-1"} and ledger.reuse_missing == [SWEEP]
    assert ledger.reused_from == "merge-1"
    assert ledger.health_row() == ("Base reuse", "base_reused 1 (from merge-1); re-captured 0; reuse_missing 1")
    # the PR run's records: the reused base names its source run, the lost one is needs_baseline (reuse_missing)
    pair = c.st.register_pair("branch-C", "main-B", "pr")
    c.st.begin_run(RunPlan("pr-1", "pr", "c", "t", "s", "h", "auth/v1", 2700,
                           (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
                            ScopeEntry(SWEEP, WS, "active_trader", 1, "frozen")), pair_id=pair, started_at=T0))
    cand = arts.put(png((9, 9, 9)), name="cand.png", kind="capture", media_type="image/png",
                    redaction=Redaction("redact-v3"))
    common = {"workspace_id": WS, "persona": "active_trader", "data_mode": "frozen", "fingerprint_id": c.fp,
              "data_epoch_id": EPOCH}
    c.st.write_checkpoint(CheckpointOutcome(run_id="pr-1", checkpoint_id=CENSUS, outcome="identical",
                                            artifacts={"candidate": cand.sha256, "base": h[CENSUS][0]},
                                            base_source_run_id=ledger.base_reused[CENSUS], **common),
                          artifacts=[cand])
    c.st.write_checkpoint(CheckpointOutcome(run_id="pr-1", checkpoint_id=SWEEP, outcome="needs_baseline",
                                            reason=b.reason, artifacts={"candidate": cand.sha256}, **common))
    assert c.st.get_outcome("pr-1", CENSUS).base_source_run_id == "merge-1"
    assert c.st.get_outcome("pr-1", SWEEP).reason == "reuse_missing"
    c.st.close()
    s = sample()
    s["context"] = replace(s["context"], base_reused_from=ledger.reused_from)
    text = render_report_markdown(build_report(**s, summary={}))
    assert "; base captures reused from merge-1 |" in text


def test_the_ledger_counts_recaptures_and_is_silent_without_frozen_decisions():
    ledger = ReuseLedger()
    assert ledger.health_row() is None and ledger.reused_from is None
    ledger.add(decide([cap(key=replace(KEY, epoch_id="sha256:old"))]))
    ledger.add(reuse_decision(SWEEP, KEY, [], redaction_version="redact-v3", region_version="reg-7", expected=True))
    assert ledger.health_row() == ("Base reuse", "base_reused 0; re-captured 1; reuse_missing 1")
    assert ledger.reuse_missing == [SWEEP] and ledger.base_reused == {}
