"""Money cells on the Region Registry (4.8, SD-28, SD-29): `RegionsFile.validate` takes a census money cell as a rule
region and refuses it on a flow checkpoint, `Region.money_role` is the closed list, and the lint's L-RGN-08 refuses a
money cell without a role (the half the example migration made enforceable)."""
from __future__ import annotations

from copy import deepcopy

import pytest

from sentinel.demo import EXAMPLE
from sentinel.diff.regions import Rect, Region, RegionError, RegionsFile
from sentinel.lint.repo import Repo
from sentinel.lint.rules import lint_repo

CENSUS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
FLOW = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
S07 = f"regions/{FLOW}.yaml"


def regions(cid: str, **kw) -> RegionsFile:
    from sentinel.diff.regions import Landmark
    import numpy as np
    lm = Landmark("lm.a", np.zeros((16, 16), dtype=np.uint8))
    base = dict(id="rgn.a", type="rule", landmark="lm.a", rect=Rect(0, 0, 40, 20), panel="order_confirmation",
                rule={"field": "price"})
    return RegionsFile(cid, [lm], [Region(**{**base, **kw})])


def test_a_census_money_cell_may_be_a_rule_region_but_a_flow_one_may_not():
    """SD-29: on a census, sweep or split checkpoint every money cell is asserted as a rule region; on a flow
    checkpoint a money cell stays diff or freeze (3.8, L-RGN-04)."""
    regions(CENSUS, money_cell=True, money_role="price").validate()
    with pytest.raises(RegionError, match="SD-29"):
        regions(FLOW, money_cell=True, money_role="price").validate()
    with pytest.raises(RegionError, match="SD-29"):
        regions(CENSUS, type="mask", rule=None, mask_ref="msk.x", money_cell=True, money_role="price").validate()


def test_money_role_is_the_closed_list_and_needs_money_cell():
    """SD-28: `money_role` is one of qty, price, bp_effect, max_loss, send, cancel, delete, set only on a money cell."""
    assert Region.from_dict({"id": "r", "type": "diff", "landmark": "l", "rect": {"dx": 0, "dy": 0, "w": 1, "h": 1},
                             "money_cell": True, "money_role": "bp_effect"}).money_role == "bp_effect"
    with pytest.raises(RegionError, match="L-RGN-08"):
        regions(CENSUS, money_role="price").validate()
    with pytest.raises(RegionError, match="L-RGN-08"):
        regions(CENSUS, money_cell=True, money_role="notional").validate()


def test_the_example_money_cells_name_their_roles_and_sit_on_order_panels():
    """The example migration (SD-28): every money cell names its role; the Position Statement and Active Trader
    quantity columns are no money cells."""
    repo = Repo.load(EXAMPLE)
    cells = [(doc.rel, r) for doc in repo.of("regions") for r in doc.data["regions"] if r.get("money_cell")]
    assert cells and all(r.get("money_role") and r.get("panel") in ("order_confirmation", "order_ticket")
                         for _, r in cells)
    assert not [r["id"] for _, r in cells if r["id"] in ("rgn.ps.qty", "rgn.ladder.qty", "rgn.atcell.qty")]
    roles = {r["id"]: r["money_role"] for rel, r in cells if rel == S07}
    assert roles == {"rgn.confirm.qty": "qty", "rgn.confirm.price": "price", "rgn.confirm.bp-effect": "bp_effect",
                     "rgn.confirm.max-loss": "max_loss", "rgn.confirm.send-button": "send",
                     "rgn.confirm.cancel-button": "cancel", "rgn.confirm.delete-button": "delete"}


def test_l_rgn_08_refuses_a_money_cell_without_a_role():
    """L-RGN-08, both halves: a `money_cell: true` region needs a `money_role` (SD-28)."""
    repo = Repo.load(EXAMPLE)
    assert "L-RGN-08" not in lint_repo(repo).rules()
    bad = Repo(repo.root, dict(repo.docs))
    doc = deepcopy(bad.docs[S07])
    cell = next(r for r in doc.data["regions"] if r["id"] == "rgn.confirm.max-loss")
    del cell["money_role"]
    bad.docs[S07] = doc
    (found,) = [v for v in lint_repo(bad).violations if v.rule == "L-RGN-08"]
    assert (found.file, found.severity) == (S07, "error") and found.path.endswith("].money_role")
    assert "rgn.confirm.max-loss is a money cell without a money_role" in found.message
