"""Tests for the 4.12 fingerprint: assembly from the live reads, unreadable fields, the rig specification with SD-71's
precedence, G5, the pair check and the mid-run re-reads (3.3, 4.12, 7.1, 7.4, 7.13, 12.7 G5)."""
import dataclasses

import pytest

from sentinel import fingerprint as F
from sentinel.canonical import calibration_key, fingerprint_id
from sentinel.diff import load_params, params_sha256
from sentinel.lint import lint_fingerprint
from sentinel.orchestrator.ports import AppReads, OsReads
from sentinel.orchestrator.results import ControlResult
from sentinel.records import Fingerprint
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

H = "sha256:" + "c" * 64
RUN, SHARD, RIG = "run-2026-11-09-pr-0931", "run-2026-11-09-pr-0931/1", "rig-01"
CAND, BASE = "branch-1998.3.0-5d3e812", "main-1998.2.0-4f1c9a2"
EPOCH = {"epoch_id": "sha256:" + "3" * 64, "data_mode": "backend_fixture"}
ACCOUNT = {"account_id_sha256": H, "login_sha256": H, "role": "empty"}
READ_AT = "2026-11-09T14:02:11-05:00"
ALLOWED = {"id": "cfg.1920x1080-100.dark-medium", "width": 1920, "height": 1080, "scale_percent": 100, "theme": "dark",
           "font_size": "medium", "work_area": {"w": 1920, "h": 1009}}


def harness(**kw):
    return F.harness_block(version="1.4.2", ocr_engine="tesseract-5.3.4", ocr_model="eng-best-4.1",
                           capture_lib="mss-9.0.1", redaction_version="r3", region_registry_version="c41a0f2",
                           config_commit="c41a0f2", config_tarball_sha256=H, allowlist_sha256=kw.pop("allowlist", H),
                           **kw)


def os_reads(**kw) -> OsReads:
    """Synthetic OS reads equal to the 7.1 rig specification of a VM rig."""
    base = dict(
        windows_build={"current_build": "26100", "ubr": "2314"},
        display={"width": 1920, "height": 1080, "bits": 32, "frequency": 60, "scale_percent": 100, "dpi": 96,
                 "awareness": "PER_MONITOR_AWARE_V2", "monitors": 1},
        cleartype={"smoothing": True, "type": "FE_FONTSMOOTHINGCLEARTYPE", "gamma": 1800, "contrast": 1400,
                   "orientation": "RGB"},
        gpu={"name": "Hyper-V Video", "driver_version": "10.0.26100.1"},
        os_theme={"apps_light": False, "system_light": False, "transparency": False, "animations": False},
        keyboard={"layout": "0x0409", "layout_count": 1, "toggle_keys": {"caps": False, "num": True, "scroll": False},
                  "locale": "en-US"},
        time_zone_os="Eastern Standard Time", input_desktop="Default",
        rig={"hostname": "tos-rig-01", "hypervisor": "hyperv", "hypervisor_version": "10.0.26100",
             "snapshot_ids": ("snap-2026-10-28-base",), "vm_image_id": "tos-rig-win11-2026-10-28"})
    return OsReads(**{**base, **kw})


def app_reads(**kw) -> AppReads:
    base = dict(
        build_id_title=CAND, build_id_about=CAND, jre_release={"sha256": H, "java_version": "17.0.12"},
        vmoptions_hash=H, java2d_pipeline={"pinned": "software", "confirmed": "software"},
        app_theme={"scheme": "dark", "font_size": "medium"}, workspace_hash={"ws.tier-a.futures-trader": H},
        settings_profile_hash=H, time_zone_app={"app_display": "America/New_York", "chart": "America/New_York"},
        render_probe_hash="e3b0c44298fc1c14", jar_listing_hash=H)
    return AppReads(**{**base, **kw})


def build(os=None, app=None, side="candidate", **kw):
    return F.build_fingerprint(os or os_reads(), app or app_reads(), RUN, SHARD, side, RIG, kw.pop("harness", harness()),
                               data_epoch=kw.pop("data_epoch", EPOCH), account=ACCOUNT, read_at=kw.pop("read_at", READ_AT),
                               **kw)


def fp(os=None, app=None, **kw) -> Fingerprint:
    out = build(os, app, **kw)
    assert isinstance(out, Fingerprint), out
    return out


def outcome(r: ControlResult):
    u = r.unreached
    return u.outcome, u.reason, u.gate, u.detail


# -- assembly (7.4, 4.12) ---------------------------------------------------------------------------------------

def test_fingerprint_is_assembled_from_the_reads():
    f = fp(trading_24h_list_sha256=H)
    assert lint_fingerprint(F.document(f)) == []
    assert f.fingerprint_id == fingerprint_id(f.match) and f.shard_id == SHARD and f.build_id == CAND
    assert f.match["time_zone"] == {"os": "Eastern Standard Time", "app_display": "America/New_York",
                                    "chart": "America/New_York"}
    assert f.match["data_epoch"] == EPOCH and f.match["harness"]["allowlist_sha256"] == H
    assert f.record == {"jar_listing_hash": H, "account": ACCOUNT, "run_date": "2026-11-09",
                        "rig": {**os_reads().rig, "snapshot_ids": ["snap-2026-10-28-base"]},
                        "trading_24h_list_sha256": H}
    assert "trading_24h_list_sha256" not in fp().record          # optional record field, never null
    # the candidate and base of one environment share the id: build_id is not part of it
    assert fp(app=app_reads(build_id_title=BASE), side="base").fingerprint_id == f.fingerprint_id


def test_run_date_is_the_new_york_date():
    assert fp(read_at="2026-11-10T03:30:00Z").record["run_date"] == "2026-11-09"
    assert fp(read_at="2026-11-10T05:30:00Z").record["run_date"] == "2026-11-10"
    with pytest.raises(ValueError, match="UTC offset"):
        build(read_at="2026-11-10T03:30:00")


def test_build_id_falls_back_to_help_about():
    assert fp(app=app_reads(build_id_title=None, build_id_about=BASE)).build_id == BASE


def test_harness_block_hashes_the_diff_engine_parameters():
    params = load_params()
    h = harness()
    assert h["settle_params_sha256"] == params_sha256(params, "settle")
    assert h["diff_engine_params_sha256"] == params_sha256(params) != h["settle_params_sha256"]
    changed = {**params, "settle": {**params["settle"], "regression_factor": 99}}
    assert harness(diff_params=changed)["settle_params_sha256"] != h["settle_params_sha256"]


# -- unreadable fields (7.4, 7.1, L-FPR-01) -----------------------------------------------------------------------

def test_an_unreadable_os_field_is_env_error_at_provisioned():
    r = build(os=os_reads(cleartype=None))
    assert isinstance(r, ControlResult) and not r.passed and r.gate == "provisioned"
    assert outcome(r) == ("env_error", "fingerprint_unreadable:cleartype", "provisioned", "unreadable: cleartype")
    assert r.run_status == "complete_with_env_errors" and "on_call_channel" in r.alert.notified


def test_an_unreadable_app_field_is_env_error_at_launched():
    assert outcome(build(app=app_reads(render_probe_hash=None)))[:3] == (
        "env_error", "fingerprint_unreadable:render_probe_hash", "launched")
    assert outcome(build(app=app_reads(build_id_title=None, build_id_about=None)))[:3] == (
        "env_error", "fingerprint_unreadable:build_id", "launched")
    assert outcome(build(app=app_reads(jar_listing_hash=None)))[1] == "fingerprint_unreadable:jar_listing_hash"


def test_the_first_os_field_names_the_reason_and_every_field_is_listed():
    r = build(os=os_reads(gpu=None, rig=None), app=app_reads(vmoptions_hash=None))
    assert outcome(r) == ("env_error", "fingerprint_unreadable:gpu", "provisioned",
                          "unreadable: vmoptions_hash, gpu, rig")


def test_a_read_of_the_wrong_shape_is_unreadable():
    gamma_missing = {"smoothing": True, "type": "FE_FONTSMOOTHINGCLEARTYPE", "contrast": 1400, "orientation": "RGB"}
    assert outcome(build(os=os_reads(cleartype=gamma_missing)))[1] == "fingerprint_unreadable:cleartype"
    assert outcome(build(app=app_reads(app_theme="dark")))[1:3] == ("fingerprint_unreadable:app_theme", "launched")


def test_each_half_of_the_time_zone_has_its_own_gate():
    assert outcome(build(os=os_reads(time_zone_os=None)))[1:3] == ("fingerprint_unreadable:time_zone", "provisioned")
    assert outcome(build(app=app_reads(time_zone_app=None)))[1:3] == ("fingerprint_unreadable:time_zone", "launched")
    assert outcome(build(app=app_reads(time_zone_app={"app_display": "America/New_York"})))[2] == "launched"


def test_a_malformed_run_supplied_field_is_a_harness_fault():
    with pytest.raises(ValueError, match="harness"):
        build(harness={k: v for k, v in harness().items() if k != "allowlist_sha256"})
    with pytest.raises(ValueError, match="data_epoch"):
        build(data_epoch={"epoch_id": "E1", "data_mode": "frozen"})


def test_os_fields_at_provisioned():
    m = F.os_fields(os_reads(), harness())
    assert set(m) == {"windows_build", "display", "cleartype", "gpu", "os_theme", "keyboard", "input_desktop", "harness"}
    assert m == {k: v for k, v in fp().match.items() if k in m}
    r = F.os_fields(os_reads(input_desktop=None), harness())
    assert outcome(r) == ("env_error", "fingerprint_unreadable:input_desktop", "provisioned", "unreadable: input_desktop")
    assert outcome(F.os_fields(os_reads(time_zone_os=None), harness()))[1] == "fingerprint_unreadable:time_zone"


# -- the rig specification, SD-71 (7.1, L-FPR-02) -----------------------------------------------------------------

def spec(f, **kw):
    return F.rig_spec_check(f, display_config=kw.pop("display_config", ALLOWED), allowlist_sha256=kw.pop("allowlist", H),
                            manifest=kw.pop("manifest", fp().match), **kw)


def test_a_fingerprint_on_the_rig_specification_passes():
    assert spec(fp()) is None


@pytest.mark.parametrize("os_kw,app_kw,gate", [
    ({"cleartype": {**os_reads().cleartype, "smoothing": False}}, {}, "preflight.R3"),
    ({"display": {**os_reads().display, "width": 1366}}, {}, "preflight.R2"),
    ({"keyboard": {**os_reads().keyboard, "toggle_keys": {"caps": True, "num": True, "scroll": False}}}, {}, "preflight.R4"),
    ({"windows_build": {"current_build": "26100", "ubr": "2400"}}, {}, "preflight.R1"),
    ({"input_desktop": "Winlogon"}, {}, "preflight.R6"),
    ({}, {"java2d_pipeline": {"pinned": "d3d", "confirmed": "d3d"}}, "preflight.B3"),
])
def test_a_value_a_preflight_item_reads_takes_that_items_outcome(os_kw, app_kw, gate):
    f = fp(os_reads(**os_kw), app_reads(**app_kw))
    r = spec(f, manifest=fp().match)
    assert outcome(r)[:3] == ("env_error", "gate", gate) and r.gate == gate
    assert r.run_status == "complete_with_env_errors"


@pytest.mark.parametrize("os_kw,app_kw,kw", [
    ({"os_theme": {**os_reads().os_theme, "transparency": True}}, {}, {}),
    ({"gpu": {"name": "Microsoft Basic Display Adapter", "driver_version": "10.0.26100.1"}}, {}, {}),
    ({}, {"app_theme": {"scheme": "light", "font_size": "medium"}}, {}),
    ({}, {}, {"allowlist": "sha256:" + "d" * 64}),
])
def test_a_value_no_item_reads_is_rig_spec_mismatch(os_kw, app_kw, kw):
    r = spec(fp(os_reads(**os_kw), app_reads(**app_kw)), **kw)
    assert outcome(r)[:3] == ("env_error", "rig_spec_mismatch", "provisioned")
    assert r.run_status == "complete_with_env_errors" and "on_call_channel" in r.alert.notified


def test_wrong_harness_awareness_is_the_h3_abort():
    r = spec(fp(os_reads(display={**os_reads().display, "awareness": "SYSTEM_AWARE"})))
    assert (r.run_status, r.run_reason) == ("aborted_harness", "dpi_awareness")
    assert (r.unreached.outcome, r.unreached.reason) == ("harness_error", "dpi_awareness")


def test_app_time_zones_are_the_settings_gate():
    r = spec(fp(app=app_reads(time_zone_app={"app_display": "America/Chicago", "chart": "America/New_York"})))
    assert outcome(r)[:3] == ("env_error", "fixture_drift", "settings_attested")


def test_the_first_item_wins_and_every_value_is_in_detail():
    f = fp(os_reads(gpu={"name": "other", "driver_version": "1"}, display={**os_reads().display, "dpi": 120},
                    cleartype={**os_reads().cleartype, "type": "FE_FONTSMOOTHINGSTANDARD"}))
    r = spec(f)
    assert outcome(r)[2] == "preflight.R2"
    assert {"match.display.dpi", "match.cleartype.type", "match.gpu.name"} <= set(r.evidence["violations"])
    assert "gpu.name" in r.detail and "cleartype.type" in r.detail


def test_a_physical_rig_pins_d3d():
    d3d = fp(app=app_reads(java2d_pipeline={"pinned": "d3d", "confirmed": "unknown"}))
    assert spec(d3d, rig_kind="physical", manifest=None) is None
    assert outcome(spec(fp(), rig_kind="physical", manifest=None))[2] == "preflight.B3"


# -- G5 and the pair check (7.4, 12.7 G5, L-FPR-03, L-FPR-05) -----------------------------------------------------

def test_g5_detail_convention():
    cand, base = fp(), fp(app=app_reads(build_id_title=BASE), side="base")
    key = calibration_key(cand.match)
    assert F.g5(cand, base, {key}) == F.G5_PASS == "pass"
    assert F.g5(cand, base, set()) == "no_calibration"
    other = fp(os_reads(gpu={"name": "other", "driver_version": "1"}, cleartype={**os_reads().cleartype, "gamma": 2200}))
    assert F.g5(other, base, set()) == "fingerprint_mismatch:cleartype,gpu"       # a mismatch before a calibration
    assert F.g5(F.document(cand), F.document(base)) == "pass"


def test_g5_at_provisioned_compares_the_fields_read_so_far():
    c_os = F.os_fields(os_reads(), harness())
    assert F.g5(c_os, F.os_fields(os_reads(), harness())) == "pass"
    assert F.g5(c_os, F.os_fields(os_reads(gpu={"name": "x", "driver_version": "1"}), harness())) == "fingerprint_mismatch:gpu"
    assert F.g5(c_os, fp(side="base")) == "pass"                   # second half against the first half's whole record
    assert F.g5(F.os_fields(os_reads(), harness(allowlist="sha256:" + "e" * 64)), c_os) == "fingerprint_mismatch:harness"
    with pytest.raises(ValueError):
        F.g5(c_os, c_os, set())                                    # no rendering key before the app fields


def test_pair_check_refuses_a_pair_that_differs():
    cand, base = fp(), fp(app=app_reads(build_id_title=BASE), side="base")
    assert F.pair_check(cand, base) is None
    r = F.pair_check(fp(os_reads(gpu={"name": "other", "driver_version": "1"})), base)
    assert outcome(r) == ("not_comparable", "fingerprint_mismatch", None, "field: gpu")
    assert r.run_status == "complete_with_env_errors" and "on_call_channel" in r.alert.notified


def test_a_render_probe_mismatch_says_rasterization():
    base = fp(app=app_reads(build_id_title=BASE), side="base")
    r = F.pair_check(fp(app=app_reads(render_probe_hash="e3b0c44298fc1c15")), base)
    assert outcome(r)[3] == "field: render_probe_hash" and F.RASTERIZATION_NOTE in r.detail
    assert F.mismatch_note(["render_probe_hash", "windows_build"]) is None
    both = fp(os_reads(windows_build={"current_build": "26200", "ubr": "1"}), app_reads(render_probe_hash="ffff"))
    assert F.RASTERIZATION_NOTE not in F.pair_check(both, base).detail


# -- re-reads at flow boundaries and after relaunch (3.3, 7.4, SD-73) ---------------------------------------------

def test_unchanged_cheap_fields_pass():
    assert F.midrun_fingerprint_change(fp(), F.cheap_reread(os_reads(), CAND)) is None
    assert set(F.cheap_reread(os_reads(), CAND)) == {"build_id", *F.CHEAP_FIELDS}


def test_a_cleartype_change_between_flows_is_a_fingerprint_mismatch():
    reread = F.cheap_reread(os_reads(cleartype={**os_reads().cleartype, "gamma": 2200}), CAND)
    r = F.midrun_fingerprint_change(fp(), reread)
    assert outcome(r) == ("not_comparable", "fingerprint_mismatch", None, "field: cleartype")
    assert r.run_status == "complete_with_env_errors"


def test_a_changed_title_is_version_changed_mid_run():
    r = F.midrun_fingerprint_change(fp(), F.cheap_reread(os_reads(input_desktop="Winlogon"), "branch-1998.3.1-0aa0aa0"))
    assert outcome(r) == ("env_error", "version_changed_mid_run", "launched", "field: build_id")   # wins over the desktop


def test_a_relaunch_that_rewrote_vmoptions_is_version_changed_mid_run():
    relaunched = fp(app=app_reads(vmoptions_hash="sha256:" + "f" * 64))
    assert outcome(F.midrun_fingerprint_change(fp(), relaunched))[:3] == ("env_error", "version_changed_mid_run", "launched")
    probe = fp(app=app_reads(render_probe_hash="e3b0c44298fc1c15"))
    assert outcome(F.midrun_fingerprint_change(fp(), probe))[:2] == ("not_comparable", "fingerprint_mismatch")
    assert F.midrun_fingerprint_change(fp(), fp(read_at="2026-11-10T09:00:00Z")) is None   # record fields only


def test_an_unreadable_reread_is_env_error_at_its_gate():
    r = F.midrun_fingerprint_change(fp(), F.cheap_reread(os_reads(display=None), CAND))
    assert outcome(r)[:3] == ("env_error", "fingerprint_unreadable:display", "provisioned")
    r = F.midrun_fingerprint_change(fp(), F.cheap_reread(os_reads(), None))
    assert outcome(r)[:3] == ("env_error", "fingerprint_unreadable:build_id", "launched")
    with pytest.raises(ValueError, match="bounds_source"):
        F.midrun_fingerprint_change(fp(), {"bounds_source": "B"})


# -- storage (8.3 env_fingerprints) -------------------------------------------------------------------------------

def test_one_row_per_shard_and_side(tmp_path):
    store = Store(tmp_path / "run.db")
    store.register_rig(RIG, login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    for b, channel in ((CAND, "branch"), (BASE, "main")):
        store.register_build(b, channel=channel, source_commit=b[-7:], pipeline_artifact_url="sim://b",
                             installer_sha256="c" * 64)
    store.begin_run(RunPlan(run_id=RUN, run_type="pr", config_commit="c41a0f2", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=2700, scope=(ScopeEntry("census/trade.active-trader.ladder/ws.tier-a.active-trader",
                                                               "ws.tier-a.active-trader", "active_trader", 1, "frozen"),),
                            shards=(ShardPlan(SHARD, RIG),), base_commit_distance=2, authorization_sha256="sha256:auth",
                            pr_committers=("ann",)))
    cand, base = fp(), fp(app=app_reads(build_id_title=BASE), side="base")
    for f in (cand, base, cand):                                   # the same fingerprint again is a no-op
        store.write_fingerprint(f)
    for side, f in (("candidate", cand), ("base", base)):
        stored = store.fingerprint(SHARD, side)
        assert stored.read_at == "2026-11-09T19:02:11+00:00"         # the store keeps UTC text (8.3)
        assert dataclasses.replace(stored, read_at=READ_AT) == f
    store.close()
