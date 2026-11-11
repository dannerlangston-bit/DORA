"""Tests for sentinel.canonical: one canonical JSON for fingerprint ids, epoch ids and calibration file names
(4.9, 4.12, 7.4, SD-36, SD-38)."""
import hashlib
import json
from datetime import date

import pytest

from sentinel import canonical as C
from sentinel.diff import engine
from sentinel.lint import rules

MATCH = {"build_id": "branch-1998.3.0-7be42d0", "gpu": {"name": "Radeon™ Graphics", "driver_version": "31.0"},
         "app_theme": "darké", "display": {"width": 1920, "height": 1080}, "render_probe_hash": "e3b0c44298fc1c14"}


def test_canonical_json_is_utf8_sorted_and_compact():
    assert C.canonical_json({"b": 1, "a": ["™", 2]}) == '{"a":["™",2],"b":1}'


def test_canonical_json_never_coerces_a_value():
    with pytest.raises(TypeError):
        C.canonical_json({"run_date": date(2026, 11, 9)})


def test_fingerprint_id_ignores_build_id_and_hashes_non_ascii_as_utf8():
    want = "sha256:" + hashlib.sha256(json.dumps(
        {k: v for k, v in MATCH.items() if k != "build_id"}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    assert C.fingerprint_id(MATCH) == want
    assert C.fingerprint_id({**MATCH, "build_id": "main-1998.2.0-4f1c9a2"}) == want
    assert C.fingerprint_id({**MATCH, "app_theme": "light"}) != want


def test_lint_and_engine_give_one_id_for_a_non_ascii_match():
    """04b/07-fingerprint-id-duplicate: before, the engine escaped non-ASCII and the lint did not."""
    fp = {"fingerprint_id": None, "match": MATCH}
    assert engine.fingerprint_id(MATCH) == rules.fingerprint_id(fp) == C.fingerprint_id_of(fp)
    assert engine.fingerprint_id is C.fingerprint_id


def test_fingerprint_id_of_a_malformed_document_hashes_an_empty_match():
    assert C.fingerprint_id_of({"match": ["not", "a", "map"]}) == C.fingerprint_id({})
    assert C.fingerprint_id_of({}) == C.fingerprint_id({})


def test_epoch_id_excludes_its_own_id_and_the_24h_list_hash():
    """SD-38: the 24-hour trading list hash is recorded, never an epoch_id input."""
    epoch = {"window_date": "2026-11-09", "front_month": {"/ES": "ESZ26"}, "data_delay": "delayed_20"}
    base = C.epoch_id(epoch)
    assert base == C.sha256_id(epoch)
    assert C.epoch_id({**epoch, "epoch_id": "sha256:x", "trading_24h_list_sha256": "sha256:aa"}) == base
    assert C.epoch_id({**epoch, "data_delay": "realtime"}) != base
    assert rules.epoch_id({**epoch, "trading_24h_list_sha256": "sha256:bb"}) == base


def test_calibration_filename_drops_the_prefix():
    hexd = "9a3f" + "0" * 60
    assert C.calibration_filename("sha256:" + hexd) == f"{hexd}.yaml"
    assert ":" not in C.calibration_filename(C.fingerprint_id(MATCH))
    for bad in ("9a3f" + "0" * 60, "sha256:9a3f...c1e2", "sha256:" + "A" * 64, "md5:" + "0" * 64):
        with pytest.raises(ValueError):
            C.calibration_filename(bad)


RENDER_MATCH = {
    "build_id": "branch-1998.3.0-7be42d0", "jre_release": {"sha256": "sha256:" + "1" * 64, "java_version": "17.0.12"},
    "vmoptions_hash": "sha256:" + "2" * 64, "java2d_pipeline": {"pinned": "software", "confirmed": "software"},
    "windows_build": {"current_build": "26100", "ubr": "2314"}, "display": {"width": 1920, "height": 1080, "dpi": 96},
    "cleartype": {"smoothing": True}, "gpu": {"name": "Hyper-V Video", "driver_version": "10.0"},
    "os_theme": {"apps_light": False}, "app_theme": {"scheme": "dark", "font_size": "medium"},
    "workspace_hash": {"ws.tier-a.active-trader": "sha256:" + "3" * 64}, "settings_profile_hash": "sha256:" + "4" * 64,
    "time_zone": {"os": "Eastern Standard Time"}, "keyboard": {"layout": "0x0409", "locale": "en-US"},
    "data_epoch": {"epoch_id": "sha256:" + "5" * 64, "data_mode": "backend_fixture"}, "input_desktop": "Default",
    "harness": {"version": "1.4.2", "ocr_engine": "tesseract-5.3.4", "capture_lib": "mss-9.0.1",
                "settle_params_sha256": "sha256:" + "6" * 64, "diff_engine_params_sha256": "sha256:" + "7" * 64,
                "redaction_version": "r3", "config_commit": "c41a0f2"},
    "render_probe_hash": "e3b0c44298fc1c14",
}


def test_calibration_key_hashes_the_rendering_fields_only():
    """4.12 Calibration key, SD-69: a new epoch, corpus file, build, OCR engine or config commit keeps the key."""
    key = C.calibration_key(RENDER_MATCH)
    subset = {f: RENDER_MATCH[f] for f in C.CALIBRATION_KEY_FIELDS}
    subset["harness"] = {k: RENDER_MATCH["harness"][k] for k in C.CALIBRATION_KEY_HARNESS}
    assert key == C.sha256_id(subset) and key != C.fingerprint_id(RENDER_MATCH)
    for name, value in (("build_id", "main-1998.2.0-4f1c9a2"), ("data_epoch", {"epoch_id": "sha256:" + "9" * 64}),
                        ("workspace_hash", {}), ("keyboard", {"layout": "0x0407"}), ("settings_profile_hash", "x")):
        assert C.calibration_key({**RENDER_MATCH, name: value}) == key, name
    for sub in ("ocr_engine", "config_commit", "redaction_version"):
        assert C.calibration_key({**RENDER_MATCH, "harness": {**RENDER_MATCH["harness"], sub: "changed"}}) == key
    for name, value in (("render_probe_hash", "ffffffffffffffff"), ("app_theme", {"scheme": "light"}),
                        ("java2d_pipeline", {"pinned": "d3d", "confirmed": "d3d"})):
        assert C.calibration_key({**RENDER_MATCH, name: value}) != key, name
    assert C.calibration_key({**RENDER_MATCH, "harness": {**RENDER_MATCH["harness"], "capture_lib": "mss-10"}}) != key
    assert C.calibration_key_of({"match": RENDER_MATCH}) == key


def test_calibration_key_refuses_a_partial_match():
    with pytest.raises(ValueError, match="gpu"):
        C.calibration_key({k: v for k, v in RENDER_MATCH.items() if k != "gpu"})
    with pytest.raises(ValueError, match="harness.capture_lib"):
        C.calibration_key({**RENDER_MATCH, "harness": {"version": "1"}})
    with pytest.raises(ValueError):
        C.calibration_key_of({"match": None})


def test_the_calibration_file_is_named_by_the_calibration_key(tmp_path):
    """SD-36 with SD-69: calibration/<hex>.yaml, hex the calibration key, never the fingerprint id."""
    from sentinel.diff.calibration import CalibrationFile
    key = C.calibration_key(RENDER_MATCH)
    path = CalibrationFile(key, "run-cal-1").save(tmp_path)
    assert path.name == C.calibration_filename(key) == key[len("sha256:"):] + ".yaml"
    assert CalibrationFile.load(tmp_path, key) is not None
    assert CalibrationFile.load(tmp_path, C.fingerprint_id(RENDER_MATCH)) is None
