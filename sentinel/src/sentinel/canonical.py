"""Canonical JSON and the ids hashed over it: one definition for the lint, the Diff Engine and the store.

- `canonical_json` (4.12, 4.9): UTF-8 text with sorted keys, no whitespace between tokens and non-ASCII characters
  kept as themselves (`ensure_ascii=False`). Nothing is coerced: a value JSON cannot hold (a date, bytes, an
  object) raises TypeError instead of being turned into its `str()`, so two callers can never hash one value two
  ways. Before this module the lint (ensure_ascii False) and the Diff Engine (ensure_ascii True, `default=str`)
  gave different ids for a match with a non-ASCII value, such as a GPU name with a trademark sign.
- `fingerprint_id` (4.12, 7.4): `sha256:` plus the hex digest of the canonical JSON of the fingerprint's `match`
  block without `build_id`. `fingerprint_id_of` takes the whole fingerprint document (the form L-FPR-01 reads).
- `epoch_id` (4.9, SD-38): over the canonical JSON of the epoch fields other than `epoch_id` and the 24-hour
  trading list hash, which 7.4 carries as a `record` field and never as an epoch input.
- `calibration_key` (4.12 Calibration key, 3.9, 7.13, SD-69): the rendering key, `sha256:` plus the hex digest of
  the canonical JSON of the `match` fields that affect pixels: `windows_build`, `display`, `cleartype`, `gpu`,
  `os_theme`, `app_theme`, `jre_release`, `vmoptions_hash`, `java2d_pipeline`, `render_probe_hash`, and of the
  `harness` subfields `capture_lib`, `settle_params_sha256` and `diff_engine_params_sha256`. `build_id`,
  `data_epoch`, `workspace_hash` and every other field stay out, so a new epoch or corpus file never discards a
  calibration. Choices (4.12 names the fields, not the shape): the hashed object keeps the match's own nesting,
  `harness` holding only its three subfields; a missing field raises ValueError rather than hashing as absent,
  since L-FPR-01 makes every match field non-null.
- `calibration_filename` (4.1, 4.12, 4.16, SD-36 with SD-69): `<hex>.yaml`, the calibration key without its
  `sha256:` prefix, so no file name holds a colon. The file lives under `CALIBRATION_DIR` of `sentinel-config`.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SHA256_PREFIX = "sha256:"
CALIBRATION_DIR = "calibration"
# SD-38: epoch fields that are recorded with the epoch but are not inputs of epoch_id (7.4: class record)
EPOCH_RECORD_FIELDS = frozenset({"trading_24h_list_sha256"})
# 4.12 Calibration key (SD-69): the match fields that affect pixels, and the pixel-affecting harness subfields
CALIBRATION_KEY_FIELDS = ("windows_build", "display", "cleartype", "gpu", "os_theme", "app_theme", "jre_release",
                          "vmoptions_hash", "java2d_pipeline", "render_probe_hash")
CALIBRATION_KEY_HARNESS = ("capture_lib", "settle_params_sha256", "diff_engine_params_sha256")
_SHA256_ID = re.compile(r"^sha256:([0-9a-f]{64})$")


def canonical_json(obj: Any) -> str:
    """The canonical JSON text of `obj` (module docstring); TypeError for a value JSON cannot hold."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_id(obj: Any) -> str:
    """`sha256:` plus the hex digest of the UTF-8 bytes of `canonical_json(obj)`."""
    return SHA256_PREFIX + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def fingerprint_id(match: Mapping[str, Any]) -> str:
    """4.12: the id over the canonical JSON of the `match` fields, `build_id` removed (7.4)."""
    return sha256_id({k: v for k, v in match.items() if k != "build_id"})


def fingerprint_id_of(fingerprint: Mapping[str, Any]) -> str:
    """`fingerprint_id` of a whole 4.12 fingerprint document; a missing or malformed `match` hashes as empty."""
    match = fingerprint.get("match")
    return fingerprint_id(match if isinstance(match, Mapping) else {})


def epoch_id(epoch: Mapping[str, Any]) -> str:
    """4.9: the id over the canonical JSON of the epoch fields other than `epoch_id` and the record-only fields
    of SD-38."""
    return sha256_id({k: v for k, v in epoch.items() if k != "epoch_id" and k not in EPOCH_RECORD_FIELDS})


def calibration_key(match: Mapping[str, Any]) -> str:
    """4.12: the calibration (rendering) key of a fingerprint's `match` block (module docstring); ValueError when a
    field it hashes is missing."""
    harness = match.get("harness")
    harness = harness if isinstance(harness, Mapping) else {}
    missing = [f for f in CALIBRATION_KEY_FIELDS if f not in match]
    missing += [f"harness.{k}" for k in CALIBRATION_KEY_HARNESS if k not in harness]
    if missing:
        raise ValueError(f"the calibration key needs the match fields {missing} (4.12)")
    subset = {f: match[f] for f in CALIBRATION_KEY_FIELDS}
    subset["harness"] = {k: harness[k] for k in CALIBRATION_KEY_HARNESS}
    return sha256_id(subset)


def calibration_key_of(fingerprint: Mapping[str, Any]) -> str:
    """`calibration_key` of a whole 4.12 fingerprint document (ValueError for a missing or malformed `match`)."""
    match = fingerprint.get("match")
    if not isinstance(match, Mapping):
        raise ValueError("a fingerprint without a match block has no calibration key (4.12)")
    return calibration_key(match)


def calibration_filename(calibration_key: str) -> str:
    """SD-36 with SD-69: the file name `<hex>.yaml` of `calibration/` for a `sha256:<64 hex>` calibration key
    (ValueError otherwise)."""
    m = _SHA256_ID.match(calibration_key)
    if m is None:
        raise ValueError(f"calibration key {calibration_key!r} is not sha256:<64 lowercase hex> (4.12)")
    return f"{m.group(1)}.yaml"
