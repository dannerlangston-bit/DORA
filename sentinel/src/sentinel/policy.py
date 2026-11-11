"""The operations files of `sentinel-config` the harness reads at run time (4.1, 4.16): the hard caps of
`policy/budgets.yaml` (11.4), the signed authorization of 6.12 (`authorization/<version>.md` and `.yaml`), the soak
budgets of `soak.yaml` (10.11) and the `CODEOWNERS` routing of 11.5 (L-XREF-26).

Loaders validate a file against its 4.16 schema (`sentinel.lint.schemas`) and raise `PolicyError` on a missing or
invalid file, so a run never carries values the lint would refuse:

- `load_budgets(root) -> Budgets`: the reviewer item caps per run type, the agent-rescue limits and the three dollar
  caps. `Budgets.reviewer_call_caps()` feeds `reviewer.config.ReviewerConfig.call_caps`, `usd_per_run` and
  `usd_per_week` the `SpendLedger`, `rescue_doc()` `executor.locator.RescueCap.from_budgets`.
- `load_authorization(root, version=None) -> Authorization`: the version in force (or the one named), its reference
  `authorization/<version>.md` and the sha256 of that document, which every run manifest carries (L-XREF-16;
  `RunPlan.authorization_ref`, `RunPlan.authorization_sha256`). `gateway_rate_caps()` gives the 6.4 caps keyed as
  `orchestrator.budgets.RATE_CAPS`.
- `load_soak(root) -> SoakBudgets`: one budget per `soak.<metric>` assertion of 10.11, defaults filled in.
- `load_release_gate(root) -> ReleaseGate`: N business days per train and the train dates (11.2, 10.9); with no
  file, N = 3 and an RC cut at G-7 are assumed and `header_line()` gives the RC report header line (11.7 row 12).
- `codeowners_check(text, oncall) -> list[CodeownersProblem]`: CODEOWNERS routes `flows/tier-<n>/` to the tier's
  flow owner, `registry/` to the flow owners and every 4.16 file to the owner its 4.1 row names.

Choices where the spec is ambiguous (reported):

1. The authorization version in force (4.15 L-XREF-16, L-XREF-20) is the one record no other record's `supersedes`
   names; no record, or more than one such head, has no version in force (`in_force`).
2. CODEOWNERS follows the GitHub file: `#` comments, `<pattern> <owner>...` lines, the last matching line wins, a
   pattern without a slash (other than a trailing one) matches at any depth, a trailing slash matches everything
   below the directory, `*` stays inside one path segment and `**` crosses segments. An owner token names a person
   of `ops/oncall.yaml` as `@person`, `@org/person` or the bare person. Only the root `CODEOWNERS` of 4.1 is read.
3. `registry/` is not split by tier, so "the tier's flow owner" (11.5, L-XREF-26) is read as: the `registry/` line
   lists the flow owner of every tier `ops/oncall.yaml` names.
4. The owner of each 4.16 file (`OWNERS_4_16`): the owner its 4.1 row names (the system owner for the gateway and
   policy safety files, the registry owner for measured sizes and palettes); else the owner its governing section
   names (the accounts owner for the credential rotation calendar, 7.7 and 11.6; the release manager for the release
   gate, its `owner` field; the accountable owner for the authorization, whose signers are not `ops/oncall.yaml`
   roles, 6.12); else the system owner, who owns change control (11.6). The role's `person` must be among the
   owners; other owners may be listed too.
5. Soak budgets (10.11): a `_max` limit or `within_pct` is looser when higher, a `_min` limit when lower, the
   render-latency `tick_window_s` when longer; the growth-slope `window_h` is a fit parameter, so any change from
   the default counts as looser.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml

from .lint import schemas
from .lint.repo import load_yaml_text

BUDGETS_PATH = "policy/budgets.yaml"
SOAK_PATH = "soak.yaml"
RELEASE_GATE_PATH = "policy/release_gate.yaml"
DEFAULT_BUSINESS_DAYS = 3                   # N, PA-14 of 11.1
ASSUMED_RC_CUT = "G-7"                      # 10.9, 11.7 row 12: assumed when the file is missing
CODEOWNERS_PATH = "CODEOWNERS"
AUTHORIZATION_DIR = "authorization"

# 11.4 hard caps (4.16 budgets schema defaults): reviewer items per run type, the agent rescue of 5.5, the dollars
BUDGET_CAPS_11_4: Mapping[str, Mapping[str, float]] = MappingProxyType({
    "reviewer_items": MappingProxyType({"pr": 60, "merge": 200, "rc": 500}),
    "rescue": MappingProxyType({"max_actions": 25, "max_seconds": 90, "max_rescued_steps_per_run": 25}),
    "usd": MappingProxyType({"per_explorer_session": 25, "per_run": 50, "per_week": 500}),
})
# 6.4 rate caps as the 4.16 authorization record names them, and their orchestrator.budgets.RATE_CAPS keys
RATE_CAPS_6_4: Mapping[str, int] = MappingProxyType({"logins_per_day": 40, "symbol_changes_per_hour": 600,
                                                     "staged_orders_per_hour": 60, "staged_orders_per_day": 300})
GATEWAY_RATE_KEYS: Mapping[str, str] = MappingProxyType({
    "logins_per_day": "logins_per_day", "symbol_changes_per_hour": "symbol_changes_per_hour",
    "staged_orders_per_hour": "orders_staged_per_hour", "staged_orders_per_day": "orders_staged_per_day"})
SIGNATURE_ROLES = ("platform_owner", "security_contact", "compliance_contact")     # 6.12
# 10.11 default soak budgets (4.16 soak schema defaults), one per soak.<metric> assertion
SOAK_DEFAULTS: Mapping[str, Mapping[str, float]] = MappingProxyType({
    "working_set": MappingProxyType({"growth_pct_max": 25, "growth_mb_max": 1536}),
    "growth_slope": MappingProxyType({"window_h": 3, "mb_per_h_max": 60}),
    "gdi_objects": MappingProxyType({"max": 9000}),
    "handles": MappingProxyType({"within_pct": 20}),
    "threads": MappingProxyType({"within_pct": 20}),
    "heap": MappingProxyType({"pct_of_xmx_max": 80}),
    "freezes": MappingProxyType({"max": 0}),
    "relaunches": MappingProxyType({"max": 0}),
    "render_latency": MappingProxyType({"tick_window_s": 2, "windows_pct_min": 99}),
})
SOAK_METRICS = tuple(SOAK_DEFAULTS)
_SOAK_PARAMS = frozenset({("growth_slope", "window_h")})            # choice 5: any change is looser
_SOAK_LONGER = frozenset({("render_latency", "tick_window_s")})     # choice 5: a longer window is looser

# choice 4: (4.16 file, a sample path the CODEOWNERS patterns are matched against, oncall.yaml role of its owner)
OWNERS_4_16: tuple[tuple[str, str, str], ...] = (
    ("gateway/default_order_hotkeys.yaml", "gateway/default_order_hotkeys.yaml", "system_owner"),
    ("gateway/dismiss_allowlist.yaml", "gateway/dismiss_allowlist.yaml", "system_owner"),
    ("policy/explorer_deny_list.yaml", "policy/explorer_deny_list.yaml", "system_owner"),
    ("policy/egress_allowlist.yaml", "policy/egress_allowlist.yaml", "system_owner"),
    ("policy/computer_use_ruling.yaml", "policy/computer_use_ruling.yaml", "system_owner"),
    ("authorization/<version>.md", "authorization/v1.md", "accountable_owner"),
    ("authorization/<version>.yaml", "authorization/v1.yaml", "accountable_owner"),
    ("policy/budgets.yaml", "policy/budgets.yaml", "system_owner"),
    ("policy/release_gate.yaml", "policy/release_gate.yaml", "release_manager"),
    ("policy/reporting.yaml", "policy/reporting.yaml", "system_owner"),
    ("schedule/windows.yaml", "schedule/windows.yaml", "system_owner"),
    ("ops/oncall.yaml", "ops/oncall.yaml", "system_owner"),
    ("ops/credential_rotation.yaml", "ops/credential_rotation.yaml", "accounts_owner"),
    ("rig/process_allowlist.yaml", "rig/process_allowlist.yaml", "system_owner"),
    ("rig/thinkorswim.vmoptions.template", "rig/thinkorswim.vmoptions.template", "system_owner"),
    ("corpus/measured_sizes.yaml", "corpus/measured_sizes.yaml", "registry_owner"),
    ("palettes/<theme>.yaml", "palettes/dark.yaml", "registry_owner"),
    ("soak.yaml", "soak.yaml", "system_owner"),
    ("calibration/<hex>.yaml", "calibration/0000.yaml", "system_owner"),
    ("quarantine.yaml", "quarantine.yaml", "system_owner"),
    ("eval/reports/<hex>.yaml", "eval/reports/0000.yaml", "system_owner"),
    ("phase0/branches.yaml", "phase0/branches.yaml", "system_owner"),
)


class PolicyError(ValueError):
    """An operations file is missing, unreadable or fails its 4.16 schema; the run is `config_invalid` (4.1)."""


def _read(root: str | Path, rel: str, schema: str) -> dict[str, Any]:
    path = Path(root) / rel
    try:
        data = load_yaml_text(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"{rel} is missing") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PolicyError(f"{rel} cannot be read: {exc}") from exc
    problems = schemas.validate(schema, data)
    if problems:
        raise PolicyError(f"{rel} fails its schema: " + "; ".join(f"{loc or '/'}: {msg}" for loc, msg in problems))
    return data


def sha256_of(path: str | Path) -> str:
    """`sha256:` plus the hex digest of a file's bytes (4.1 hash convention)."""
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


# -- policy/budgets.yaml (11.4) ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Budgets:
    """The hard caps of 11.4 as `policy/budgets.yaml` sets them."""
    reviewer_items: Mapping[str, int] = field(default_factory=lambda: dict(BUDGET_CAPS_11_4["reviewer_items"]))
    rescue_max_actions: int = 25
    rescue_max_seconds: float = 90.0
    rescue_max_steps: int = 25
    usd_per_explorer_session: float = 25.0
    usd_per_run: float = 50.0
    usd_per_week: float = 500.0
    approved_by: str | None = None

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "Budgets":
        """From a parsed `policy/budgets.yaml`; a key the file leaves out takes its 4.16 default (the 11.4 value)."""
        caps = {k: dict(v) for k, v in BUDGET_CAPS_11_4.items()}
        for section, values in caps.items():
            given = doc.get(section)
            if isinstance(given, Mapping):
                values.update({k: given[k] for k in values if k in given})
        r, u = caps["rescue"], caps["usd"]
        return cls({k: int(v) for k, v in caps["reviewer_items"].items()}, int(r["max_actions"]),
                   float(r["max_seconds"]), int(r["max_rescued_steps_per_run"]), float(u["per_explorer_session"]),
                   float(u["per_run"]), float(u["per_week"]), doc.get("approved_by") or None)

    def values(self) -> dict[str, dict[str, float]]:
        """The caps in the file's shape (`reviewer_items`, `rescue`, `usd`)."""
        return {"reviewer_items": dict(self.reviewer_items),
                "rescue": {"max_actions": self.rescue_max_actions, "max_seconds": self.rescue_max_seconds,
                           "max_rescued_steps_per_run": self.rescue_max_steps},
                "usd": {"per_explorer_session": self.usd_per_explorer_session, "per_run": self.usd_per_run,
                        "per_week": self.usd_per_week}}

    def above_11_4(self) -> list[tuple[str, float, float]]:
        """Every cap above its 11.4 value as (`section.key`, value, 11.4 value): each needs the system owner's
        approval (`approved_by`, L-XREF-22)."""
        return [(f"{s}.{k}", v, BUDGET_CAPS_11_4[s][k]) for s, vals in self.values().items() for k, v in vals.items()
                if v > BUDGET_CAPS_11_4[s][k]]

    def reviewer_call_caps(self) -> dict[str, int]:
        """The reviewer item caps per run type (`pr`, `merge`, `rc`) for `ReviewerConfig.call_caps` (3.15)."""
        return dict(self.reviewer_items)

    def rescue_doc(self) -> dict[str, Any]:
        """`{"rescue": {...}}`, the shape `executor.locator.RescueCap.from_budgets` reads (5.5)."""
        return {"rescue": self.values()["rescue"]}


def load_budgets(root: str | Path) -> Budgets:
    """Read `policy/budgets.yaml` of the config checkout `root` (11.4, 4.16); `PolicyError` when missing or invalid."""
    return Budgets.from_doc(_read(root, BUDGETS_PATH, "budgets"))


# -- authorization/<version> (6.12) -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Authorization:
    """The signed authorization of 6.12: its version, the document reference and sha256 a run manifest carries
    (L-XREF-16), and the machine-readable record (4.16)."""
    version: str
    ref: str                        # authorization/<version>.md
    sha256: str                     # of the document: RunPlan.authorization_sha256
    record: Mapping[str, Any]

    @property
    def account_ids(self) -> frozenset[str]:
        return frozenset(str(a) for a in self.record.get("account_ids") or ())

    @property
    def rig_hostnames(self) -> tuple[str, ...]:
        return tuple(str(h) for h in self.record.get("rig_hostnames") or ())

    @property
    def send_policy_branch(self) -> str:
        return str(self.record.get("send_policy_branch"))

    @property
    def rate_caps(self) -> dict[str, int]:
        return {k: int(v) for k, v in (self.record.get("rate_caps") or {}).items()}

    def gateway_rate_caps(self) -> dict[str, int]:
        """The 6.4 rate caps keyed as `orchestrator.budgets.RATE_CAPS` (`RateCounters.caps`, `run_budget(caps=)`)."""
        return {GATEWAY_RATE_KEYS[k]: v for k, v in self.rate_caps.items() if k in GATEWAY_RATE_KEYS}


def authorization_records(root: str | Path) -> dict[str, dict[str, Any]]:
    """Every parsed `authorization/<version>.yaml` record by its file version (unreadable files left out)."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((Path(root) / AUTHORIZATION_DIR).glob("*.yaml")):
        try:
            data = load_yaml_text(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            out[path.stem] = data
    return out


def in_force(records: Mapping[str, Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """(version in force, None) or (None, why there is none): the one record no other record supersedes (choice 1)."""
    superseded = {str(r.get("supersedes")) for r in records.values() if r.get("supersedes")}
    heads = sorted(v for v in records if v not in superseded)
    if len(heads) == 1:
        return heads[0], None
    if not records:
        return None, "no authorization record exists; nothing runs against an account until one does (6.12)"
    if not heads:
        return None, "every authorization version is superseded by another (a supersedes cycle)"
    return None, f"versions {', '.join(heads)} are each superseded by none; exactly one version is in force"


def load_authorization(root: str | Path, version: str | None = None) -> Authorization:
    """The authorization in force (or `version`), checked as the run needs it: both files exist, the record passes
    its schema and names its version, and `document_sha256` is the document's hash. `PolicyError` otherwise."""
    if version is None:
        version, why = in_force(authorization_records(root))
        if version is None:
            raise PolicyError(f"no authorization in force: {why}")
    md, rec = f"{AUTHORIZATION_DIR}/{version}.md", f"{AUTHORIZATION_DIR}/{version}.yaml"
    record = _read(root, rec, "authorization")
    doc = Path(root) / md
    if not doc.is_file():
        raise PolicyError(f"{md} is missing; the record {rec} has no signed document")
    sha = sha256_of(doc)
    if record.get("version") != version:
        raise PolicyError(f"{rec} names version {record.get('version')!r}, not {version!r}")
    if record.get("document_sha256") != sha:
        raise PolicyError(f"{rec} document_sha256 is not the sha256 of {md} ({sha})")
    return Authorization(version, md, sha, MappingProxyType(record))


# -- policy/release_gate.yaml (11.2, 10.9) --------------------------------------------------------------------------
@dataclass(frozen=True)
class ReleaseTrain:
    train: str
    ga_date: str
    rc_cut_date: str
    business_days: int


@dataclass(frozen=True)
class ReleaseGate:
    """N and the train dates of `policy/release_gate.yaml`; `assumed` when the file is missing (11.2)."""
    default_business_days: int = DEFAULT_BUSINESS_DAYS
    trains: tuple[ReleaseTrain, ...] = ()
    owner: str | None = None
    assumed: bool = False

    def train(self, name: str) -> ReleaseTrain | None:
        return next((t for t in self.trains if t.train == name), None)

    def business_days(self, train: str | None = None) -> int:
        """N for `train` (its own `business_days`, else the default)."""
        t = self.train(train) if train else None
        return t.business_days if t else self.default_business_days

    def header_line(self) -> str | None:
        """The RC report header line when the values are assumed (11.7 row 12), else None."""
        if not self.assumed:
            return None
        return (f"release gate assumed: N = {self.default_business_days} business days and an RC cut at "
                f"{ASSUMED_RC_CUT} ({RELEASE_GATE_PATH} is missing)")


def load_release_gate(root: str | Path) -> ReleaseGate:
    """Read `policy/release_gate.yaml`; a missing file gives the assumed gate, an invalid one `PolicyError`."""
    if not (Path(root) / RELEASE_GATE_PATH).is_file():
        return ReleaseGate(assumed=True)
    data = _read(root, RELEASE_GATE_PATH, "release-gate")
    n = int(data.get("default_business_days", DEFAULT_BUSINESS_DAYS))
    trains = tuple(ReleaseTrain(str(t["train"]), str(t["ga_date"]), str(t["rc_cut_date"]),
                                int(t.get("business_days") or n)) for t in data.get("trains") or ())
    return ReleaseGate(n, trains, data.get("owner"))


# -- soak.yaml (10.11) ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SoakBudgets:
    """One budget per `soak.<metric>` assertion of 10.11, the file's values over the defaults."""
    budgets: Mapping[str, Mapping[str, float]]
    calibrated_from: str | None = None

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "SoakBudgets":
        given = doc.get("budgets") if isinstance(doc.get("budgets"), Mapping) else {}
        out = {m: {**dict(d), **{k: v for k, v in (given.get(m) or {}).items() if k in d}}
               for m, d in SOAK_DEFAULTS.items()}
        return cls(MappingProxyType(out), doc.get("calibrated_from") or None)

    @classmethod
    def defaults(cls) -> "SoakBudgets":
        return cls.from_doc({})

    def budget(self, metric: str) -> Mapping[str, float]:
        """The budget of `soak.<metric>` (`metric` with or without the `soak.` prefix)."""
        return self.budgets[metric.removeprefix("soak.")]

    def looser_than_default(self) -> list[tuple[str, str, float, float]]:
        """Every value looser than its 10.11 default as (metric, key, value, default) (choice 5)."""
        return [(m, k, v, d) for m, vals in self.budgets.items() for k, v in vals.items()
                if soak_looser(m, k, v, d := SOAK_DEFAULTS[m][k])]


def soak_looser(metric: str, key: str, value: float, default: float) -> bool:
    """True when `value` of `metric.key` is looser than the 10.11 `default` (choice 5)."""
    if (metric, key) in _SOAK_PARAMS:
        return value != default
    if key.endswith("_min"):
        return value < default
    return value > default            # _max limits, within_pct, and the longer tick window


def load_soak(root: str | Path) -> SoakBudgets:
    """Read `soak.yaml` of the config checkout `root` (10.11, 4.16); `PolicyError` when missing or invalid."""
    return SoakBudgets.from_doc(_read(root, SOAK_PATH, "soak"))


# -- CODEOWNERS (11.5, L-XREF-26) ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class CodeownersRule:
    pattern: str
    owners: tuple[str, ...]
    line: int


@dataclass(frozen=True, order=True)
class CodeownersProblem:
    """One routing CODEOWNERS gets wrong: the path (as 4.1 names it) and what is wrong."""
    path: str
    message: str


def parse_codeowners(text: str) -> list[CodeownersRule]:
    """The `<pattern> <owner>...` lines of a CODEOWNERS file, in file order (choice 2)."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        out.append(CodeownersRule(pattern, tuple(owners), n))
    return out


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body, directory = pattern.strip("/"), pattern.endswith("/")
    rx = ""
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif body.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif body[i] == "*":
            rx, i = rx + "[^/]*", i + 1
        elif body[i] == "?":
            rx, i = rx + "[^/]", i + 1
        else:
            rx, i = rx + re.escape(body[i]), i + 1
    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "/.*$" if directory else "(?:/.*)?$"
    return re.compile(prefix + rx + suffix)


def owners_of(rules: Iterable[CodeownersRule], path: str) -> tuple[str, ...] | None:
    """The owners of `path` (the last matching line wins), or None when no line matches."""
    found = None
    for rule in rules:
        if rule.pattern == "*" or _pattern_regex(rule.pattern).match(path):
            found = rule.owners
    return found


def owner_names(owners: Iterable[str]) -> set[str]:
    """The person names owner tokens give (choice 2)."""
    return {o.lstrip("@").rsplit("/", 1)[-1] for o in owners} | {o.lstrip("@") for o in owners}


def _people(oncall: Mapping[str, Any]) -> tuple[dict[str, str], dict[int, str]]:
    roles = {str(r.get("role")): str(r.get("person")) for r in oncall.get("roles") or ()
             if isinstance(r, Mapping) and r.get("person")}
    tiers = {}
    for r in oncall.get("flow_owners") or ():
        if isinstance(r, Mapping) and r.get("person") and isinstance(r.get("tier"), int):
            tiers[int(r["tier"])] = str(r["person"])
    return roles, tiers


def codeowners_check(text: str, oncall: Mapping[str, Any]) -> list[CodeownersProblem]:
    """L-XREF-26: CODEOWNERS routes `flows/tier-<n>/` to that tier's flow owner, `registry/` to the flow owners of
    every tier (choice 3), and every 4.16 file to its owner (`OWNERS_4_16`, choice 4), as `ops/oncall.yaml` names
    them. A role the roster does not name is reported, since its files cannot be routed."""
    rules = parse_codeowners(text)
    roles, tiers = _people(oncall)
    out: list[CodeownersProblem] = []

    def want(path: str, sample: str, person: str, who: str) -> None:
        got = owners_of(rules, sample)
        if got is None:
            out.append(CodeownersProblem(path, f"no CODEOWNERS line routes {path} (it should go to {who} {person})"))
        elif person not in owner_names(got):
            out.append(CodeownersProblem(path, f"{path} is routed to {' '.join(got)}, not to {who} {person}"))

    if not tiers:
        out.append(CodeownersProblem("flows/", "ops/oncall.yaml names no flow owner, so flows/ and registry/ cannot "
                                               "be routed"))
    for tier, person in sorted(tiers.items()):
        want(f"flows/tier-{tier}/", f"flows/tier-{tier}/flow.x.yaml", person, f"the tier-{tier} flow owner")
    for person in sorted(set(tiers.values())):
        want("registry/", "registry/surfaces/x/x.yaml", person, "the flow owner")
    for path, sample, role in OWNERS_4_16:
        if role not in roles:
            out.append(CodeownersProblem(path, f"ops/oncall.yaml names no {role}, the owner of {path}"))
        else:
            want(path, sample, roles[role], f"the {role.replace('_', ' ')}")
    return sorted(set(out))
