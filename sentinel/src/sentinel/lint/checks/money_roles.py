"""L-RGN-08: the closed money-cell list on regions files (4.8 `money_role`, SD-28).

Money cells are one closed list of roles, `taxonomy.yaml` `money_roles`: Qty, Price, Buying Power Effect, Max Loss
and the Send, Cancel and Delete buttons (`qty`, `price`, `bp_effect`, `max_loss`, `send`, `cancel`, `delete`; the
ticket's Confirm and Send button is `send`). A region names its role with `money_role`; `money_cell` is true exactly
when `money_role` is set, and a role sits only on a region whose `panel` is `order_confirmation` or `order_ticket`.
The role values themselves are the schema's enum (L-RGN-00).

This module checks the half of the "if and only if" that every region carrying a role must meet: a `money_role`
needs `money_cell: true` and an order panel. Left out, and reported for the integrator: the other half, a
`money_cell: true` region without a `money_role`. Every money cell of the example regions predates SD-28 and carries
no role, and some sit on panels outside the closed list (the Position Statement and Active Trader quantity columns),
whose tier-1 surfaces L-XREF-02 still requires to have a money cell; that half lands with the regions migration and a
decision on L-XREF-02 for tier-1 surfaces without an order panel.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ..repo import Repo
from ..rules import MONEY_PANELS

RULE_IDS = ("L-RGN-08",)


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    """Report every region whose `money_role` lacks `money_cell: true` or sits outside the two order panels."""
    for doc in repo.of("regions"):
        regions = doc.data.get("regions") if isinstance(doc.data, dict) else None
        for i, x in enumerate(regions if isinstance(regions, list) else []):
            x = x if isinstance(x, dict) else {}
            role: Any = x.get("money_role")
            if role is None:
                continue
            if x.get("money_cell") is not True:
                add("L-RGN-08", doc.rel, f"region {x.get('id')} has money_role {role} without money_cell: true",
                    f"regions[{i}].money_cell")
            if x.get("panel", "other") not in MONEY_PANELS:
                add("L-RGN-08", doc.rel, f"money_role {role} on region {x.get('id')} of panel {x.get('panel', 'other')}; "
                                         "money roles sit only on order_confirmation and order_ticket", f"regions[{i}].panel")
