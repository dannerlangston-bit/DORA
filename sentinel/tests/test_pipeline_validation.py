"""Registry validation wired into the simulated PR run (2.2.3, SD-15; w3-final item 1): the rig's probe lists at the
`canary_flow_passed` hook, the probes' census rows and the unregistered labels, input B for removed and new entries,
rename candidates, entries listed on neither build, unlocated anchors, and the Reporter's proposals."""
from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from sentinel.demo import EXAMPLE, PS_CENSUS, clean_scenario
from sentinel.pipeline.orchestration import RigScript
from sentinel.pipeline.run import run_pipeline
from sentinel.store import Store

TODAY = date(2026, 11, 10)
WS = "ws.tier-a.active-trader"
PS = "monitor.activity.position-statement"


def probe(n: int) -> str:
    return f"census/registry.validation-p{n}/{WS}"


def _run(tmp_path, **script):
    sc = dataclasses.replace(clean_scenario(TODAY), rig=RigScript(**script))
    r = run_pipeline(EXAMPLE, sc, tmp_path, today=TODAY)
    with Store(tmp_path / "sentinel.db") as store:
        return (r, {o.checkpoint_id: o for o in store.outcomes(r.run_id)}, store.findings(r.run_id),
                store.run_summary(r.run_id), [tuple(x) for x in store.query(
                    "SELECT kind, ref, owner FROM maintenance_items WHERE run_id = ? AND kind <> 'sent_ticket'",
                    (r.run_id,))])                 # less the SENT task ledger (report.jira_sync, 9.9)


@pytest.fixture(scope="module")
def removed_and_renamed(tmp_path_factory):
    """The candidate no longer lists the Position Statement (P2) and lists the Order Entry ticket (P6) renamed."""
    return _run(tmp_path_factory.mktemp("val-a"), probe_removed=(("P2", "Position Statement"),),
                probe_renamed=(("P6", "Order Entry", "Order Entries"),))


def test_a_clean_run_validates_every_probe_once_identical(tmp_path):
    r, by, finds, summary, tasks = _run(tmp_path)
    assert {n: by[probe(n)].outcome for n in range(1, 9)} == {n: "identical" for n in range(1, 9)}
    assert all(by[probe(n)].artifacts["base"] and by[probe(n)].artifacts["candidate"] for n in range(1, 9))
    assert r.status == "complete" and "registry_proposals" not in summary and tasks == []
    assert not [f for f in finds if f.kind == "unregistered_surface"]


def test_a_removed_entrys_census_visit_is_not_attempted_and_is_row_26_collateral(removed_and_renamed):
    """2.2.3 removed row, 3.11 rows 24 to 26: input B `removed`; with no manifest entry declaring the removal the
    visit is `collateral` and the two probe screenshots replace `diff`."""
    r, by, *_ = removed_and_renamed
    ps = by[PS_CENSUS]
    assert (ps.outcome, ps.diff, ps.classification.by) == ("collateral", None, "rule")
    assert ps.artifacts == by[probe(2)].artifacts and "removed surface" in ps.detail
    assert by[probe(2)].outcome == "collateral"


def test_a_renamed_label_pairs_with_its_entry_and_proposes_an_alias(removed_and_renamed):
    """2.2.3 pairing rule: "Order Entries" pairs with trade.order-entry.stock (edit distance at most 0.3): the label
    keeps its `unregistered_surface` record with the rename candidate, an alias PR is proposed, and the entry's
    checkpoints are not taken as removed."""
    r, by, finds, summary, _ = removed_and_renamed
    u = by[f"census/unregistered.p6.order-entries/{WS}"]
    assert (u.outcome, u.reason) == ("unregistered_surface", "probe:P6")
    assert "rename_candidate: trade.order-entry.stock" in u.detail
    (alias,) = [p for p in summary["registry_proposals"] if p["kind"] == "alias"]
    assert alias["document"] == {"id": "trade.order-entry.stock", "aliases": ["Order Entries"]}
    (f,) = [f for f in finds if f.checkpoint_id == u.checkpoint_id]
    assert "may rename trade.order-entry.stock" in f.description
    assert not [o for o in by.values() if o.surface_id == "trade.order-entry.stock" and o.outcome == "collateral"
                and o.diff is None]


def test_an_entry_listed_on_the_candidate_only_has_no_base(tmp_path):
    """2.2.3, 3.11 input B: an entry the base build does not list and the candidate does is `absent
    (new_surface)`, so its checkpoints are `needs_baseline (new_surface)` on the base side."""
    r, by, *_ = _run(tmp_path, probe_hidden=(("P2", "Position Statement"),),
                     probe_added=(("P2", "Position Statement"),))
    ps = by[PS_CENSUS]
    assert (ps.outcome, ps.reason, ps.side) == ("needs_baseline", "new_surface", "base")


def test_an_entry_on_neither_build_and_an_unlocated_anchor(tmp_path):
    """2.2.3: an entry listed on neither build while its entitlements are present is `harness_error
    (registry_entry_not_found)` on every checkpoint of its surface, with the owner's maintenance task and the system
    owner told; an anchor not found is the probe's `landmark_missing` and run health says validation is incomplete."""
    r, by, finds, summary, tasks = _run(tmp_path, probe_hidden=(("P2", "Position Statement"),),
                                        probe_unlocated=(("P8", "candidate"),))
    ps = [o for o in by.values() if o.surface_id == PS]
    assert ps and {(o.outcome, o.reason) for o in ps} == {("harness_error", "registry_entry_not_found")}
    assert all("watchdog_event:" in o.detail for o in ps)
    assert tasks == [("registry_entry_not_found", PS, "tos-monitor-ui")]
    p8 = by[probe(8)]
    assert (p8.outcome, p8.reason, p8.side) == ("landmark_missing", "probe:P8", "candidate")
    rows = [text for label, text in summary["health_rows"] if label == "Registry validation"]
    assert rows == ["validation incomplete for P8: landmark_missing (probe:P8) on candidate"]
    (alert,) = [a for a in r.alerts if a["control"] == "registry_validation"]
    assert alert["rule"] == "harness_error" and "system_owner" in alert["notified"]
