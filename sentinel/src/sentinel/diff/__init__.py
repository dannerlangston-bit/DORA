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

Every numeric parameter lives in `diff_engine.yaml` (3.7: "All parameters live in `diff_engine.yaml` in
git"), which 4.1 places at the root of the run's pinned `sentinel-config` checkout ("any change re-runs the canary
catalog before the next product run"). `load_params(config_root=<checkout>)` reads that file; `params_path` says
which file a call reads, and `params_sha256` gives the hashes the fingerprint's `harness` field carries
(`diff_engine_params_sha256`, `settle_params_sha256`, 4.12), so a parameter change shows in the run record.

Choice (reported): the file is taken in this order: an explicit `path`; `<config_root>/diff_engine.yaml` when the
checkout holds one; `$SENTINEL_DIFF_PARAMS`; the harness's `config/diff_engine.yaml` (`DEFAULT_PARAMS_PATH`), the
fallback for tests and for a checkout that has not committed its own file yet.
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
CONFIG_FILE = "diff_engine.yaml"            # 4.1: at the root of sentinel-config


def params_path(path: str | os.PathLike | None = None, config_root: str | os.PathLike | None = None) -> Path:
    """The `diff_engine.yaml` a `load_params` call reads (the module docstring's order)."""
    if path:
        return Path(path)
    if config_root is not None and (Path(config_root) / CONFIG_FILE).is_file():
        return Path(config_root) / CONFIG_FILE
    return Path(os.environ.get("SENTINEL_DIFF_PARAMS", DEFAULT_PARAMS_PATH))


def load_params(path: str | os.PathLike | None = None, config_root: str | os.PathLike | None = None
                ) -> Mapping[str, Any]:
    """Load the Diff Engine parameters as a nested dict: `path`, else the `diff_engine.yaml` of the config checkout
    `config_root`, else `$SENTINEL_DIFF_PARAMS`, else the harness default.

    The result is cached per file and shared: treat it as read-only.
    """
    return _load(params_path(path, config_root))


@lru_cache(maxsize=8)
def _load(p: Path) -> Mapping[str, Any]:
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"{p}: diff_engine.yaml must be a mapping with schema_version 1")
    return data


def clear_cache() -> None:
    """Forget every parsed parameters file (after a file was rewritten in place)."""
    _load.cache_clear()


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
