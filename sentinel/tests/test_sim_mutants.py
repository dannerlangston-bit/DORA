"""Every catalog mutant rendered and scored on the simulated app (10.3 Injection, 12.3, 12.5; SD-28, SD-43, SD-90).

Each image mutant is the candidate capture of its checkpoint against the approved capture, through the Diff Engine,
the Invariant Checker, the Oracle and classification (`pipeline.run.evaluate`, as the Nightly pass runs it), with the
mutant's bounds model and screen read from its pixels (`sim.mutant_capture`) and the regions file's declared
invariant inputs (`sim.declared_rules`)."""
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest

from sentinel import canary as cn
from sentinel import invariants as inv
from sentinel.demo import BASE_ID, EXAMPLE, blind_overlay
from sentinel.diff.settle import RerunBudget
from sentinel.pipeline import sim
from sentinel.pipeline.assertions import FlowOracle
from sentinel.pipeline.config import SentinelConfig, materialize
from sentinel.pipeline.run import _recapturable, _Run, evaluate, planned_for
from sentinel.pipeline.sim import SimBuild

NOW = datetime(2026, 11, 10, 8, 12, tzinfo=timezone.utc)
S07 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
LADDER = "flow/trade.active-trader.ladder/flow.trade.active-trader.read-back/cp-ladder"
SWEEP = "sweep/trade.active-trader.ladder/flexgrid/480x360"
# scored on another path of the Nightly run: the redaction hit (M19), the batch pre-pass of 3.7 (M21, M23), the
# gateway's step status (M28)
OTHER_PATH = {"M19", "M21", "M23", "M28"}


class Nightly:
    """The canary pass of one Nightly run on the example config, per mutant: the capture and what the path made."""

    def __init__(self, workdir, config=EXAMPLE, mutants=None):
        self.cfg = SentinelConfig.load(config)
        b = SimBuild(BASE_ID)
        self.run = _Run(self.cfg, "run-2026-11-10-nightly-01", "nightly", b, b, NOW, None, None,
                        lambda s: "undeclared")
        self.entries = [e for e in self.cfg.catalog.entries if mutants is None or e.mutant_id in mutants]
        bound = sorted({e.checkpoint_id for e in self.entries} | {e.params["donor_checkpoint"] for e in self.entries
                                                                   if e.params.get("donor_checkpoint")})
        self.cps = {cid: planned_for(self.cfg, cid) for cid in bound}
        self.run.budget = RerunBudget(len(self.cps), self.run.params)
        self.run.calibrate(list(self.cps.values()), workdir)
        self.approved = {cid: self.run.capture(cp, "base") for cid, cp in self.cps.items()}
        self.boxes = {}
        for cid, cp in self.cps.items():
            reg = self.run.registry(cp, self.approved[cid])
            lm = {m.id: m.expected for m in reg.landmarks}
            self.boxes[cid] = {r.id: (r.rect.x + lm[r.landmark][0], r.rect.y + lm[r.landmark][1], r.rect.w, r.rect.h)
                               for r in reg.regions if lm.get(r.landmark) is not None}
            self.run.cp_rules[cid] = sim.declared_rules(self.run.surface_rules(cp), reg, self.approved[cid])
        self.oracle = FlowOracle.load(self.cfg, NOW)
        gen = cn.generate(self.entries, {cid: c.screen.image for cid, c in self.approved.items()}, self.boxes)
        self.mutants = {m.key: m for m in gen.mutants}
        self.caps, self.evals = {}, {}
        for m in gen.mutants:
            assert not m.failed, (m.mutant_id, m.error)
            base, cp = self.approved[m.checkpoint_id], self.cps[m.checkpoint_id]
            cap = self.caps[m.mutant_id] = sim.mutant_capture(base, m)
            self.evals[m.mutant_id] = evaluate(self.run, cp, base, cap, x="undeclared", primitive=None,
                                               entry_ref=None, recapture=lambda c=cap: c,
                                               recapturable=_recapturable(self.cfg, cp), synthetic=True,
                                               injected=True, oracle=self.oracle)

    def fired(self, mid):
        ev = self.evals[mid]
        return set(ev.comparison.invariant_ids if ev.comparison is not None else ())

    def failed(self, mid):
        ev = self.evals[mid]
        return {r.assertion_id for r in ev.oracle.failed()} if ev.oracle is not None else set()


@pytest.fixture(scope="module")
def nightly(tmp_path_factory):
    return Nightly(tmp_path_factory.mktemp("nightly"))


def test_the_catalog_holds_all_33_mutants_and_every_one_is_generated(nightly):
    assert sorted(e.mutant_id for e in nightly.entries) == [f"M{i:02d}" for i in range(1, 34)]
    assert sorted(nightly.evals) == [f"M{i:02d}" for i in range(1, 34)]


@pytest.mark.parametrize("mid", [f"M{i:02d}" for i in range(1, 34) if f"M{i:02d}" not in OTHER_PATH])
def test_each_mutant_ends_in_its_expected_outcome_with_its_signal(nightly, mid):
    """10.3 Scoring: the final outcome is in `expected`; the invariant or assertion it names fired (12.3: every
    invariant has a proving mutant)."""
    entry = next(e for e in nightly.entries if e.mutant_id == mid)
    ev = nightly.evals[mid]
    assert ev.classified.outcome in entry.expected.outcomes, (mid, ev.classified.outcome, ev.classified.reason)
    if entry.expected.invariant_id:
        assert entry.expected.invariant_id in nightly.fired(mid)
    if entry.expected.assertion_id:
        assert entry.expected.assertion_id in nightly.failed(mid)
        assert not nightly.fired(mid)                     # an invariant would outrank the Oracle (3.11 row 7)


def test_the_money_cell_mutants_fail_the_oracle_assertion_on_their_cell(nightly):
    """10.3: M03, M04 and M05 are caught by the Oracle reading the mutated pixels, not by a collateral diff."""
    for mid, aid, region, base_text in (("M03", "qty", "rgn.confirm.qty", "100"),
                                        ("M04", "R02", "rgn.confirm.bp-effect", "-15,025.00"),
                                        ("M05", "price", "rgn.confirm.price", "150.25")):
        ev = nightly.evals[mid]
        assert (ev.classified.outcome, ev.classified.reason) == ("assertion_failed", f"assertion:{aid}")
        assert ev.oracle_base.values[region] == base_text != ev.oracle.values[region]


def test_the_proving_mutants_of_inv06_to_inv15_fire_them(nightly):
    """12.3: INV-06 M17, INV-07 M16, INV-08 M15, INV-12 M31, INV-13 M32, INV-14 M33, INV-15 M07."""
    for mid, inv_id in (("M17", "INV-06"), ("M16", "INV-07"), ("M15", "INV-08"), ("M31", "INV-12"),
                        ("M32", "INV-13"), ("M33", "INV-14"), ("M07", "INV-15")):
        assert inv_id in nightly.fired(mid), (mid, nightly.fired(mid))


def test_the_controls_absorb_and_the_aa_control_reads_identical(nightly):
    assert nightly.evals["M20"].classified.outcome in ("identical", "noise")
    m25 = nightly.caps["M25"]
    assert m25.model == nightly.approved[S07].model and nightly.evals["M25"].classified.outcome == "identical"


def test_m29_moves_the_button_across_its_strip_border_and_the_bounds_agree(nightly):
    """04b: the generator moves BUY MKT toward the strip's nearest border (down: 2 px of room against 4 above) and
    the bounds model follows the pixels the same way, so INV-02 fires on the button against the strip."""
    base = {c.name: c.bbox for c in nightly.approved[SWEEP].model.components}
    moved = {c.name: c.bbox for c in nightly.caps["M29"].model.components}
    assert (moved["ladder.buy"].x, moved["ladder.buy"].y) == (base["ladder.buy"].x, base["ladder.buy"].y + 6)
    assert moved["ladder.strip"] == base["ladder.strip"]
    rep = nightly.evals["M29"].comparison.reported
    assert any(r.violation.invariant_id == "INV-02" and r.violation.subject == "ladder.buy" for r in rep)


def test_the_screen_the_oracle_and_reviewer_read_is_the_mutants(nightly):
    """10.3: the simulated Oracle reads the mutant's pixels; the simulated Reviewer's change notes see the change."""
    base, cap = nightly.approved[S07], nightly.caps["M06"]
    reg = nightly.run.registry(nightly.cps[S07], base)
    lm = {m.id: m.expected for m in reg.landmarks}
    assert sim.region_values(base.screen, reg, lm)["rgn.confirm.side"] == "BUY"
    assert sim.region_values(cap.screen, reg, lm)["rgn.confirm.side"] == "SELL"
    obs = sim.observe(base.screen, cap.screen)
    assert any(o.change_type == "text_change" and "'BUY' now reads 'SELL'" in o.text for o in obs)


def test_ab_enablement_leaves_every_invariant_on_where_mutants_bind(nightly):
    """07-inv05, 2.7: on a faithful screen the A/A enablement test keeps every invariant on, INV-05 on the ladders
    included, with the declared inputs in force."""
    assert nightly.run.disabled == {}
    for cid, cp in nightly.cps.items():
        a, b = (nightly.run.capture(cp, "base", tick=10 + k) for k in range(2))
        rules = nightly.run.cp_rules[cid]
        st = inv.enablement([inv.AAPair(cp.data_mode, a.models, b.models)], rules,
                            config=nightly.run.checker(cp))
        assert all(v == inv.ENABLED for v in st.values()), (cid, st)


def test_inv05_still_fires_when_a_ladder_column_label_is_erased(nightly):
    base, cp = nightly.approved[LADDER], nightly.cps[LADDER]
    box = nightly.boxes[LADDER]["rgn.ladder.header-ask"]
    erased = cn.Mutant(cn.parse_entry({"mutant_id": "M34", "checkpoint_id": LADDER, "class": "erased_control",
                                       "mode": "image", "money_cell": False, "region_ref": "rgn.ladder.header-ask",
                                       "generator": {"op": "erase", "params": {}, "seed": cn.SEED},
                                       "expected": {"outcomes": ["invariant_violation"], "invariant_id": "INV-05"},
                                       "owner": "sentinel-canaries"}), LADDER, cn.SEED)
    erased.image = cn.erase(base.screen.image, box, np.random.default_rng(0))
    cap = sim.mutant_capture(base, erased)
    res = inv.compare(base.models, cap.models, nightly.run.cp_rules[LADDER], config=nightly.run.checker(cp))
    assert [r.violation.subject for r in res.reported if r.violation.invariant_id == "INV-05"] == ["Ask Size"]


def test_an_aa_control_bound_to_every_checkpoint_reads_identical_on_each(nightly):
    """SD-43, 12.5 M25: checkpoint_id '*' expands to every checkpoint an image mutant binds; each control is the
    approved capture unchanged and ends identical."""
    star = cn.parse_entry({"mutant_id": "M25", "checkpoint_id": "*", "class": "aa_control", "mode": "image",
                           "money_cell": False, "generator": {"op": "translate", "params": {}, "seed": cn.SEED},
                           "expected": {"outcomes": ["identical"]}, "owner": "sentinel-canaries"})
    images = [e for e in nightly.entries if e.mode == "image"]
    gen = cn.generate(images + [star], {cid: c.screen.image for cid, c in nightly.approved.items()}, nightly.boxes)
    controls = [m for m in gen.mutants if m.entry is star]
    assert sorted(m.checkpoint_id for m in controls) == sorted({e.checkpoint_id for e in images})
    for m in controls:
        base, cp = nightly.approved[m.checkpoint_id], nightly.cps[m.checkpoint_id]
        cap = sim.mutant_capture(base, m)
        ev = evaluate(nightly.run, cp, base, cap, x="undeclared", primitive=None, entry_ref=None,
                      recapture=lambda c=cap: c, synthetic=True, injected=True, oracle=nightly.oracle)
        assert ev.classified.outcome == "identical", (m.checkpoint_id, ev.classified)


def test_the_oracle_catches_m04_under_a_mask_until_r02_is_dropped(tmp_path):
    """10.3 Scoring, the demo's blind night: a lint-clean edit masks the Buying Power Effect cell and drops its money
    flag, so the swapped digit is no longer a diff; the R02 assertion still reads it from the pixels. Only an overlay
    that also drops R02 leaves the path with nothing: M04 ends identical, the produced outcome of the miss."""
    flow = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"
    text = (EXAMPLE / flow).read_text(encoding="utf-8")
    line = ("      - {id: R02, kind: rule, rule_id: R02, inputs: {bp_effect: rgn.confirm.bp-effect, "
            "notional: rgn.confirm.cost}}\n")
    assert line in text
    masked = Nightly(tmp_path, materialize(EXAMPLE, blind_overlay(EXAMPLE), tmp_path / "masked"), {"M04"})
    assert (masked.evals["M04"].classified.outcome, masked.failed("M04")) == ("assertion_failed", {"R02"})
    blind = Nightly(tmp_path, materialize(EXAMPLE, dict(blind_overlay(EXAMPLE), **{flow: text.replace(line, "")}),
                                          tmp_path / "blind"), {"M04"})
    assert blind.evals["M04"].classified.outcome == "identical" and not blind.failed("M04")


def test_declared_rules_add_value_text_regions_and_column_alignment(nightly):
    """Declared regions (module docstring): rule regions are value regions, one-row regions are text regions, a
    column's alignment is the edge its base cells share."""
    cid = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
    rules = nightly.run.cp_rules[cid]
    assert {r.id for r in rules.value_regions} == {"rgn.ps.mark", "rgn.ps.pl-open"}
    assert {r.id for r in rules.text_regions} == {"rgn.ps.header", "rgn.ps.header-mark", "rgn.ps.trade-price-r3"}
    assert rules.grid_columns == {"Qty": "left", "Trade Price": "right", "Mark": "right", "P/L Open": "right",
                                  "Instrument": "right"}
    plain = nightly.run.effective.surface_rules(nightly.cps[cid])
    assert replace(rules, value_regions=(), text_regions=(), grid_columns={}, calibration_pending=frozenset()) == \
        replace(plain, calibration_pending=frozenset())
