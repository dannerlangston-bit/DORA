"""The Recorder's save (5.16 Failure outcomes and Key internals, 11.5 Recorder, 6.9, 6.12): every check a recording
passes before its flow, controls and templates reach the sentinel-config checkout.

`check_and_emit(recording, header, config_root=, ocr=, lease=)` runs, in order, and stops at the first that fails:

1. the host: a Recorder on a host matching the rig pattern refuses (`refused`);
2. the pause: a recording whose keystroke log grew while paused is deleted unsaved and is an S1 secret incident
   (`deleted`);
3. the secret scan: each secret the session referenced is fetched under a short-lived lease and searched for, in
   memory, in the keystroke log, in every typed value and in the OCR text of every screenshot; a hit deletes the
   recording unsaved and is an S1 secret incident (credential rotated, security paged; `deleted`); a lease that
   cannot be had refuses the save and keeps the recording (`refused`);
4. the flow (`recorder.core.recorded_events_to_flow`): a step whose template is not unique on its own screenshot
   rejects the save (`rejected`), as does a step the Recorder could not anchor;
5. the `order_stage` prompts: every prompt needs the owner's answer (`needs_answer` until then); a yes sets
   `order_stage: true` on the step;
6. the lint: the flow, the surfaces it adds controls to and the templates are written into a scratch copy of the
   checkout and linted there (4.15, `sentinel lint --file` in repository context); an error is `lint_failed` and
   nothing is written;
7. the write: the same files into the checkout (`saved`), for the owner's pull request.

Choices where the spec is ambiguous or silent (reported):

1. The incident: a `ControlResult` (control `recorder_save`, incident S1, the 9.10 safety routing, action
   `rotate_credential`, one `secret_leaked` finding per secret or one for the pause breach), and, when a store is
   given, a 6.12 incident row (`store.safety.open_incident`, no run, trigger `recorder: <cause>`). A pause breach
   names no secret; the credential to rotate is every secret the session referenced.
2. The lease is the caller's callable from vault references to plaintexts (6.9, the Orchestrator's locked buffer on
   Windows); the scanner's buffers are zeroed when the scan ends, as is a plaintext given as a `bytearray`.
3. OCR text of a screenshot is scanned as its words joined by spaces and joined without them (a value OCR splits).
4. New controls are appended to the end of the surface file's `controls` block as flow-style lines, so the rest of
   the file, comments included, is kept as it was; a surface file without a `controls` key gets one at its end.
5. Screenshots are not written to the checkout: they hold unredacted account data (6.10) and stay on the authoring
   rig for re-anchor mode; only the 40x20 templates are committed.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..capture.redaction import WordOcr
from ..executor.locator import Branches
from ..lint.repo import Repo
from ..lint.rules import lint_repo
from ..orchestrator import alerts
from ..orchestrator.results import ControlResult, FindingSpec
from ..store.secret_scan import SecretHit, SecretScanner
from .core import (DEFAULT_RIG_PATTERN, FlowHeader, RecordedFlow, Recording, RecorderRefused, recorded_events_to_flow,
                   refuse_rig_host)

STATUSES = ("saved", "rejected", "needs_answer", "lint_failed", "deleted", "refused")
FLOW_COMMENT = "# Recorded with the Recorder (5.16); health figures are derived by the Reporter, not written here.\n"


@dataclass(frozen=True)
class SaveResult:
    """What `check_and_emit` decided (module docstring)."""

    status: str
    reasons: tuple[str, ...] = ()
    flow: RecordedFlow | None = None
    written: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    incident: ControlResult | None = None
    incident_id: int | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"save status {self.status!r}; one of {STATUSES}")

    @property
    def saved(self) -> bool:
        return self.status == "saved"


def incident(cause: str, refs: Sequence[str], hits: Sequence[SecretHit] = ()) -> ControlResult:
    """The S1 secret incident of a deleted recording (6.12, choice 1)."""
    if hits:
        by_ref: dict[str, list[str]] = {}
        for h in hits:
            by_ref.setdefault(h.ref, [])
            if h.location not in by_ref[h.ref]:
                by_ref[h.ref].append(h.location)
        findings = tuple(FindingSpec("secret_leaked", f"Recorder: plaintext of secret {ref} found in the recording",
                                     severity="S1", detail="; ".join(locs)) for ref, locs in sorted(by_ref.items()))
    else:
        findings = (FindingSpec("secret_leaked", "Recorder: keystrokes were captured while the Recorder was paused",
                                severity="S1", detail=cause),)
    return ControlResult("recorder_save", False, alert=alerts.safety_abort(), actions=("rotate_credential",),
                         findings=findings, incident="S1", detail=f"recording deleted unsaved: {cause}",
                         evidence={"rotate": sorted(set(refs)),
                                   "hits": [{"ref": h.ref, "location": h.location, "form": h.form} for h in hits]})


def _zero(secrets: Mapping[str, str | bytes | bytearray]) -> None:
    """Overwrite every plaintext handed over as a `bytearray` (choice 2)."""
    for v in secrets.values():
        if isinstance(v, bytearray):
            v[:] = bytes(len(v))


def scan(recording: Recording, secrets: Mapping[str, str | bytes | bytearray], ocr: WordOcr | None) -> list[SecretHit]:
    """Every place a secret's plaintext appears in the recording (5.16 save-time scan; choice 3)."""
    if not secrets:
        return []
    scanner = SecretScanner(secrets)
    try:
        hits = list(scanner.scan_text("".join(recording.keystrokes), "keystroke log"))
        hits += scanner.scan_text("\n".join(recording.keystrokes), "keystroke log")
        for n, ev in enumerate(recording.events, 1):
            if ev.text:
                hits += scanner.scan_text(ev.text, f"event {n} typed value")
        for sid, img in sorted(recording.screenshots.items()):
            words = [w.text for w in ocr.words(img)] if ocr is not None else []
            for text in (" ".join(words), "".join(words)):
                hits += scanner.scan_text(text, f"screenshot {sid} (OCR)")
    finally:
        scanner.zero()
        _zero(secrets)
    seen: dict[tuple[str, str], SecretHit] = {}
    for h in hits:
        seen.setdefault((h.ref, h.location), h)
    return list(seen.values())


def _control_line(control: Mapping[str, Any], indent: str) -> str:
    body = yaml.safe_dump(dict(control), default_flow_style=True, sort_keys=False, width=10_000).strip()
    return f"{indent}- {body}\n"


def add_controls(text: str, controls: Sequence[Mapping[str, Any]]) -> str:
    """`text` (a surface file) with `controls` appended to its `controls` block (choice 4)."""
    if not controls:
        return text
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, ln in enumerate(lines) if re.match(r"^controls:\s*(#.*)?$", ln)), None)
    if start is None:
        return "".join(lines) + "controls:\n" + "".join(_control_line(c, "  ") for c in controls)
    end, indent = start + 1, "  "
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith((" ", "-", "#")):
            break
        m = re.match(r"^(\s*)- ", ln)
        if m:
            indent = m.group(1)
        if ln.strip() and not ln.lstrip().startswith("#"):
            end = i + 1
    return "".join(lines[:end] + [_control_line(c, indent) for c in controls] + lines[end:])


def _flow_text(doc: Mapping[str, Any]) -> str:
    return FLOW_COMMENT + yaml.safe_dump(dict(doc), sort_keys=False, width=120, allow_unicode=True)


def _files(root: Path, rec: RecordedFlow, repo: Repo) -> dict[str, bytes]:
    """The files the save writes, by repository-relative path."""
    out: dict[str, bytes] = {rec.path: _flow_text(rec.doc).encode("utf-8")}
    out.update(rec.templates)
    by_id = {d.data.get("id"): d.rel for d in repo.of("surface") if isinstance(d.data, dict)}
    for sid, controls in rec.controls.items():
        rel = by_id.get(sid)
        if rel is None:
            raise RecorderRefused(f"surface {sid} is not in the registry; register it before recording on it")
        out[rel] = add_controls((root / rel).read_text(encoding="utf-8"), controls).encode("utf-8")
    return out


def _lint(root: Path, files: Mapping[str, bytes], phase: int, today: date | None) -> list[str]:
    """Lint the checkout with `files` written into a scratch copy; the error lines of those files."""
    with tempfile.TemporaryDirectory(prefix="recorder-lint-") as tmp:
        copy = Path(tmp) / "config"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git"))
        for rel, data in files.items():
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            (copy / rel).write_bytes(data)
        report = lint_repo(Repo.load(copy), phase, today)
    return [v.line() for v in report.violations if v.file in files and v.severity == "error"]


def check_and_emit(recording: Recording, header: FlowHeader, *, config_root: str | Path, ocr: WordOcr | None,
                   lease: Callable[[Sequence[str]], Mapping[str, str | bytes | bytearray]],
                   order_dialog_regexes: Iterable[str] = (), order_stage: Mapping[str, bool] | None = None,
                   branches: Branches | None = None, rig_pattern: str = DEFAULT_RIG_PATTERN,
                   rig_hostnames: Iterable[str] = (), phase: int = 0, today: date | None = None, store: Any = None,
                   at: str | None = None) -> SaveResult:
    """Check a recording and write its flow, controls and templates into `config_root` (module docstring)."""
    try:
        refuse_rig_host(recording.host, pattern=rig_pattern, rig_hostnames=rig_hostnames)
    except RecorderRefused as e:
        return SaveResult("refused", (str(e),))
    refs = list(recording.secret_refs)
    if recording.breaches:
        return _delete(recording, "; ".join(recording.breaches), refs, (), store, at)
    if refs:
        try:
            secrets = lease(refs)
        except Exception as e:                       # the vault did not answer: nothing could be checked
            return SaveResult("refused", (f"secret_scan_unavailable: {e}; the recording is kept, save again",))
        missing = sorted(set(refs) - set(secrets))
        if missing:
            _zero(secrets)
            return SaveResult("refused", (f"secret_scan_unavailable: no lease for {', '.join(missing)}",))
        hits = scan(recording, secrets, ocr)
        if hits:
            return _delete(recording, f"plaintext of {', '.join(sorted({h.ref for h in hits}))} in the recording",
                           refs, hits, store, at)
    root = Path(config_root)
    repo = Repo.load(root)
    existing = {d.data["id"]: d.data for d in repo.of("surface") if isinstance(d.data, dict) and d.data.get("id")}
    rec = recorded_events_to_flow(recording.events, recording.screenshots, header=header, ocr=ocr,
                                  branches=branches, existing=existing, order_dialog_regexes=order_dialog_regexes)
    if rec.not_unique or rec.problems:
        reasons = tuple(f"step {s}: its template is not unique on its own screenshot; pick another suggestion or "
                        "re-record" for s in rec.not_unique) + rec.problems
        return SaveResult("rejected", reasons, rec)
    unanswered = rec.answer(order_stage or {})
    if unanswered:
        return SaveResult("needs_answer", tuple(p.text() for p in unanswered), rec)
    try:
        files = _files(root, rec, repo)
    except RecorderRefused as e:
        return SaveResult("rejected", (str(e),), rec)
    errors = _lint(root, files, phase, today)
    if errors:
        return SaveResult("lint_failed", ("the recorded flow does not pass sentinel lint",), rec,
                          violations=tuple(errors))
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return SaveResult("saved", (), rec, written=tuple(sorted(files)))


def _delete(recording: Recording, cause: str, refs: Sequence[str], hits: Sequence[SecretHit], store: Any,
            at: str | None) -> SaveResult:
    """Delete the recording unsaved and raise the S1 incident (5.16, 6.12)."""
    recording.wipe()
    result = incident(cause, refs, hits)
    iid = None
    if store is not None:
        from ..store.safety import open_incident
        iid = open_incident(store, run_id=None, rig_id=None, login_alias=None, severity="S1",
                            trigger=f"recorder: {cause}", at=at)
    return SaveResult("deleted", (cause,), incident=result, incident_id=iid)


__all__ = ["SaveResult", "add_controls", "check_and_emit", "incident", "scan"]
