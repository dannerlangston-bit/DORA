"""The config mirror (8.3, 8.5): regions and masks at the run's commit, L-DEC-04 on money cells and the mask budget,
the request_mask link (9.6), and checkpoint renames (8.3, gap G33)."""
from __future__ import annotations

import copy

import pytest

from sentinel.diff.regions import MaskRecord
from sentinel.records import Decision, Finding, Signature
from sentinel.store import RunPlan, ScopeEntry, Store, StoreRefused
from sentinel.store.lifecycle import expire_due
from sentinel.store.mirror import (MaskBudgetExceeded, MirrorRefused, apply_renames, approved_baseline,
                                   flake_stats_for, link_mask, load_config_mirror, record_request_mask,
                                   request_mask_check)

RUN = "run-01"
COMMIT = "c0ffee"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"                          # tier 1
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"   # tier 2
OLD = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/cp1"
PH = "0123456789abcdef"
TS = "2026-11-05T15:20:00+00:00"
BUDGETS = {"order_confirmation": 0.05}


def rect(dx, dy, w, h):
    return {"dx": dx, "dy": dy, "w": w, "h": h}


def regions_doc(**changes) -> dict:
    """The s07 regions file: panel bounds 470 x 216 (101520 px), a clock mask of 1440 px."""
    doc = {"schema_version": 1, "checkpoint_id": FLOW,
           "landmarks": [{"id": "lm.title", "template": "regions/landmarks/lm.title.png"}],
           "regions": [
               {"id": "rgn.confirm.title", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(0, 0, 200, 20)},
               {"id": "rgn.confirm.qty", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(90, 48, 48, 20), "money_role": "qty", "money_cell": True},
               {"id": "rgn.confirm.symbol", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(144, 48, 110, 20)},
               {"id": "rgn.confirm.cost", "type": "diff", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(24, 96, 200, 20)},
               {"id": "rgn.confirm.timestamp", "type": "rule", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(260, 200, 110, 16), "rule": {"field": "time"}},
               {"id": "rgn.confirm.clock", "type": "mask", "landmark": "lm.title", "panel": "order_confirmation",
                "rect": rect(380, 0, 90, 16), "mask_ref": "msk.clock-01"}]}
    for rid, fields in changes.items():
        region = next(r for r in doc["regions"] if r["id"] == rid.replace("_", "."))
        region.update(fields)
    return doc


REDACTION = {"schema_version": 1, "owner": "security-contact",
             "landmarks": [{"id": "lm.title", "template": "x.png"}, {"id": "lm.other", "template": "y.png"}],
             "regions": [
                 {"id": "rgn.redact.account", "type": "redaction", "landmark": "lm.title",
                  "panel": "order_confirmation", "rect": rect(24, 24, 120, 16), "identity_field": True},
                 {"id": "rgn.redact.note", "type": "redaction", "landmark": "lm.title", "panel": "order_confirmation",
                  "rect": rect(300, 100, 50, 20)},                                  # 1000 px, counts
                 {"id": "rgn.redact.elsewhere", "type": "redaction", "landmark": "lm.other",
                  "rect": rect(0, 0, 10, 10)}]}


def mask(mask_id="msk.clock-01", region_id="rgn.confirm.clock", decision_ref="dec-000812", expires="2026-12-05"):
    return MaskRecord.from_dict({"mask_id": mask_id, "checkpoint_id": FLOW, "region_id": region_id,
                                 "owner": "tos-trade-ui", "reason": "the clock ticks", "created_by": "human",
                                 "created_run": "run-2026-11-05-merge-0418", "created_at": "2026-11-05T15:20:00-05:00",
                                 "expires_at": f"{expires}T15:20:00-05:00", "decision_ref": decision_ref})


def scope(*extra):
    return (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),
            ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard"), *extra)


def plan(run_id=RUN, entries=None):
    return RunPlan(run_id=run_id, run_type="merge", config_commit=COMMIT, config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=entries or scope(), started_at=TS)


def decision(did, action, region, phash=PH, cid=FLOW, by="alice", **kw):
    verdict = {"confirm_bug": "confirmed_bug", "mark_intended": "intended"}.get(action, "noise")
    ticket = "TOS-1" if action in ("confirm_bug", "mark_intended") else None
    return Decision(did, action, verdict, Signature(cid, region, phash, "collateral"), decided_by=by, ticket=ticket,
                    reason="because", decided_at=TS, **kw)


def finding(s, fid, region, cid=FLOW):
    s.write_finding(Finding(fid, RUN, "collateral", "moved", RUN, checkpoint_id=cid, tier=2, region_id=region,
                            phash=PH, category="collateral", created_at=TS))


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "central.db")
    st.begin_run(plan())
    yield st
    st.close()


def floor(region, tolerance, key, fallback=0):
    return {"checkpoint_id": FLOW, "region_id": region, "rendering_key_sha256": key, "calibration_run_id": RUN,
            "samples": 15, "threshold_component_area": 4, "threshold_changed_px": 4, "threshold_delta_e": 2.0,
            "threshold_shift_px": 1, "noise_spread": 2, "fault_separation": 9, "hamming_tolerance": tolerance,
            "exact_fallback": fallback, "valid_from": TS}


# -- the mirror (8.5) -----------------------------------------------------------------------------------------------

def test_the_mirror_holds_regions_redactions_and_masks_at_the_commit(s):
    s.write_calibration([floor("rgn.confirm.symbol", 5, "sha256:k1"), floor("rgn.confirm.symbol", 3, "sha256:k2", 1)])
    r = load_config_mirror(s, COMMIT, {FLOW: regions_doc()}, {"msk.clock-01": mask()}, REDACTION)
    assert (r.regions, r.masks, r.linked, r.unlinked) == (8, 1, (), ("msk.clock-01",))   # dec-000812 is unknown here
    rows = {x["region_id"]: x for x in s.query("SELECT * FROM regions WHERE config_commit = ?", (COMMIT,))}
    assert set(rows) == {"rgn.confirm.title", "rgn.confirm.qty", "rgn.confirm.symbol", "rgn.confirm.cost",
                         "rgn.confirm.timestamp", "rgn.confirm.clock", "rgn.redact.account", "rgn.redact.note"}
    q = rows["rgn.confirm.qty"]
    assert (q["kind"], q["money_cell"], q["money_role"], q["dx"], q["w"]) == ("diff", 1, "qty", 90, 48)
    assert (rows["rgn.redact.account"]["identity_field"], rows["rgn.redact.note"]["identity_field"]) == (1, 0)
    sym = rows["rgn.confirm.symbol"]
    assert (sym["hamming_tolerance"], sym["exact_fallback"]) == (3, 1)        # the lowest current floor (9.7)
    assert rows["rgn.confirm.title"]["hamming_tolerance"] is None
    m = s.query("SELECT * FROM masks")[0]
    assert (m["mask_id"], m["landmark_id"], m["dx"], m["w"], m["h"], m["critical_panel"], m["decision_id"]) == (
        "msk.clock-01", "lm.title", 380, 90, 16, "order_confirmation", None)
    assert m["expires_at"] == "2026-12-05T20:20:00+00:00"
    again = load_config_mirror(s, COMMIT, {FLOW: regions_doc()}, {"msk.clock-01": mask()}, REDACTION)
    assert (again.regions, again.masks) == (0, 0)                              # a commit is loaded once
    assert load_config_mirror(s, "c2", {FLOW: regions_doc()}).regions == 6    # another commit, its own rows


def test_l_dec_04_a_money_cell_takes_no_noise_or_mask_and_calibration_clamps_tolerance(s):
    s.write_calibration([floor("rgn.confirm.symbol", 3, "sha256:k1")])
    load_config_mirror(s, COMMIT, {FLOW: regions_doc()})
    for action in ("noise_signature", "request_mask"):
        with pytest.raises(StoreRefused, match="L-DEC-04: rgn.confirm.qty"):
            s.record_decision(decision("dec-000001", action, "rgn.confirm.qty"), run_id=RUN)
    assert s.query("SELECT count(*) FROM decisions")[0][0] == 0
    assert s.record_decision(decision("dec-000001", "confirm_bug", "rgn.confirm.qty"), run_id=RUN).status == "active"
    assert s.record_decision(decision("dec-000002", "noise_signature", "rgn.confirm.symbol"),
                             run_id=RUN).hamming_tolerance == 3


def test_an_exact_fallback_region_stores_tolerance_0_with_the_box_and_matches_only_it(s):
    """4.14, 9.7, L-DEC-03: a region whose tolerance is at or below its noise spread falls back to pHash equality
    plus the largest component's 4 px box; the decision must carry `bbox_q4` and the lookup requires it equal."""
    s.write_calibration([floor("rgn.confirm.symbol", 3, "sha256:k1", fallback=1)])
    load_config_mirror(s, COMMIT, {FLOW: regions_doc()})
    with pytest.raises(StoreRefused, match="L-DEC-03"):
        s.record_decision(decision("dec-000001", "noise_signature", "rgn.confirm.symbol"), run_id=RUN)
    d = s.record_decision(decision("dec-000001", "noise_signature", "rgn.confirm.symbol", bbox_q4=(8, 4, 16, 12)),
                          run_id=RUN)
    assert (d.hamming_tolerance, d.bbox_q4) == (0, (8, 4, 16, 12))
    sig = Signature(FLOW, "rgn.confirm.symbol", PH, "collateral")
    near = Signature(FLOW, "rgn.confirm.symbol", PH[:-1] + "e", "collateral")          # one bit away
    assert [m.decision.decision_id for m in s.lookup_decisions(sig, at=TS, bbox_q4=(8, 4, 16, 12)).matches] == [
        "dec-000001"]
    assert s.lookup_decisions(sig, at=TS, bbox_q4=(8, 4, 20, 12)).matches == ()          # another box
    assert s.lookup_decisions(sig, at=TS).matches == ()                                  # no box: no exact match
    assert s.lookup_decisions(near, at=TS, bbox_q4=(8, 4, 16, 12)).matches == ()
    other = s.record_decision(decision("dec-000002", "noise_signature", "rgn.confirm.title"), run_id=RUN)
    title = Signature(FLOW, "rgn.confirm.title", PH, "collateral")
    assert other.hamming_tolerance == 8 and s.lookup_decisions(title, at=TS).matches[0].decision.decision_id == \
        "dec-000002"                                                                    # a tolerance needs no box


def test_the_mirror_refuses_what_its_tables_cannot_hold(s):
    no_role = regions_doc(rgn_confirm_qty={"money_role": None})
    with pytest.raises(MirrorRefused, match="L-RGN-08"):
        load_config_mirror(s, COMMIT, {FLOW: no_role})
    with pytest.raises(MirrorRefused, match="no mask region"):
        load_config_mirror(s, COMMIT, {FLOW: regions_doc()}, [mask(region_id="rgn.confirm.cost")])
    assert s.query("SELECT count(*) FROM regions")[0][0] == 0                  # nothing written
    role_only = regions_doc(rgn_confirm_qty={"money_cell": False})
    load_config_mirror(s, COMMIT, {FLOW: role_only})
    assert s.query("SELECT money_cell FROM regions WHERE region_id = 'rgn.confirm.qty'")[0][0] == 1


# -- request_mask: the budget refusal and the link at merge (L-DEC-04, 8.3, 9.6) ------------------------------------

def test_a_request_mask_over_the_panel_budget_is_refused_with_its_three_numbers(s):
    load_config_mirror(s, COMMIT, {FLOW: regions_doc()})
    finding(s, "F-cost", "rgn.confirm.cost")
    with pytest.raises(MaskBudgetExceeded) as e:
        record_request_mask(s, decision("dec-000001", "request_mask", "rgn.confirm.cost"), run_id=RUN,
                            finding_id="F-cost", registry={FLOW: regions_doc()}, redaction=REDACTION, budgets=BUDGETS)
    x = e.value
    assert (x.panel, x.landmark, x.budget) == ("order_confirmation", "lm.title", 0.05)
    assert x.current == pytest.approx(2440 / 101520) and x.mask_share == pytest.approx(4000 / 101520)
    assert "current share 0.024" in str(x) and "budget 0.050" in str(x)
    assert s.query("SELECT count(*) FROM decisions")[0][0] == 0                # no row (and no mask PR)
    ok = request_mask_check(s, "F-cost", registry={FLOW: regions_doc()}, redaction=REDACTION,
                            budgets={"order_confirmation": 0.10})
    assert ok.mask_share == pytest.approx(4000 / 101520) and ok.budget == 0.10


def test_a_request_mask_on_or_over_a_money_cell_is_refused(s):
    load_config_mirror(s, COMMIT, {FLOW: regions_doc()})
    finding(s, "F-qty", "rgn.confirm.qty")
    finding(s, "F-sym", "rgn.confirm.symbol")
    with pytest.raises(StoreRefused, match="L-DEC-04"):
        request_mask_check(s, "F-qty", registry={FLOW: regions_doc()})
    with pytest.raises(StoreRefused, match="money cell rgn.confirm.qty"):
        request_mask_check(s, "F-sym", registry={FLOW: regions_doc()},
                           rect={"landmark": "lm.title", "rect": rect(100, 40, 60, 30)})
    finding(s, "F-grid", "g-3-4")
    with pytest.raises(StoreRefused, match="names its mask rect"):
        request_mask_check(s, "F-grid", registry={FLOW: regions_doc()})


def test_a_request_mask_acts_once_its_mask_merges(s):
    load_config_mirror(s, COMMIT, {FLOW: regions_doc()})
    finding(s, "F-sym", "rgn.confirm.symbol")
    d = record_request_mask(s, decision("dec-000001", "request_mask", "rgn.confirm.symbol"), run_id=RUN,
                            finding_id="F-sym", registry={FLOW: regions_doc()}, redaction=REDACTION)
    assert s.get_finding("F-sym").triage_state == "open"                      # open until the merge
    sig = Signature(FLOW, "rgn.confirm.symbol", PH, "collateral")
    assert s.lookup_decisions(sig).matches == ()                              # inert before the merge
    merged = regions_doc(rgn_confirm_symbol={"type": "mask", "mask_ref": "msk.symbol-01"})
    r = load_config_mirror(s, "c2", {FLOW: merged},
                           [mask(), mask("msk.symbol-01", "rgn.confirm.symbol", d.decision_id, expires="2026-12-01")])
    assert r.linked == ("dec-000001",) and r.unlinked == ("msk.clock-01",)
    assert s.get_decision("dec-000001").ticket is None
    assert s.query("SELECT mask_ref FROM decisions WHERE decision_id = 1")[0][0] == "msk.symbol-01"
    assert s.get_finding("F-sym").triage_state == "noise"
    assert s.lookup_decisions(sig).inherited.decision_id == "dec-000001"
    row = s.query("SELECT decision_id, created_by FROM masks WHERE mask_id = 'msk.symbol-01'")[0]
    assert (row["decision_id"], row["created_by"]) == (1, "alice")
    assert link_mask(s, "dec-000001", "msk.symbol-01").decision_id == "dec-000001"   # a reload: no-op
    with pytest.raises(StoreRefused, match="set once"):
        link_mask(s, "dec-000001", "msk.symbol-02")
    s.record_decision(decision("dec-000002", "noise_signature", "rgn.confirm.cost"), run_id=RUN)
    with pytest.raises(StoreRefused, match="active request_mask"):
        link_mask(s, "dec-000002", "msk.cost-01")
    # the request ends with its mask (9.6), not at its own 30 days
    assert [e.why for e in expire_due(s, "2026-12-02T00:00:00+00:00").expired] == ["mask expired"]


def test_a_mask_naming_a_revoked_request_loads_unlinked(s):
    load_config_mirror(s, COMMIT, {FLOW: regions_doc()})
    finding(s, "F-sym", "rgn.confirm.symbol")
    d = s.record_decision(decision("dec-000001", "request_mask", "rgn.confirm.symbol"), run_id=RUN, finding_id="F-sym")
    s.revoke_decision(d.decision_id, by="tess", reason="mask_budget_exceeded")
    merged = regions_doc(rgn_confirm_symbol={"type": "mask", "mask_ref": "msk.symbol-01"})
    r = load_config_mirror(s, "c2", {FLOW: merged}, [mask("msk.symbol-01", "rgn.confirm.symbol", d.decision_id)])
    assert (r.masks, r.linked, r.unlinked) == (1, (), ("msk.symbol-01",))
    assert s.query("SELECT mask_ref FROM decisions WHERE decision_id = 1")[0][0] is None
    assert s.get_finding("F-sym").triage_state == "open"


# -- renames (8.3, gap G33) -----------------------------------------------------------------------------------------

def test_a_rename_carries_decisions_baselines_and_flake_stats_and_tombstones_the_old_id(tmp_path):
    old_entry = ScopeEntry(OLD, WS, "active_trader", 2, "frozen", flow_class="standard")
    new_entry = ScopeEntry(FLOW, WS, "active_trader", 2, "frozen", flow_class="standard")
    with Store(tmp_path / "c.db") as s:
        s.begin_run(plan("run-00", (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"), old_entry)))
        s.record_decision(decision("dec-000001", "noise_signature", "rgn.confirm.symbol", cid=OLD), run_id="run-00")
        s.register_build("b1", channel="main", source_commit="b1", pipeline_artifact_url="u", installer_sha256="1")
        s.insert("baselines", {"checkpoint_id": OLD, "lineage": "released", "build_id": "b1", "image_sha256": "a" * 64,
                               "state": "approved", "source_run_id": "run-00", "proposed_at": TS, "approved_by": "fay",
                               "approved_at": TS, "approval_reason": "first capture"})
        s.replace_flake_stats([{"checkpoint_id": OLD, "observations": 30, "flake_score": 0.1, "updated_at": TS}])
        renames = {"schema_version": 1, "renames": [{"old": OLD, "new": FLOW, "build": "b2"},
                                                    {"old": CENSUS, "new": "retired", "build": "b2"},
                                                    {"old": "flow/x.y/flow.z/s01", "new": "retired", "build": "b2"}]}
        r = apply_renames(s, renames, "c2")
        assert (r.renamed, r.retired, r.unknown) == ((FLOW,), (CENSUS,), ("flow/x.y/flow.z/s01",))
        row = s.query("SELECT renamed_from, tier, flow_class FROM checkpoints WHERE checkpoint_id = ?", (FLOW,))[0]
        assert tuple(row) == (OLD, 2, "standard")                              # created from the old row
        old = s.query("SELECT retired_in_build, retired_commit FROM checkpoints WHERE checkpoint_id = ?", (OLD,))[0]
        assert tuple(old) == ("b2", "c2")
        s.begin_run(plan("run-02", (new_entry,)))
        hit = s.lookup_decisions(Signature(FLOW, "rgn.confirm.symbol", PH, "collateral"))
        assert hit.inherited.decision_id == "dec-000001"                       # inherited across the rename
        assert approved_baseline(s, FLOW)["checkpoint_id"] == OLD
        assert flake_stats_for(s, FLOW)["observations"] == 30
        for entries in ((old_entry,), (ScopeEntry(CENSUS, WS, "active_trader", 1, "frozen"),)):
            with pytest.raises(StoreRefused, match="never reused"):
                s.begin_run(plan("run-03", entries))
        with pytest.raises(StoreRefused, match="never reused"):
            s.register_checkpoints([old_entry], config_commit="c3")
        assert apply_renames(s, renames, "c2").renamed == (FLOW,)              # idempotent
        assert s.query("SELECT retired_commit FROM checkpoints WHERE checkpoint_id = ?", (OLD,))[0][0] == "c2"
        other = copy.deepcopy(renames)
        other["renames"] = [{"old": "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/cp0",
                             "new": FLOW, "build": "b3"}]
        with pytest.raises(StoreRefused, match="already renamed"):
            apply_renames(s, other, "c3")


def test_a_rename_whose_old_id_the_store_never_saw_creates_its_tombstone(s):
    assert s.rename_checkpoint(OLD, FLOW, build_id="b2", config_commit="c2")
    row = s.query("SELECT retired_in_build, flow_class, tier FROM checkpoints WHERE checkpoint_id = ?", (OLD,))[0]
    assert tuple(row) == ("b2", "standard", 2)
    assert s.query("SELECT renamed_from FROM checkpoints WHERE checkpoint_id = ?", (FLOW,))[0][0] == OLD
    assert not s.rename_checkpoint("flow/a.b/flow.c/s1", "flow/a.b/flow.c/s2", build_id="b2", config_commit="c2")
