"""L-XREF-19 (4.15): `policy/egress_allowlist.yaml` names exact host names only, each with purposes from the
`egress_log` purpose list of 6.10; it carries the security contact's signature; its model host equals the endpoint
of `policy/computer_use_ruling.yaml`; and the Jira, Slack and PR hosts of `policy/reporting.yaml` are on it.

The host, purpose and signature-field checks are `sentinel.egress.allowlist_problems`, the same check the
runtime loader applies. Choices (reported):

1. No allowlist file is not a violation: the harness HTTP client then refuses every host, and preflight N5 fails.
2. "The security contact's signature": `signed_by` is the `security_contact` person or deputy of
   `ops/oncall.yaml` when that file names one; the signature itself is not verified (no scheme in 4.16).
3. The model host is every host carrying a model purpose (`review`, `batch_review`, `agent_step`,
   `explorer_step`, `model_check`); each must be the ruling's `endpoint` (a host, or a URL whose host is taken),
   and the endpoint must be one of them, when the ruling names an endpoint.
4. `policy/reporting.yaml` carries Jira project keys and Slack channels, not host names (4.16), so "its hosts are
   on it" is checked as: some allowlisted host carries `pr_comment` (the PR comment of every PR run, 9.2), `jira`
   when the file sets `jira`, and `slack` when it sets `slack`.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from ...egress import MODEL_PURPOSES, allowlist_problems, normalize_host

RULE_IDS = ("L-XREF-19",)
RULE = "L-XREF-19"


def _endpoint_host(endpoint: str) -> str:
    return normalize_host((urlparse(endpoint).hostname or "") if "://" in endpoint else endpoint)


def _security_contact(oncall: Any) -> set[str]:
    roles = oncall.get("roles") if isinstance(oncall, Mapping) else None
    out: set[str] = set()
    for r in roles if isinstance(roles, list) else []:
        if isinstance(r, Mapping) and r.get("role") == "security_contact":
            out |= {str(r[k]) for k in ("person", "deputy") if r.get(k)}
    return out


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    doc = repo.one("egress_allowlist")
    if doc is None or not isinstance(doc.data, Mapping):
        return                                                  # choice 1; an unreadable file fails its schema
    f, data = doc.rel, doc.data
    for path, message in allowlist_problems(data):
        add(RULE, f, message, path)
    hosts = [h for h in data.get("hosts") or [] if isinstance(h, Mapping) and isinstance(h.get("host"), str)]
    purposes_of = [(normalize_host(h["host"]), set(h.get("purposes") or [])) for h in hosts]

    oncall = repo.one("oncall")
    contact = _security_contact(oncall.data) if oncall is not None else set()
    if contact and data.get("signed_by") not in contact:
        add(RULE, f, f"signed_by {data.get('signed_by')!r} is not the security contact of ops/oncall.yaml "
                     f"({', '.join(sorted(contact))})", "signed_by")

    ruling = repo.one("computer_use_ruling")
    endpoint = ruling.data.get("endpoint") if ruling is not None and isinstance(ruling.data, Mapping) else None
    if isinstance(endpoint, str) and endpoint.strip():
        ep = _endpoint_host(endpoint)
        model_hosts = {h for h, ps in purposes_of if ps & MODEL_PURPOSES}
        if ep not in model_hosts:
            add(RULE, f, f"the model endpoint {ep} of policy/computer_use_ruling.yaml is not on the allowlist with a "
                         "model purpose", "hosts")
        for other in sorted(model_hosts - {ep}):
            add(RULE, f, f"{other} carries a model purpose but is not the ruling's endpoint {ep}", "hosts")

    reporting = repo.one("reporting")
    if reporting is not None and isinstance(reporting.data, Mapping):
        needed = ["pr_comment"] + [p for p in ("jira", "slack") if reporting.data.get(p)]
        for p in needed:
            if not any(p in ps for _, ps in purposes_of):
                add(RULE, f, f"policy/reporting.yaml posts to {p}, but no allowlisted host carries purpose {p}",
                    "hosts")
