"""The Diff Engine package (3.2 to 3.10, 4.8, 4.12): captures in, per-region metrics and findings out.

Modules:

- `phash`: the 64-bit DCT perceptual hash and Hamming distance of 3.7.
- `regions`: Region Registry records and semantics of 3.8 and 4.8 (kinds, landmark-anchored
  offsets, mask expiry, the mask budget, money cells, the threshold record a region carries).
- `engine`: the three-granularity diff of 3.7 (landmarks, translation, YIQ per-pixel diff with
  anti-aliasing detection, SSIM, CIEDE2000, connected components), the fingerprint
  comparability rule of 3.3 and 4.12, and the run-level global-shift pre-pass.
- `calibration`: the A/A procedure and threshold derivation of 3.9.
- `settle`: the settle rule of 3.5 and the targeted re-run stability logic of 3.10.

Every numeric parameter lives in `config/diff_engine.yaml` (3.7: "All parameters live in
`diff_engine.yaml` in git"); `load_params` reads it, and `params_sha256` gives the hashes the
fingerprint's `harness` field carries (`diff_engine_params_sha256`, `settle_params_sha256`, 4.12).
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULT_PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "diff_engine.yaml"


@lru_cache(maxsize=4)
def load_params(path: str | os.PathLike | None = None) -> Mapping[str, Any]:
    """Load `diff_engine.yaml` (or `$SENTINEL_DIFF_PARAMS`, or `path`) as a nested dict.

    The result is cached and shared: treat it as read-only.
    """
    p = Path(path) if path else Path(os.environ.get("SENTINEL_DIFF_PARAMS", DEFAULT_PARAMS_PATH))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"{p}: diff_engine.yaml must be a mapping with schema_version 1")
    return data


def params_sha256(params: Mapping[str, Any], section: str | None = None) -> str:
    """sha256 over the canonical JSON of the parameters (or of one section, e.g. "settle").

    The diff hash excludes the `settle` section, which the fingerprint hashes on its own (4.12).
    """
    if section is not None:
        body: Any = params[section]
    else:
        body = {k: v for k, v in params.items() if k != "settle"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
