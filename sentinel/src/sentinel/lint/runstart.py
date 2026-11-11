"""The Orchestrator's run-start lint (4.1 The lint, 4.3 Missing manifest, 4.5 L-SRF-11, 4.8 L-MSK-02 and L-RGN-05).

At run start the Orchestrator runs the whole lint before gate `provisioned`; any error ends the run
`aborted_harness (reason: config_invalid)` (4.1). Two kinds of failure must not stop a run, and this module keeps
them out of the report that decides that:

- Expiry. L-MSK-02 is "error (CI), ignored at run start": the mask simply stops applying, run health lists
  "mask expired: <id>" and the run is never `config_invalid` over it; L-RGN-05 and L-SRF-11 drop an expired floor
  exception or invariant exception the same way. `lint_at_run_start` returns each as an `ExpiredRecord` (finding
  kind `mask_expired`, `floor_exception_expired` or `invariant_exception_expired`, and `run_health_line()`) instead
  of a violation. Everything else about those records (a missing owner, a malformed date) still fails.
- Manifests. "A manifest that fails the lint is treated as missing for the run and the lint output is printed in the
  PR comment's Intended section" (4.3), and an L-MAN-09 candidate mismatch the same. No violation of a file under
  `manifests/` is in the run-start report; `lint_manifest` lints the run's manifest on its own, and a manifest with
  any error is the missing-manifest case (a) or (b) of 4.3 for the run.

`expiring_soon` lists the masks whose `expires_at` falls within the next seven days, for the Reporter's warning to
the owner (3.8: "the Reporter warns the owner seven days before").

Choice where the spec is ambiguous (reported): `lint_manifest` reports every rule that fires on the manifest file
(the L-MAN rows, its schema failures and the L-XREF-06 stale references), since 4.3 treats a manifest that "fails the
lint" as missing, not only one failing an L-MAN row.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .repo import Repo, _read
from .rules import RULES, ExpiredRecord, LintReport, Violation, lint_repo

WARN_DAYS = 7                                       # 3.8: the owner is warned seven days before a mask expires
MANIFEST_DIR = "manifests"


def _repo(root: str | Path | Repo) -> Repo:
    return root if isinstance(root, Repo) else Repo.load(root)


def _manifest_files(repo: Repo) -> set[str]:
    return {rel for rel, doc in repo.docs.items() if doc.kind == "manifest"}


def lint_at_run_start(root: str | Path | Repo, phase: int = 1,
                      today: date | None = None) -> tuple[LintReport, list[ExpiredRecord]]:
    """The run-start lint of 4.1: (report, expired records). An error left in `report` aborts the run
    `config_invalid`; the expired records are dropped by the run and listed in run health; manifests are left to
    `lint_manifest`."""
    repo = _repo(root)
    full = lint_repo(repo, phase, today, run_start=True)
    manifests = _manifest_files(repo)
    report = replace(full, violations=[v for v in full.violations if v.file not in manifests])
    return report, list(full.expired)


def lint_manifest(repo: str | Path | Repo, manifest_path: str | Path, candidate: str | None = None,
                  phase: int = 1, today: date | None = None) -> list[Violation]:
    """Every violation on one manifest (4.3), linted against the config checkout `repo`: its schema, the L-MAN rows
    and L-XREF-06, plus L-MAN-09 when `candidate` (the pair's candidate) is given and differs from the manifest's.

    `manifest_path` is repository relative (`manifests/<build_id>.yaml`, the copy the Orchestrator made at run
    creation) or a file anywhere (`sentinel lint --manifest .sentinel/manifest.yaml`), which is linted as
    `manifests/<its file name>`. Any `error` in the result means the run treats the manifest as missing."""
    r = _repo(repo)
    p = Path(manifest_path)
    rel = p.as_posix()
    if rel not in r.docs:
        try:
            rel = p.resolve().relative_to(r.root.resolve()).as_posix()
        except ValueError:
            rel = f"{MANIFEST_DIR}/{p.name}"
        if rel not in r.docs:
            r = Repo(r.root, dict(r.docs))
            r.docs[rel] = _read(p, rel, "manifest")
    found = [v for v in lint_repo(r, phase, today, run_start=True).violations if v.file == rel]
    got = r.docs[rel].data.get("candidate") if isinstance(r.docs[rel].data, dict) else None
    if candidate is not None and got != candidate:
        found.append(Violation(rel, "L-MAN-09", RULES["L-MAN-09"].severity, "candidate",
                               f"manifest candidate {got} is not the pair's candidate {candidate}; the manifest is treated "
                               "as missing for the run"))
    return sorted(found)


@dataclass(frozen=True, order=True)
class ExpiryWarning:
    """A mask that expires within the warning window: the Reporter warns its owner (3.8)."""
    expires_at: str
    mask_id: str
    checkpoint_id: str
    owner: str
    file: str
    days_left: int

    def line(self) -> str:
        """`mask msk.x expires in 5 days (2026-11-15T00:00:00Z); owner tos-ui`."""
        return f"mask {self.mask_id} expires in {self.days_left} days ({self.expires_at}); owner {self.owner}"


def expiring_soon(root: str | Path | Repo, today: date, days: int = WARN_DAYS) -> list[ExpiryWarning]:
    """Masks still in force whose `expires_at` is at most `days` days after the start of `today` (3.8, L-MSK-02)."""
    now = datetime.combine(today, time(0), tzinfo=timezone.utc)
    out = []
    for doc in _repo(root).of("mask"):
        m = doc.data if isinstance(doc.data, dict) else {}
        try:
            exp = datetime.fromisoformat(str(m.get("expires_at")))
        except ValueError:
            continue
        exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        if now < exp <= now + timedelta(days=days):
            out.append(ExpiryWarning(str(m.get("expires_at")), str(m.get("mask_id")), str(m.get("checkpoint_id")),
                                     str(m.get("owner") or ""), doc.rel, (exp - now).days))
    return sorted(out)
