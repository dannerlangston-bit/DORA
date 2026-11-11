"""The operations-file rules of 4.15 over the files of 4.16 (`sentinel.policy` holds the shared logic):

- L-XREF-20: for the authorization version in force (6.12), `authorization/<version>.md` and `.yaml` both exist;
  `document_sha256` is the document's hash; the platform owner, security contact and compliance contact signed;
  `account_ids` equals the union of `gateway/allowlist.yaml`; `rig_hostnames` covers every rig; `model_endpoint`
  equals the endpoint of `policy/computer_use_ruling.yaml`; `rate_caps` equal 6.4's; `send_policy_branch` is
  `gated` exactly when `policy/send_policy.yaml` is enabled (6.7).
- L-XREF-22: `policy/budgets.yaml` exists and no value exceeds 11.4's unless the system owner approved
  (`approved_by`); its Explorer session and weekly dollar caps equal `policy/explorer_budget.yaml`'s.
- L-XREF-26: `CODEOWNERS` routes `flows/<tier>/`, `registry/` and every 4.16 file to its owner
  (`policy.codeowners_check`).
- L-XREF-27: `soak.yaml` has exactly one budget for each `soak.<metric>` assertion of 10.11 and none looser than the
  10.11 default unless the system owner approved the pull request. A missing `soak.yaml` is an error from Phase 3,
  when the soak ships (11.9); before that nothing reads it (`soak.load_budgets` falls back to the 10.11 defaults).
  `soak.looser_budgets` is the same loosening check for the pull-request lint.

L-XREF-16 (the run manifest carries the reference and sha256, error at run start) is checked against the run context
by `sentinel.lint.diffmode.run_context_rules`; a repository with no authorization in force is reported here under
L-XREF-20, since then every run is refused at preflight.

Choices (reported):

1. No authorization record at all, or no single version in force (`policy.in_force`), is an L-XREF-20 error on
   `authorization/`; every record also names its own file version, and every document has a record.
2. A rig is covered when its id of `gateway/allowlist.yaml` is a hostname of `rig_hostnames` or the first DNS label
   of one (`rig-01` covers `rig-01` and `rig-01.sentinel.internal`).
3. `model_endpoint` is compared as a host (a URL's host is taken), and only when the ruling names an endpoint; under
   an open ruling no model is approved and the document's endpoint is not checked against it.
4. The system owner's approval of a budget above 11.4 is `approved_by` naming the `system_owner` person or deputy
   of `ops/oncall.yaml` (any non-empty name when the roster is missing).
5. `soak.yaml` has no approval field (4.16), and CODEOWNERS routes it to the system owner (L-XREF-26), so a merged
   change already carries that approval: a looser soak budget warns while CODEOWNERS routes `soak.yaml` to the
   system owner, and is an error otherwise. A metric written twice in `budgets` (YAML keeps the last) is an error.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import yaml

from ... import policy as P
from .. import schemas
from ..repo import ConfigLoader
from ..rules import file_sha256

RULE_IDS = ("L-XREF-20", "L-XREF-22", "L-XREF-26", "L-XREF-27")
AUTH_DIR = P.AUTHORIZATION_DIR + "/"


def _map(doc: Any) -> Mapping[str, Any]:
    return doc.data if doc is not None and isinstance(doc.data, Mapping) else {}


def _host(endpoint: str) -> str:
    return ((urlparse(endpoint).hostname or "") if "://" in endpoint else endpoint).strip().lower().rstrip(".")


def _role(oncall: Mapping[str, Any], role: str) -> set[str]:
    return {str(r[k]) for r in oncall.get("roles") or () if isinstance(r, Mapping) and r.get("role") == role
            for k in ("person", "deputy") if r.get(k)}


# -- L-XREF-20 ------------------------------------------------------------------------------------------------------
def _authorization(repo: Any, add: Callable[..., None]) -> None:
    rule = "L-XREF-20"
    records = {Path(d.rel).stem: (d.rel, d.data) for d in repo.of("authorization") if isinstance(d.data, Mapping)}
    for version, (rel, data) in records.items():
        if data.get("version") != version:
            add(rule, rel, f"version {data.get('version')!r} is not {version!r}, the version in the file name", "version")
        if not repo.exists(f"{AUTH_DIR}{version}.md"):
            add(rule, rel, f"{AUTH_DIR}{version}.md is missing: the record has no signed document")
    for md in sorted((repo.root / P.AUTHORIZATION_DIR).glob("*.md")):
        if md.stem not in records:
            add(rule, f"{AUTH_DIR}{md.name}", f"{AUTH_DIR}{md.stem}.yaml is missing: the document has no record")
    version, why = P.in_force({v: d for v, (_, d) in records.items()})
    if version is None:
        add(rule, AUTH_DIR, f"no authorization in force: {why}; preflight refuses every run (L-XREF-16, 11.7)")
        return
    rel, rec = records[version]
    md = f"{AUTH_DIR}{version}.md"
    if repo.exists(md) and rec.get("document_sha256") != file_sha256(repo.path(md)):
        add(rule, rel, f"document_sha256 is not the sha256 of {md}", "document_sha256")
    signed = {s.get("role") for s in rec.get("signatures") or () if isinstance(s, Mapping) and s.get("name")}
    for role in P.SIGNATURE_ROLES:
        if role not in signed:
            add(rule, rel, f"the {role.replace('_', ' ')} has not signed (6.12)", "signatures")
    rigs = [r for r in _map(repo.one("allowlist")).get("rigs") or () if isinstance(r, Mapping)]
    if repo.one("allowlist") is not None:
        accounts = {str(a) for r in rigs for a in r.get("account_ids") or ()}
        got = {str(a) for a in rec.get("account_ids") or ()}
        if got != accounts:
            add(rule, rel, f"account_ids {sorted(got)} are not the accounts of gateway/allowlist.yaml {sorted(accounts)}",
                "account_ids")
        hosts = [str(h).lower() for h in rec.get("rig_hostnames") or ()]
        for rid in sorted({str(r.get("rig")) for r in rigs if r.get("rig")}):
            if not any(h == rid.lower() or h.split(".", 1)[0] == rid.lower() for h in hosts):
                add(rule, rel, f"rig {rid} of gateway/allowlist.yaml is not among rig_hostnames", "rig_hostnames")
    endpoint = _map(repo.one("computer_use_ruling")).get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip() and _host(str(rec.get("model_endpoint") or "")) != _host(endpoint):
        add(rule, rel, f"model_endpoint {rec.get('model_endpoint')!r} is not the endpoint {endpoint!r} of "
                       "policy/computer_use_ruling.yaml", "model_endpoint")
    caps = rec.get("rate_caps") if isinstance(rec.get("rate_caps"), Mapping) else {}
    for k, want in P.RATE_CAPS_6_4.items():
        if caps.get(k) != want:
            add(rule, rel, f"rate_caps.{k} is {caps.get(k)!r}; 6.4 sets {want}", f"rate_caps.{k}")
    send = repo.one("send_policy")
    enabled = _map(send).get("enabled") is True
    branch = rec.get("send_policy_branch")
    if (branch == "gated") != enabled:
        add(rule, rel, f"send_policy_branch is {branch!r} but policy/send_policy.yaml is "
                       f"{'enabled' if enabled else 'not enabled'}; the gated branch needs a new signed version whose "
                       "branch is gated (6.7)", "send_policy_branch")


# -- L-XREF-22 ------------------------------------------------------------------------------------------------------
def _budgets(repo: Any, add: Callable[..., None]) -> None:
    rule = "L-XREF-22"
    doc = repo.one("budgets")
    if doc is None:
        add(rule, P.BUDGETS_PATH, "policy/budgets.yaml is missing; it holds the hard caps of 11.4 (4.16)")
        return
    if not isinstance(doc.data, Mapping) or schemas.validate("budgets", doc.data):
        return                                                  # the schema failure is reported already
    budgets = P.Budgets.from_doc(doc.data)
    oncall = repo.one("oncall")
    owners = _role(_map(oncall), "system_owner") if oncall is not None else set()
    approved = budgets.approved_by is not None and (budgets.approved_by in owners if owners else True)
    for path, value, cap in budgets.above_11_4():
        if not approved:
            add(rule, doc.rel, f"{path} is {value:g}, above 11.4's {cap:g}, without the system owner's approval "
                               "(approved_by)", path)
    eb = repo.one("explorer_budget")
    if eb is not None and isinstance(eb.data, Mapping):
        for mine, theirs in (("per_explorer_session", "max_usd_per_session"), ("per_week", "max_usd_per_week")):
            ours, other = budgets.values()["usd"][mine], eb.data.get(theirs)
            if not isinstance(other, (int, float)) or float(other) != float(ours):
                add(rule, doc.rel, f"usd.{mine} is {ours:g} but policy/explorer_budget.yaml {theirs} is {other!r}; "
                                   "they must be equal", f"usd.{mine}")


# -- L-XREF-26 ------------------------------------------------------------------------------------------------------
def _codeowners(repo: Any, add: Callable[..., None]) -> list[P.CodeownersRule]:
    rule = "L-XREF-26"
    path = repo.path(P.CODEOWNERS_PATH)
    if not path.is_file():
        add(rule, P.CODEOWNERS_PATH, "CODEOWNERS is missing; it routes flows/, registry/ and every 4.16 file to its "
                                     "owner (11.5)")
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    oncall = repo.one("oncall")
    if oncall is None or not isinstance(oncall.data, Mapping):
        add(rule, P.CODEOWNERS_PATH, "ops/oncall.yaml is missing or unreadable, so the CODEOWNERS routing cannot be "
                                     "checked against the owners it names")
        return P.parse_codeowners(text)
    for problem in P.codeowners_check(text, oncall.data):
        add(rule, P.CODEOWNERS_PATH, problem.message, problem.path)
    return P.parse_codeowners(text)


# -- L-XREF-27 ------------------------------------------------------------------------------------------------------
def _duplicate_metrics(text: str) -> list[str]:
    """Metric keys written more than once under `budgets` (YAML keeps only the last)."""
    try:
        root = yaml.compose(text, Loader=ConfigLoader)          # noqa: S506 - ConfigLoader is a SafeLoader
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []
    for key, value in root.value:
        if getattr(key, "value", None) == "budgets" and isinstance(value, yaml.MappingNode):
            names = [getattr(k, "value", None) for k, _ in value.value]
            return sorted({n for n in names if isinstance(n, str) and names.count(n) > 1})
    return []


SOAK_REQUIRED_FROM_PHASE = 3            # the soak ships in Phase 3 (11.9); before that nothing reads soak.yaml


def _soak(repo: Any, add: Callable[..., None], rules: list[P.CodeownersRule], phase: int) -> None:
    rule = "L-XREF-27"
    doc = repo.one("soak")
    if doc is None:
        if phase >= SOAK_REQUIRED_FROM_PHASE:
            add(rule, P.SOAK_PATH, "soak.yaml is missing; it holds one budget per soak.<metric> assertion of 10.11")
        return
    if not isinstance(doc.data, Mapping):
        return
    given = doc.data.get("budgets") if isinstance(doc.data.get("budgets"), Mapping) else {}
    for metric in P.SOAK_METRICS:
        if metric not in given:
            add(rule, doc.rel, f"no budget for soak.{metric} (10.11)", f"budgets.{metric}")
    try:
        text = repo.path(doc.rel).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for metric in _duplicate_metrics(text):
        add(rule, doc.rel, f"soak.{metric} has more than one budget", f"budgets.{metric}")
    if schemas.validate("soak", doc.data):
        return                                                  # the schema failure is reported already
    oncall = repo.one("oncall")
    owners = _role(_map(oncall), "system_owner") if oncall is not None else set()
    routed = P.owners_of(rules, P.SOAK_PATH)
    approved = bool(owners and routed and owners & P.owner_names(routed))
    for metric, key, value, default in P.SoakBudgets.from_doc(doc.data).looser_than_default():
        if metric in given:
            add(rule, doc.rel, f"soak.{metric} {key} {value:g} is looser than the 10.11 default {default:g}; it needs "
                               "the system owner's approval in the pull request" +
                               ("" if approved else ", and CODEOWNERS does not route soak.yaml to the system owner"),
                f"budgets.{metric}.{key}", "warn" if approved else None)


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    _authorization(repo, add)
    _budgets(repo, add)
    rules = _codeowners(repo, add)
    _soak(repo, add, rules, phase)
