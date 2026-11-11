"""L-MUT-03 and L-MUT-04: behavioral coverage of critical checkpoints and the canary flows' order stage (4.11,
10.3 Catalog; SD-42, SD-45).

- L-MUT-03 (SD-42): every critical checkpoint (a checkpoint on a surface with `critical: true`, 4.5) has at least two
  `behavioral` entries bound to it, counting a behavioral entry whose `canary_flow` checkpoint is on the same
  surface. The 20-to-30 count and the six brief classes are counted over generated mutants at Nightly generation,
  not here (`canary.CanaryReport.coverage_gaps`).
- L-MUT-04 (SD-45): `behavioral` requires a `canary_flow` of `class: canary` and `image` forbids one; a canary flow
  may hold one `order_stage` step, which clicks Confirm and Send, and then closes the Order Confirmation dialog with
  Delete through the pinned dismiss allowlist, as attestation item A9 does (6.3); it never targets Send.

Choices where the spec is ambiguous (reported):

1. L-MUT-03 is checked per critical surface: a behavioral entry counts for every checkpoint of the surfaces its
   bound checkpoint and its canary flow's checkpoint steps are on, so every checkpoint of one surface has the same
   count; the violation names the surface and the entries found, in the catalog file (or the surface file when
   there is no catalog).
2. L-MUT-04 targets: a step's `target`, `from`, `to` and `at` resolve to a registry control `<surface>.<control>`.
   Confirm and Send is control id `confirm-and-send` or the label "Confirm and Send" (as L-FLW-07 reads it); Send is
   control id `send` or `send-button` or the exact label "Send"; Delete is control id `delete` or `delete-button` or
   the exact label "Delete". The Delete click must come after the order-stage step, and "Delete" must be a label of
   the dismiss allowlist with its Phase 0 read recorded (gateway_config choice 1), since an unpinned allowlist makes
   the gateway refuse Confirm and Send (6.2).
3. The behavioral and image checks keep the wording rules.py used for them, so the same violation is reported once
   while rules.py still carries its older copy of L-MUT-04.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ...orchestrator.gateway_config import PIN_FIELDS
from ...taxonomy import checkpoint_parts, is_checkpoint_id
from ..repo import Repo

RULE_IDS = ("L-MUT-03", "L-MUT-04")
MIN_BEHAVIORAL = 2                                # L-MUT-03
TARGET_FIELDS = ("target", "from", "to", "at")
SECTIONS = ("normalize", "preconditions", "steps", "teardown")


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _labels(control: dict) -> set[str]:
    return {t.strip().casefold() for t in (str(control.get("ocr_text") or ""), str(_d(control.get("jab")).get("name") or ""))
            if t.strip()}


def _is(control: dict, ids: tuple[str, ...], label: str) -> bool:
    return control.get("id") in ids or label in _labels(control)


def _catalog(repo: Repo) -> tuple[str, list[dict]]:
    doc = repo.one("mutants")
    if doc is None:
        return "", []
    items = doc.data if isinstance(doc.data, list) else _d(doc.data).get("entries")
    return doc.rel, [_d(x) for x in _l(items)]


def _flows(repo: Repo) -> dict[str, tuple[str, dict]]:
    out: dict[str, tuple[str, dict]] = {}
    for doc in repo.of("flow"):
        fid = _d(doc.data).get("flow_id")
        if isinstance(fid, str) and fid not in out:
            out[fid] = (doc.rel, _d(doc.data))
    return out


def _steps(flow: dict) -> list[tuple[str, int, dict]]:
    return [(sec, i, _d(st)) for sec in SECTIONS for i, st in enumerate(_l(flow.get(sec)))]


def _surfaces_of(entry: dict, flows: dict[str, tuple[str, dict]]) -> set[str]:
    cid = entry.get("checkpoint_id")
    out = {checkpoint_parts(cid)["surface"]} if isinstance(cid, str) and is_checkpoint_id(cid) else set()
    _rel, flow = flows.get(str(entry.get("canary_flow")), ("", {}))
    out |= {st["surface_id"] for _s, _i, st in _steps(flow)
            if st.get("type") == "checkpoint" and isinstance(st.get("surface_id"), str)}
    return out


def _mut03(repo: Repo, add: Callable[..., None], catalog: str, items: list[dict],
           flows: dict[str, tuple[str, dict]]) -> None:
    critical = {}
    for doc in repo.of("surface"):
        s = _d(doc.data)
        if s.get("critical") is True and isinstance(s.get("id"), str):
            critical.setdefault(s["id"], doc.rel)
    bound: dict[str, list[str]] = {sid: [] for sid in critical}
    for e in items:
        if e.get("mode") == "behavioral":
            for sid in sorted(_surfaces_of(e, flows) & set(critical)):
                bound[sid].append(str(e.get("mutant_id")))
    for sid in sorted(critical):
        found = bound[sid]
        if len(found) < MIN_BEHAVIORAL:
            add("L-MUT-03", catalog or critical[sid],
                f"critical surface {sid}: its checkpoints have {len(found)} behavioral entr"
                f"{'y' if len(found) == 1 else 'ies'} bound ({', '.join(found) or 'none'}); every critical checkpoint "
                f"needs at least {MIN_BEHAVIORAL} (SD-42)")


def _controls(repo: Repo) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for doc in repo.of("surface"):
        s = _d(doc.data)
        for c in map(_d, _l(s.get("controls"))):
            if isinstance(s.get("id"), str) and isinstance(c.get("id"), str):
                out.setdefault(f"{s['id']}.{c['id']}", c)
    return out


def _pinned_labels(repo: Repo) -> set[str]:
    doc = repo.one("dismiss_allowlist")
    data = _d(doc.data) if doc is not None else {}
    return {str(x) for x in _l(data.get("labels"))} if all(data.get(k) for k in PIN_FIELDS) else set()


def _canary_flow(repo: Repo, add: Callable[..., None], fid: str, rel: str, flow: dict,
                 controls: dict[str, dict]) -> None:
    """SD-45: at most one order-stage step, Confirm and Send, closed by Delete through the dismiss allowlist."""
    steps = _steps(flow)

    def targets(st: dict) -> list[dict]:
        return [controls[t] for t in (st.get(k) for k in TARGET_FIELDS) if isinstance(t, str) and t in controls]

    staged = [(sec, i, st) for sec, i, st in steps if st.get("order_stage") is True]
    if len(staged) > 1:
        add("L-MUT-04", rel, f"canary flow {fid} holds {len(staged)} order_stage steps; L-MUT-04 allows one, the "
                             "Confirm and Send click", f"{staged[1][0]}[{staged[1][1]}]")
    for sec, i, st in staged:
        if not any(_is(c, ("confirm-and-send",), "confirm and send") for c in targets(st)):
            add("L-MUT-04", rel, f"order_stage step {st.get('id')} of canary flow {fid} does not click Confirm and Send",
                f"{sec}[{i}]")
    for sec, i, st in steps:
        if any(_is(c, ("send", "send-button"), "send") for c in targets(st)):
            add("L-MUT-04", rel, f"step {st.get('id')} of canary flow {fid} targets Send; a canary flow never does",
                f"{sec}[{i}]")
    if not staged:
        return
    start = steps.index(staged[0])
    deletes = [(sec, i, st) for sec, i, st in steps[start + 1:]
               if any(_is(c, ("delete", "delete-button"), "delete") for c in targets(st))]
    if not deletes:
        add("L-MUT-04", rel, f"canary flow {fid} clicks Confirm and Send in step {staged[0][2].get('id')} but no later "
                             "step closes the Order Confirmation dialog with Delete", f"{staged[0][0]}[{staged[0][1]}]")
    elif "Delete" not in _pinned_labels(repo):
        sec, i, st = deletes[0]
        add("L-MUT-04", rel, f"canary flow {fid} closes the dialog with Delete in step {st.get('id')}, but the dismiss "
                             "allowlist does not carry a pinned Delete label, so the gateway refuses Confirm and Send "
                             "(6.2)", f"{sec}[{i}]")


def _mut04(repo: Repo, add: Callable[..., None], catalog: str, items: list[dict],
           flows: dict[str, tuple[str, dict]]) -> None:
    checked: set[str] = set()
    controls = _controls(repo)
    for i, m in enumerate(items):
        at, mid, mode, flow = f"[{i}]", m.get("mutant_id"), m.get("mode"), m.get("canary_flow")
        if mode == "behavioral":
            rel, doc = flows.get(str(flow), ("", {}))
            if not doc or doc.get("class") != "canary":
                add("L-MUT-04", catalog, f"behavioral mutant {mid} needs a canary_flow of class canary (got {flow})",
                    f"{at}.canary_flow")
            elif flow not in checked:
                checked.add(str(flow))
                _canary_flow(repo, add, str(flow), rel, doc, controls)
        elif mode == "image" and "canary_flow" in m:
            add("L-MUT-04", catalog, f"image mutant {mid} carries no canary_flow", f"{at}.canary_flow")


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    """Run L-MUT-03 and L-MUT-04 over the repository (module docstring)."""
    catalog, items = _catalog(repo)
    flows = _flows(repo)
    _mut03(repo, add, catalog, items, flows)
    if catalog:
        _mut04(repo, add, catalog, items, flows)
