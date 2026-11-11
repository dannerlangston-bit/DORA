"""The calibration files `calibration/<hex>.yaml` against the rest of the repository (4.1, 4.8 Threshold source, 4.15).

The Calibration Service exports one file per calibration key (4.12; `<hex>` is the key without `sha256:`, SD-36, one
helper: `canonical.calibration_filename`). Its shape is validated as L-RGN-00 by rules.py; this module checks:

- L-RGN-00: the file name is the name of its own `calibration_key`;
- L-XREF-11: every checkpoint id the file names matches the 4.1 grammar and its surface segment is registered;
- L-XREF-04: every such id is defined by a flow, census, sweep or splitter, or retired in flows/RENAMES.yaml;
- L-RGN-06 (warn): a region of a `frozen` checkpoint with a non-zero floor, which is a bug to fix (brief 1.4);
- L-XREF-05: every tier-1 checkpoint has an entry in the canonical calibration. Its other half, the behavioral
  mutants of every critical checkpoint, is L-MUT-03 itself (sentinel.lint.checks.mutants), not repeated here.

Choices where the spec is ambiguous (reported):

1. A non-zero floor is a changed-pixel or translation floor above 0: the fix-list rule of diff.calibration (3.9);
   a CIEDE2000 floor alone only raises the color threshold.
2. The canonical calibration key (L-XREF-05) is not written in any file. `calibration_coverage` takes it from the
   caller (the run's calibration key at run start); without one it is the key of the most recently exported file
   (`exported_at`). "The first Calibration run" has happened when any calibration file exists: from then on a
   tier-1 checkpoint missing from the canonical file is an error (a new tier-1 checkpoint brings its calibration in
   the same pull request); with a key whose file does not exist yet, before the first Calibration run, the whole
   key is one warning, never one line per checkpoint. A checkout with no calibration file and no key given has no
   canonical calibration to measure against, and the run marks every region `uncalibrated` (4.8), so nothing is
   reported.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from ...canonical import calibration_filename
from ...taxonomy import is_checkpoint_id
from ..repo import Doc, Repo
from ..rules import checkpoint_definitions

RULE_IDS = ("L-RGN-00", "L-XREF-04", "L-XREF-05", "L-XREF-11", "L-RGN-06")
CALIBRATION_DIR = "calibration"


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _exported(doc: Doc) -> datetime:
    try:
        return datetime.fromisoformat(str(_d(doc.data).get("exported_at")))
    except ValueError:
        return datetime.min


def nonzero_floor(floors: Any) -> bool:
    """A region's floors record a changed-pixel or translation floor above zero (choice 1)."""
    f = _d(floors)
    return any(isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool) and f[k] > 0
               for k in ("changed_px", "shift_px"))


def canonical_calibration(repo: Repo, calibration_key: str | None = None) -> Doc | None:
    """The calibration file of `calibration_key`, or without a key the most recently exported one (choice 2)."""
    docs = [d for d in repo.of("calibration") if isinstance(d.data, dict)]
    if calibration_key is not None:
        return next((d for d in docs if d.data.get("calibration_key") == calibration_key), None)
    return max(docs, key=_exported) if docs else None


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    if not repo.of("calibration"):
        return                          # no calibration file: nothing to check and no canonical calibration (choice 2)
    defined, retired = checkpoint_definitions(repo)
    surfaces = {_d(d.data).get("id"): _d(d.data) for d in repo.of("surface")}
    for doc in repo.of("calibration"):
        _file(doc, add, defined, retired, surfaces)
    for rule, file, message, path, severity in calibration_coverage(repo, defined=defined):
        add(rule, file, message, path, severity)


def _file(doc: Doc, add: Callable[..., None], defined: dict, retired: frozenset[str], surfaces: dict) -> None:
    data = _d(doc.data)
    key = data.get("calibration_key")
    try:
        want = f"{CALIBRATION_DIR}/{calibration_filename(str(key))}"
    except ValueError:
        want = None                      # a malformed key is the schema's L-RGN-00 already
    if want is not None and doc.rel != want:
        add("L-RGN-00", doc.rel, f"a calibration file is {want}, the name of its calibration_key (4.1, SD-36)",
            "calibration_key")
    for i, cp in enumerate(map(_d, _l(data.get("checkpoints")))):
        cid, at = cp.get("checkpoint_id"), f"checkpoints[{i}].checkpoint_id"
        if not (isinstance(cid, str) and is_checkpoint_id(cid)):
            add("L-XREF-11", doc.rel, f"{cid!r} does not match the checkpoint id grammar of 4.1", at)
            continue
        if cid.split("/")[1] not in surfaces:
            add("L-XREF-11", doc.rel, f"surface segment {cid.split('/')[1]} of {cid} is not in the registry", at)
        c = defined.get(cid)
        if c is None and cid not in retired:
            add("L-XREF-04", doc.rel, f"checkpoint {cid} is defined by no flow or census and is not retired", at)
        if c is None or c.data_mode != "frozen":
            continue
        for j, rg in enumerate(map(_d, _l(cp.get("regions")))):
            if nonzero_floor(rg.get("floors")):
                add("L-RGN-06", doc.rel, f"region {rg.get('region_id')} of frozen checkpoint {cid} has a non-zero "
                                         f"calibration floor {rg.get('floors')}; a frozen checkpoint keeps a zero "
                                         "floor (brief 1.4)", f"checkpoints[{i}].regions[{j}].floors")


def calibration_coverage(repo: Repo, calibration_key: str | None = None, first_run_done: bool | None = None,
                         defined: dict | None = None) -> list[tuple[str, str, str, str, str]]:
    """L-XREF-05 as (rule, file, message, path, severity): tier-1 checkpoints missing from the canonical calibration
    (choice 2).

    `calibration_key` is the canonical key (the run's at run start); `first_run_done` says a Calibration run has
    happened (default: any calibration file exists). The run-start wiring passes both."""
    if defined is None:
        defined, _retired = checkpoint_definitions(repo)
    out: list[tuple[str, str, str, str, str]] = []
    done = bool(repo.of("calibration")) if first_run_done is None else first_run_done
    sev = "error" if done else "warn"
    tier1 = sorted(cid for cid, c in defined.items() if c.tier == 1)
    canon = canonical_calibration(repo, calibration_key)
    if canon is not None:
        have = {_d(cp).get("checkpoint_id") for cp in _l(_d(canon.data).get("checkpoints"))}
        for cid in tier1:
            if cid not in have:
                out.append(("L-XREF-05", canon.rel, f"tier-1 checkpoint {cid} has no calibration entry for the canonical "
                                                    "calibration key", "checkpoints", sev))
    elif calibration_key is not None and tier1:
        rel = f"{CALIBRATION_DIR}/{_name(calibration_key)}"
        out.append(("L-XREF-05", rel, f"no calibration for the canonical calibration key {calibration_key}: "
                                      f"{len(tier1)} tier-1 checkpoints are uncalibrated", "", sev))
    return out


def _name(calibration_key: str) -> str:
    try:
        return calibration_filename(calibration_key)
    except ValueError:
        return f"{Path(str(calibration_key)).name}.yaml"
