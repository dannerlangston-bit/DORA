"""The decision lifecycle wired into the simulated PR run and the Nightly (w2 wiring item 8): the roster of
`ops/oncall.yaml`, the decision lookup with the candidate build, `verify_fixes` after every pair run (a fixed signature
that returns reopens), novelty (`first_seen`), the config mirror and the renames at run start, and the Nightly's
expiry and decisions export."""
from __future__ import annotations

from datetime import date

from sentinel.demo import EXAMPLE, demo_fixed_reopens
from sentinel.pipeline.run import run_pipeline
from sentinel.store import Store
from sentinel.store.store import Store as StoreClass

TODAY = date(2026, 11, 10)


def test_a_fixed_signature_that_returns_reopens_after_the_run(tmp_path):
    r1, r2 = demo_fixed_reopens(tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        fixed = store.query("SELECT decision_id, status, reopen_count FROM decisions WHERE action = 'fixed_in_build'")
        jobs = store.query("SELECT kind FROM outbox WHERE kind = 'jira_transition'")
    assert fixed and {(d["status"], d["reopen_count"]) for d in fixed} == {("reopened", 1)}
    assert jobs                                                  # the ticket goes back to open (9.9)
    text = (tmp_path / "pr-comment.md").read_text()
    assert "Fixes reopened" in text


def test_the_run_loads_the_roster_and_the_config_mirror(tmp_path, monkeypatch):
    seen = []
    real = StoreClass.set_roster
    monkeypatch.setattr(StoreClass, "set_roster", lambda self, r: (seen.append(r), real(self, r))[1])
    from sentinel.demo import wrong_number_scenario            # its scope holds s03, the renamed checkpoint
    r = run_pipeline(EXAMPLE, wrong_number_scenario(TODAY), tmp_path)
    assert seen and "triage.lead" in seen[0].triage
    with Store(tmp_path / "sentinel.db") as store:
        regions = store.query("SELECT count(*) AS n FROM regions")[0]["n"]
        renamed = store.query("SELECT count(*) AS n FROM checkpoints WHERE renamed_from IS NOT NULL")[0]["n"]
    assert regions > 0 and renamed >= 1                          # 8.5 mirror, flows/RENAMES.yaml (8.3)
    assert r.status == "complete" and r.counts()["assertion_failed"] == 1


def test_a_signature_seen_before_keeps_its_first_run(tmp_path):
    from sentinel.demo import flexgrid_scenario
    r1 = run_pipeline(EXAMPLE, flexgrid_scenario(TODAY), tmp_path)
    r2 = run_pipeline(EXAMPLE, flexgrid_scenario(TODAY, seq=2, sha7="9c4f1e7"), tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        later = [f for f in store.findings(r2.run_id) if f.kind == "invariant_violation"]
    assert later and {f.first_seen_run_id for f in later} == {r1.run_id}       # 9.2 novelty
