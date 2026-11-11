"""The 4.12 environment fingerprint: assembled from the live reads, validated, compared (3.3, 4.12, 7.1, 7.4, 7.13,
12.7 G5).

The rig reads the fields through `orchestrator.ports.FingerprintReader`: the OS fields at gate `provisioned`
(`OsReads`), the app fields at gate `launched` (`AppReads`; `render_probe_hash` after `authenticated`, the app theme
and the app time zones from the attestation OCR). This module turns those reads into the 4.12 record and decides,
in the closed taxonomy, what each check over it gives:

- `os_fields(os_reads, harness)`: the OS-class `match` fields at gate `provisioned`, which G5 compares when both
  halves are provisioned, or `env_error (gate: provisioned, reason: fingerprint_unreadable:<field>)`.
- `build_fingerprint(...)`: the whole `records.Fingerprint` (L-FPR-00 and L-FPR-01 hold), or the
  `fingerprint_unreadable:<field>` env_error at the read's gate; a field is never null and never copied from the
  other side (7.4). `harness_block` builds its `harness` field. Stored with `store.Store.write_fingerprint` (8.3
  env_fingerprints, one row per shard and side); `document` is the record in the schema's shape.
- `rig_spec_check(fp, ...)`: L-FPR-02 (`lint.rules.lint_fingerprint_rig_spec`) with SD-71's outcome per value.
- `g5(candidate, base, calibrated_keys)`: the 12.7 G5 detail, `pass`, `fingerprint_mismatch:<fields>` or
  `no_calibration`, the convention `orchestrator.gates.check_provisioned` reads.
- `pair_check(candidate, base)`: L-FPR-03 and L-FPR-05 as a control result, before the first capture of the second
  half and after every relaunch (7.4); `mismatch_note` is the L-FPR-05 morning-report sentence.
- `cheap_reread(os_reads, title_build_id)` and `midrun_fingerprint_change(start, reread)`: the re-reads at every
  flow boundary and after every relaunch (3.3, 7.4, SD-73).

Every match-field comparison but the mid-run build id is `lint.rules.fingerprint_differences`, the one the lint and
the Diff Engine use.

Choices where the spec is ambiguous or silent (reported):

1. Read classes (7.4): the OS fields are windows_build, display, cleartype, gpu, os_theme, keyboard, input_desktop,
   the OS half of time_zone (tzutil) and the record field rig; the app fields are build_id (title, else Help >
   About, 7.3), jre_release, vmoptions_hash, java2d_pipeline, render_probe_hash, app_theme, workspace_hash,
   settings_profile_hash, the app half of time_zone and the record field jar_listing_hash. data_epoch (the fixture's
   epoch and data mode, 4.9), account, harness, trading_24h_list_sha256 and run_date come from the run, not the rig.
2. An unreadable read names the first unreadable OS field (7.4 table order) at gate `provisioned`, since the OS
   reads come first, else the first unreadable app field at gate `launched`; every unreadable field is in `detail`.
   A read that is present but not of the 4.12 shape (a subfield missing, a wrong type) is unreadable the same way.
   A malformed run-supplied field (harness, data_epoch, account, the ids, read_at) is a harness fault: ValueError.
3. `os_fields` leaves time_zone out: its app half is read at `launched`, so the provisioned G5 compares the OS-class
   fields and harness, and time_zone is compared whole by the later checks (R4 pins the OS half before that).
4. G5 compares the fields read on both sides so far. The calibration test needs the rendering-key fields, so it
   runs only when `calibrated_keys` is given, once both fingerprints are whole; a mismatch is reported before a
   missing calibration. `calibrated_keys` holds every key with floors, including a key this fingerprint may take
   over after a passing Nightly A/A (L-FPR-04).
5. Mid-run (7.4): "a changed title or other app field" is read as the fields that name the installed product and
   its runtime, VERSION_FIELDS (build_id, jre_release, vmoptions_hash, java2d_pipeline, jar_listing_hash; 7.4's
   examples are a new build that landed and a rewritten vmoptions): `env_error (gate: launched, reason:
   version_changed_mid_run)` for the rest of the shard. Any other changed match field, render_probe_hash among them
   (L-FPR-05 calls that a rasterization change), makes the shard's remaining checkpoints `not_comparable
   (fingerprint_mismatch)`. With several causes a version change wins, then an unreadable re-read
   (`fingerprint_unreadable:<field>` at the field's gate), then a mismatch. Record fields other than
   jar_listing_hash (account, rig, run_date) are not compared.
6. SD-71 (7.1) in `rig_spec_check`: a value a 12.7 item reads takes that item's outcome: windows_build R1, display
   R2, cleartype R3, keyboard and the OS time zone R4, input_desktop R6, java2d_pipeline, vmoptions_hash and
   jre_release B3, each `env_error (gate: preflight.<item>, reason: gate)`; the harness DPI awareness is H3's
   `aborted_harness (reason: dpi_awareness)` (7.1, 12.7 H3); the app display and chart time zones take
   `env_error (gate: settings_attested, reason: fixture_drift)` (7.1 Time zone row). gpu, os_theme, app_theme and
   the harness allowlist hash, which no item reads, are `env_error (gate: provisioned, reason: rig_spec_mismatch)`.
   With several, the H3 abort wins, then the items in 12.7 order, then rig_spec_mismatch, then the settings gate;
   every violation is in `detail`.
7. Details: `not_comparable (fingerprint_mismatch)` carries `field: <a>, <b>` (the Diff Engine's form; SD-78 makes
   it non-empty) and so does `version_changed_mid_run`; the G5 detail is `fingerprint_mismatch:<a>,<b>`. Field
   names are the top-level 7.4 names, sorted.
8. `run_date` is the America/New_York date of `read_at` (7.4), which must carry its UTC offset.
"""
from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from .canonical import calibration_key
from .diff import load_params, params_sha256
from .lint import schemas
from .lint.rules import fingerprint_differences, lint_fingerprint, lint_fingerprint_rig_spec
from .orchestrator import alerts
from .orchestrator.gates import PREFLIGHT_ITEMS
from .orchestrator.ports import AppReads, OsReads
from .orchestrator.results import ControlResult, Outcome, RunVerdict, harness_abort_outcome
from .records import Fingerprint

NEW_YORK = ZoneInfo("America/New_York")
# choice 1: the fields the rig reads at each gate (time_zone is read in two halves)
OS_FIELDS = ("windows_build", "display", "cleartype", "gpu", "os_theme", "keyboard", "input_desktop", "rig")
APP_FIELDS = ("build_id", "jre_release", "vmoptions_hash", "jar_listing_hash", "java2d_pipeline", "render_probe_hash",
              "app_theme", "workspace_hash", "settings_profile_hash")
# 3.3, 7.4: re-read at every flow boundary and after every relaunch, with the main-window title (build_id)
CHEAP_FIELDS = ("cleartype", "display", "input_desktop")
# choice 5: a change of one of these mid-run is version_changed_mid_run
VERSION_FIELDS = ("build_id", "jre_release", "vmoptions_hash", "java2d_pipeline", "jar_listing_hash")
# 12.7 G5 detail convention
G5_PASS, G5_NO_CALIBRATION, G5_MISMATCH = "pass", "no_calibration", "fingerprint_mismatch:"
# 4.12 L-FPR-05: what the morning report says about a render_probe_hash mismatch
RASTERIZATION_NOTE = "a rasterization change happened inside the same Windows build"
# SD-71 (choice 6): the 12.7 item that reads a pinned value, by match path; no entry is rig_spec_mismatch
_SETTINGS = "settings_attested"
_ITEM_OF = {"display.awareness": "H3", "windows_build": "R1", "display": "R2", "cleartype": "R3", "keyboard": "R4",
            "time_zone.os": "R4", "input_desktop": "R6", "java2d_pipeline": "B3", "vmoptions_hash": "B3",
            "jre_release": "B3", "time_zone.app_display": _SETTINGS, "time_zone.chart": _SETTINGS}


def _plain(value: Any) -> Any:
    """The JSON shape of a read: mappings as dicts, sequences as lists."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@lru_cache(maxsize=1)
def _validators() -> dict[str, Draft202012Validator]:
    props = schemas.schema("fingerprint")["properties"]
    return {k: Draft202012Validator(v)
            for k, v in {**props["match"]["properties"], **props["record"]["properties"]}.items()}


def _unreadable_read(field: str, value: Any) -> bool:
    """Choice 2: a read is unreadable when it is null or not of its 4.12 shape."""
    return value is None or not _validators()[field].is_valid(_plain(value))


def _gate_of(field: str) -> str:
    """The gate a rig-read field is read at (choice 1); ValueError for a field the run supplies."""
    if field in APP_FIELDS:
        return "launched"
    if field in OS_FIELDS or field == "time_zone":
        return "provisioned"
    raise ValueError(f"{field} is supplied by the run, not read from the rig (7.4)")


def _os_values(r: OsReads) -> dict[str, Any]:
    return {"windows_build": r.windows_build, "display": r.display, "cleartype": r.cleartype, "gpu": r.gpu,
            "os_theme": r.os_theme, "keyboard": r.keyboard, "input_desktop": r.input_desktop, "rig": r.rig}


def _app_values(r: AppReads) -> dict[str, Any]:
    build = r.build_id_title if r.build_id_title is not None else r.build_id_about
    return {"build_id": build, "jre_release": r.jre_release, "vmoptions_hash": r.vmoptions_hash,
            "jar_listing_hash": r.jar_listing_hash, "java2d_pipeline": r.java2d_pipeline,
            "render_probe_hash": r.render_probe_hash, "app_theme": r.app_theme, "workspace_hash": r.workspace_hash,
            "settings_profile_hash": r.settings_profile_hash}


def _unreadable_fields(reads: Mapping[str, Any]) -> dict[str, str]:
    return {f: _gate_of(f) for f, v in reads.items() if _unreadable_read(f, v)}


def _check_supplied(values: Mapping[str, Any]) -> None:
    wrong = sorted(f for f, v in values.items() if _unreadable_read(f, v))
    if wrong:
        raise ValueError(f"run-supplied fingerprint fields {wrong} are not of the 4.12 shape")


def _unreadable(control: str, fields: Mapping[str, str]) -> ControlResult:
    """Choice 2: `env_error (fingerprint_unreadable:<field>)` on every scoped checkpoint, run
    `complete_with_env_errors`, on-call channel (7.4, 4.12 L-FPR-01)."""
    ordered = [f for f in schemas.FINGERPRINT_FIELDS_7_4 if f in fields]
    first = ([f for f in ordered if fields[f] == "provisioned"] or ordered)[0]
    gate, detail = fields[first], "unreadable: " + ", ".join(ordered)
    return ControlResult(control, False, gate, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", f"fingerprint_unreadable:{first}", gate, detail),
                         alert=alerts.env_error(), detail=detail, evidence={"unreadable": ordered})


def _mismatch(control: str, fields: list[str]) -> ControlResult:
    """3.3, 7.4, L-FPR-03/05: the shard's checkpoints without an outcome are `not_comparable (fingerprint_mismatch)`
    with the field names, nothing further is captured, run `complete_with_env_errors`, on-call channel."""
    note = mismatch_note(fields)
    detail = "fingerprint_mismatch: " + ", ".join(fields) + (f"; {note}" if note else "")
    return ControlResult(control, False, None, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("not_comparable", "fingerprint_mismatch", None, "field: " + ", ".join(fields)),
                         alert=alerts.env_error(), detail=detail, evidence={"fields": fields})


def _ny_date(read_at: str) -> str:
    t = datetime.fromisoformat(read_at)
    if t.tzinfo is None:
        raise ValueError(f"read_at {read_at!r} carries no UTC offset; run_date is its America/New_York date (7.4)")
    return t.astimezone(NEW_YORK).date().isoformat()


def _match_of(fp: Fingerprint | Mapping[str, Any]) -> Mapping[str, Any]:
    """The match block of a record, of a 4.12 document, or a (partial) match block itself."""
    if isinstance(fp, Fingerprint):
        return fp.match
    inner = fp.get("match")
    return inner if isinstance(inner, Mapping) else fp


# -- assembly ---------------------------------------------------------------------------------------------------

def harness_block(*, version: str, ocr_engine: str, ocr_model: str, capture_lib: str, redaction_version: str,
                  region_registry_version: str, config_commit: str, config_tarball_sha256: str, allowlist_sha256: str,
                  diff_params: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The 4.12 `harness` match field (7.4, 8.5): the harness version, OCR and capture library versions, the
    redaction and Region Registry versions, the sentinel-config commit and tarball hash, the `gateway/allowlist.yaml`
    hash (`sha256:<hex>`, 6.4) and the settle and Diff Engine parameter hashes of `diff_engine.yaml`
    (`diff.params_sha256`; `diff_params` defaults to `diff.load_params()`)."""
    params = load_params() if diff_params is None else diff_params
    return {"version": version, "ocr_engine": ocr_engine, "ocr_model": ocr_model, "capture_lib": capture_lib,
            "settle_params_sha256": params_sha256(params, "settle"), "diff_engine_params_sha256": params_sha256(params),
            "redaction_version": redaction_version, "region_registry_version": region_registry_version,
            "config_commit": config_commit, "config_tarball_sha256": config_tarball_sha256,
            "allowlist_sha256": allowlist_sha256}


def os_fields(os_reads: OsReads, harness: Mapping[str, str]) -> dict[str, Any] | ControlResult:
    """The OS-class `match` fields read at gate `provisioned` and the run's `harness` (choice 3), for G5 when both
    halves are provisioned; an unreadable read is `env_error (gate: provisioned, reason:
    fingerprint_unreadable:<field>)` (7.4)."""
    reads = _os_values(os_reads)
    bad = _unreadable_fields(reads)
    if not isinstance(os_reads.time_zone_os, str):
        bad["time_zone"] = "provisioned"
    if bad:
        return _unreadable("fingerprint", bad)
    _check_supplied({"harness": harness})
    return {**{f: _plain(v) for f, v in reads.items() if f != "rig"}, "harness": _plain(harness)}


def build_fingerprint(os_reads: OsReads, app_reads: AppReads, run_id: str, shard_id: str, side: str, rig_id: str,
                      harness: Mapping[str, str], *, data_epoch: Mapping[str, str], account: Mapping[str, str],
                      read_at: str, trading_24h_list_sha256: str | None = None) -> Fingerprint | ControlResult:
    """The 4.12 fingerprint of one side of one shard from the live reads (7.4), `fingerprint_id` over the match
    fields without `build_id`, `run_date` the New York date of `read_at` (the time the last read completed); or, for
    any field the rig could not read, `env_error (fingerprint_unreadable:<field>)` at the read's gate (choice 2).
    `data_epoch` is `{epoch_id, data_mode}` (4.9), `account` `{account_id_sha256, login_sha256, role}` (redacted),
    `harness` from `harness_block`. The result passes `lint_fingerprint` (L-FPR-00, L-FPR-01)."""
    reads = {**_os_values(os_reads), **_app_values(app_reads)}
    bad = _unreadable_fields(reads)
    time_zone = None
    if not isinstance(os_reads.time_zone_os, str):
        bad["time_zone"] = "provisioned"
    elif isinstance(app_reads.time_zone_app, Mapping):
        time_zone = {**_plain(app_reads.time_zone_app), "os": os_reads.time_zone_os}
    if "time_zone" not in bad and _unreadable_read("time_zone", time_zone):
        bad["time_zone"] = "launched"                    # the OS half is a string: the app half is at fault
    if bad:
        return _unreadable("fingerprint", bad)
    supplied: dict[str, Any] = {"harness": harness, "data_epoch": data_epoch, "account": account,
                                "run_date": _ny_date(read_at)}
    if trading_24h_list_sha256 is not None:
        supplied["trading_24h_list_sha256"] = trading_24h_list_sha256
    _check_supplied(supplied)
    match = {f: _plain(v) for f, v in reads.items() if f not in ("rig", "jar_listing_hash")}
    match.update(time_zone=time_zone, data_epoch=_plain(data_epoch), harness=_plain(harness))
    record = {"jar_listing_hash": reads["jar_listing_hash"], "account": _plain(account), "rig": _plain(reads["rig"]),
              "run_date": supplied["run_date"]}
    if trading_24h_list_sha256 is not None:
        record["trading_24h_list_sha256"] = trading_24h_list_sha256
    fp = Fingerprint(run_id=run_id, side=side, rig_id=rig_id, read_at=read_at, match=match, record=record,
                     shard_id=shard_id)
    violations = lint_fingerprint(document(fp))
    if violations:
        raise ValueError("; ".join(v.line() for v in violations))
    return fp


def document(fp: Fingerprint) -> dict[str, Any]:
    """The 4.12 document of a record, the shape `schemas/fingerprint.schema.json` and `lint_fingerprint` read (the
    shard id is the store's column, not a schema field)."""
    return {"schema_version": fp.schema_version, "fingerprint_id": fp.fingerprint_id, "run_id": fp.run_id,
            "side": fp.side, "rig_id": fp.rig_id, "read_at": fp.read_at, "match": fp.match, "record": fp.record}


# -- checks -----------------------------------------------------------------------------------------------------

def rig_spec_check(fp: Fingerprint, *, rig_kind: str = "vm", display_config: Mapping[str, Any] | None = None,
                   allowlist_sha256: str | None = None, manifest: Mapping[str, Any] | None = None) -> ControlResult | None:
    """L-FPR-02 at run start (7.1): None when `match` equals the rig specification for the rig kind, the workspace's
    configs/allowed.yaml entry, the config commit's allowlist hash and the snapshot manifest's `match` block
    (`lint_fingerprint_rig_spec`); otherwise the outcome SD-71 gives the values that differ (choice 6), on every
    scoped checkpoint."""
    violations = lint_fingerprint_rig_spec(document(fp), allowlist_sha256, rig_kind=rig_kind,
                                           display_config=display_config, manifest=manifest)
    if not violations:
        return None
    owners = {_owner(v.path) for v in violations}
    detail = "; ".join(v.message for v in violations)
    ev = {"violations": [v.path for v in violations]}
    if "H3" in owners:
        run, un = harness_abort_outcome("dpi_awareness", detail)
        return ControlResult("rig_spec", False, "provisioned", run=run, unreached=un, alert=alerts.aborted_harness(),
                             detail=detail, evidence=ev)
    items = [i for i in PREFLIGHT_ITEMS if i in owners]
    if items:
        gate, reason = f"preflight.{items[0]}", "gate"
    elif None in owners:
        gate, reason = "provisioned", "rig_spec_mismatch"
    else:
        gate, reason = _SETTINGS, "fixture_drift"
    return ControlResult("rig_spec", False, gate, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", reason, gate, detail), alert=alerts.env_error(), detail=detail,
                         evidence=ev)


def _owner(path: str) -> str | None:
    """The 12.7 item (or the settings gate) that reads the value at a `match.<field>...` path; None for none."""
    parts = path.split(".")[1:]
    for n in range(len(parts), 0, -1):
        owner = _ITEM_OF.get(".".join(parts[:n]))
        if owner is not None:
            return owner
    return None


def g5(candidate: Fingerprint | Mapping[str, Any], base: Fingerprint | Mapping[str, Any],
       calibrated_keys: Container[str] | None = None) -> str:
    """12.7 G5, fingerprint pre-match (choice 4): `fingerprint_mismatch:<fields>` when a field read on both sides
    differs (`build_id` aside), else `no_calibration` when `calibrated_keys` is given and lacks the pair's
    `calibration_key` (7.13), else `pass`. Each side is a record, a 4.12 document or a partial match block such as
    `os_fields`; with `calibrated_keys` both must carry the rendering-key fields (ValueError otherwise)."""
    c, b = _match_of(candidate), _match_of(base)
    differing = fingerprint_differences(b, c, set(c) & set(b))
    if differing:
        return G5_MISMATCH + ",".join(differing)
    if calibrated_keys is not None and calibration_key(c) not in calibrated_keys:
        return G5_NO_CALIBRATION
    return G5_PASS


def pair_check(candidate: Fingerprint | Mapping[str, Any], base: Fingerprint | Mapping[str, Any]) -> ControlResult | None:
    """L-FPR-03 and L-FPR-05 on two whole fingerprints, before the first capture of the second half and after every
    relaunch (7.4): None when every match field but `build_id` is equal, else `not_comparable (fingerprint_mismatch)`
    with the field names for the shard's checkpoints without an outcome. That `build_id` is each side's requested
    build is `orchestrator.gates.check_launched`'s (7.3)."""
    differing = fingerprint_differences(_match_of(base), _match_of(candidate))
    return _mismatch("fingerprint_pair", differing) if differing else None


def mismatch_note(fields: Iterable[str]) -> str | None:
    """L-FPR-05: the morning report's sentence for a render_probe_hash mismatch inside one Windows build."""
    names = set(fields)
    return RASTERIZATION_NOTE if "render_probe_hash" in names and "windows_build" not in names else None


def cheap_reread(os_reads: OsReads, title_build_id: str | None) -> dict[str, Any]:
    """The cheap fields re-read at a flow boundary (3.3, 7.4): `build_id` from the main-window title, cleartype,
    display and input_desktop from the OS reads; None marks an unreadable read."""
    return {"build_id": title_build_id, **{f: _plain(getattr(os_reads, f)) for f in CHEAP_FIELDS}}


def midrun_fingerprint_change(start: Fingerprint, reread: Fingerprint | Mapping[str, Any]) -> ControlResult | None:
    """The re-read at a flow boundary (`cheap_reread`) or after a relaunch (a fingerprint rebuilt from fresh reads)
    against the session's start fingerprint (3.3, 7.4, choice 5): None when nothing changed; `env_error (gate:
    launched, reason: version_changed_mid_run)` when a VERSION_FIELDS value changed; `env_error
    (fingerprint_unreadable:<field>)` at the field's gate when a re-read failed; `not_comparable
    (fingerprint_mismatch)` with the fields when any other match field changed. Each covers the rest of the shard."""
    was = {**start.match, **({"jar_listing_hash": start.record["jar_listing_hash"]}
                             if "jar_listing_hash" in start.record else {})}
    if isinstance(reread, Fingerprint):
        now = {**reread.match, **({"jar_listing_hash": reread.record["jar_listing_hash"]}
                                  if "jar_listing_hash" in reread.record else {})}
    else:
        now = {f: _plain(v) for f, v in reread.items()}
    unknown = sorted(set(now) - set(was))
    if unknown:
        raise ValueError(f"re-read fields {unknown} are not in the start fingerprint")
    bad = _unreadable_fields(now)
    readable = {f: v for f, v in now.items() if f not in bad}
    changed = [f for f in VERSION_FIELDS if f in readable and readable[f] != was[f]]
    if changed:
        return ControlResult("fingerprint_reread", False, "launched", run=RunVerdict("complete_with_env_errors"),
                             unreached=Outcome("env_error", "version_changed_mid_run", "launched",
                                               "field: " + ", ".join(changed)),
                             alert=alerts.env_error(), detail="changed mid-run: " + ", ".join(changed),
                             evidence={"fields": changed})
    if bad:
        return _unreadable("fingerprint_reread", bad)
    differing = fingerprint_differences(was, readable, set(readable) - set(VERSION_FIELDS))
    return _mismatch("fingerprint_reread", differing) if differing else None
