"""The flow classes of 2.6.3: the paper_send entitlement of surfaces only a sent order reaches (L-SRF-12) and the
minimum-set coverage report.

L-SRF-12 (4.5, error): a surface with `reached_after_send: true` (fills, Working to Filled, cancel and replace)
carries `paper_send` in `entitlements`; and, the flow side of the same sentence of 2.6.3, a flow whose `surfaces`
include such a surface carries `requires: [paper_send]`, so its checkpoints are `not_run (reason: entitlement)` until
the `paper_send_ok` decision of 6.7 (choice 1).

`minimum_set(repo)` is the 2.6.3 table as a coverage report, not a lint rule (2.6.3 and 4.7 give it no rule id, and
the 4.7 header has no tag for the 2.6.3 classes): one `ClassGap` per missing required assertion or corpus part, for
the classes a flow file shows (choice 2):

- Ticket: a `standard` or `paper_send_ok` flow on a `ticket_variant` surface asserts, after the first ticket input,
  symbol, side, quantity, type, price and TIF; after the order-stage step, the Order Confirmation's quantity, price,
  cost, buying power effect and max loss; and an `absent` assertion after its close_dialog or clear_ticket teardown
  (the empty ticket after Cancel). The pre-input checkpoint is L-FLW-15.
- Chain: a flow on a `chain_layout` surface asserts R04, R03 and R05.
- Export: a flow with an `export` step asserts R09 or `export_equals`.
- Odd input: corpus/odd-inputs.yaml covers the four field classes and has a halt-list entry.
- Migration: every tier B workspace saved under the previous release is the `workspace` of a flow.

Choices (reported): 1. above. 2. A region or rule input names a field when its id's last segment contains the field's
key (`qty`, `order-type`, `bp-effect`, ...), as rules.py choice 17 reads order fields. The entry points, asset
classes and Order Defaults profiles of the Ticket row, the keyboard-only variant, and the Chart and study, Scan, Alert,
Workspace, Hotkey and Re-login rows need a class tag the 4.7 header does not have: `UNCHECKED_CLASSES`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable

from ... import oddinput
from ..repo import Repo
from .corpus import odd_input_doc, previous_release

RULE_IDS = ("L-SRF-12",)
PAPER_SEND = "paper_send"
TICKET_AFTER_INPUT = (("symbol", ("symbol",)), ("side", ("side",)), ("quantity", ("qty", "quantity")),
                      ("type", ("order-type",)), ("price", ("price",)), ("TIF", ("tif", "time-in-force")))
CONFIRMATION = (("quantity", ("qty", "quantity")), ("price", ("price",)), ("cost", ("cost",)),
                ("buying power effect", ("bp-effect", "bp_effect")), ("max loss", ("max-loss", "max_loss")))
CHAIN_RULES = ("R04", "R03", "R05")
EXPORT_RULE = "R09"
TICKET_FLOW_CLASSES = ("standard", "paper_send_ok")
UNCHECKED_CLASSES = ("Chart and study", "Scan", "Alert", "Workspace", "Hotkey", "Re-login")


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    after_send = set()
    for doc in repo.of("surface"):
        s = _d(doc.data)
        if s.get("reached_after_send") is True:
            after_send.add(s.get("id"))
            if PAPER_SEND not in _l(s.get("entitlements")):
                add("L-SRF-12", doc.rel, f"{s.get('id')} is reached only after a sent order and lists no {PAPER_SEND} "
                                         "entitlement (4.5)", "entitlements")
    for doc in repo.of("flow"):
        f = _d(doc.data)
        reached = sorted(str(s) for s in _l(f.get("surfaces")) if s in after_send)
        if reached and PAPER_SEND not in _l(f.get("requires")):
            add("L-SRF-12", doc.rel, f"the flow reaches {', '.join(reached)}, reached only after a sent order, and does "
                                     f"not require {PAPER_SEND} (2.6.3)", "requires")


@dataclass(frozen=True, order=True)
class ClassGap:
    """One missing part of the 2.6.3 minimum set: the class, the flow, workspace or corpus it concerns, what is
    missing, and the file to fix."""
    flow_class: str
    subject: str
    missing: str
    file: str

    def line(self) -> str:
        return f"{self.flow_class} {self.subject}: missing {self.missing} ({self.file})"


def _steps(flow: dict) -> list[dict]:
    return [_d(x) for x in _l(flow.get("steps")) + _l(flow.get("teardown"))]


def _assertions(st: dict) -> list[dict]:
    items = _l(st.get("expect")) + _l(st.get("assertions"))
    if isinstance(st.get("verify"), dict):
        items.append(st["verify"])
    return [_d(a) for a in items]


def _named(assertions: Iterable[dict]) -> set[str]:
    """The last id segments of every region an assertion names, as its region or a rule input."""
    out = set()
    for a in assertions:
        regions = [a.get("region"), *_d(a.get("inputs")).values()]
        out |= {r.rsplit(".", 1)[-1].casefold() for r in regions if isinstance(r, str)}
    return out


def _missing(named: set[str], fields: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    return [name for name, keys in fields if not any(k in seg for seg in named for k in keys)]


def _ticket(fid: str, rel: str, flow: dict, ticket_surfaces: set[str]) -> list[ClassGap]:
    seq = _steps(flow)
    first = next((k for k, st in enumerate(seq) if st.get("type") in ("type_fixture", "click_named")
                  and any(str(st.get("target", "")).startswith(t + ".") for t in ticket_surfaces)
                  and not str(st.get("target", "")).endswith(".symbol-box")), None)
    if first is None:
        return []
    stage = next((k for k, st in enumerate(seq) if st.get("order_stage") is True), len(seq))
    out = [ClassGap("Ticket", fid, f"an after-input assertion of the ticket's {name}", rel)
           for name in _missing(_named(a for st in seq[first:stage] for a in _assertions(st)), TICKET_AFTER_INPUT)]
    if stage < len(seq):
        closed = next((k for k in range(stage, len(seq)) if seq[k].get("type") == "teardown"
                       and seq[k].get("action") in ("close_dialog", "clear_ticket")), len(seq))
        out += [ClassGap("Ticket", fid, f"an Order Confirmation assertion of the {name}", rel)
                for name in _missing(_named(a for st in seq[stage:closed] for a in _assertions(st)), CONFIRMATION)]
        if not any(a.get("kind") == "absent" for st in seq[closed:] for a in _assertions(st)):
            out.append(ClassGap("Ticket", fid, "an absent assertion proving the ticket empty after Cancel", rel))
    return out


def minimum_set(repo: Repo) -> list[ClassGap]:
    """The 2.6.3 minimum-set gaps the files show (module docstring); empty when every checkable part is present."""
    kinds = {_d(d.data).get("id"): _d(d.data).get("kind") for d in repo.of("surface")}
    tickets = {s for s, k in kinds.items() if k == "ticket_variant"}
    chains = {s for s, k in kinds.items() if k == "chain_layout"}
    out: list[ClassGap] = []
    used_workspaces = set()
    for doc in repo.of("flow"):
        f = _d(doc.data)
        fid, surfaces = str(f.get("flow_id")), set(map(str, _l(f.get("surfaces"))))
        used_workspaces.add(f.get("workspace"))
        rules = {a.get("rule_id") for st in _steps(f) for a in _assertions(st) if a.get("kind") == "rule"}
        if surfaces & tickets and f.get("class") in TICKET_FLOW_CLASSES:
            out += _ticket(fid, doc.rel, f, surfaces & tickets)
        if surfaces & chains:
            out += [ClassGap("Chain", fid, f"rule {r}", doc.rel) for r in CHAIN_RULES if r not in rules]
        if any(st.get("type") == "export" for st in _steps(f)):
            kinds_used = {a.get("kind") for st in _steps(f) for a in _assertions(st)}
            if EXPORT_RULE not in rules and "export_equals" not in kinds_used:
                out.append(ClassGap("Export", fid, f"rule {EXPORT_RULE} or an export_equals assertion", doc.rel))
    out += _odd_input_gaps(repo)
    prev = previous_release(repo)
    for doc in repo.of("workspace"):
        w = _d(doc.data)
        if w.get("tier") == "B" and prev is not None and w.get("saved_under_build") == prev and w.get("id") not in used_workspaces:
            out.append(ClassGap("Migration", str(w.get("id")), "a migration flow loading it on the candidate", doc.rel))
    return sorted(out)


def _odd_input_gaps(repo: Repo) -> list[ClassGap]:
    doc = odd_input_doc(repo)
    if doc is None or doc.error:
        return [ClassGap("Odd input", oddinput.PATH, "the odd-input corpus", oddinput.PATH)]
    entries = [_d(e) for e in _l(_d(doc.data).get("entries"))]
    have = {e.get("field_class") for e in entries}
    out = [ClassGap("Odd input", oddinput.PATH, f"{c} entries", doc.rel) for c in oddinput.FIELD_CLASSES if c not in have]
    if not any(e.get("source") == "halt_list" for e in entries):
        out.append(ClassGap("Odd input", oddinput.PATH, "a halt-list entry (the halted or delisted symbol)", doc.rel))
    return out
