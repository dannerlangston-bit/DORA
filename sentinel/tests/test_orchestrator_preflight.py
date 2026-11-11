"""Preflight evaluators, the retry rule and the controller-side reads (5.3, 7.1, 7.11, 10.8, 12.7)."""
from __future__ import annotations

import shutil
from collections import namedtuple
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gates import PREFLIGHT_ITEMS, check_provisioned
from sentinel.orchestrator.ports import PreflightRead, PreflightReads
from sentinel.orchestrator.preflight import (CATALOGS, PreflightPins, SimulatedPreflightReads, catalog_versions,
                                             combine, evaluate, frame_uniform, g1_read, g3_read, g4_read, g5_read,
                                             h4_expected_drops, merge_reads, probe_pattern, run_preflight)
from sentinel.rigfiles import vmoptions_sha256
from sentinel.store import Store
from sentinel.store.preflight import GB, MB, storage_preflight

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
MANIFEST = "sha256:" + "1" * 64
VMOPTS = ("-Xms1g\n-Xmx4g\n-Dsun.java2d.d3d=false\n-Dsun.java2d.noddraw=true\n"
          "-XX:ErrorFile=C:\\sentinel\\crash\\hs_err_pid%p.log\n-XX:+HeapDumpOnOutOfMemoryError\n"
          "-XX:HeapDumpPath=C:\\sentinel\\crash\n-XX:+CreateCoredumpOnCrash\n")
B3_REF = {"vmoptions_hash": vmoptions_sha256(VMOPTS), "jre_release_sha256": "sha256:" + "2" * 64,
          "java2d_pipeline": "software"}
DESKTOP = np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3)
PINS = PreflightPins(
    snapshot_manifest_sha256=MANIFEST, windows_build={"current_build": "22631", "ubr": "4460"},
    cleartype={"gamma": 2200, "contrast": 1200, "orientation": "rgb"},
    artifacts={"candidate": "sha256:" + "c" * 64, "base": "sha256:" + "b" * 64},
    b3_references={"manifest": B3_REF, "pair": B3_REF}, jdk_major=11, vmoptions_sha256=vmoptions_sha256(VMOPTS),
    n1_hosts=("login.example", "md.example"), model_ids=("claude-reviewer",), dataset_id="ds-2026-11-02",
    corpus={"ws.tier-a.active-trader": "sha256:" + "a" * 64}, catalogs={})

GOOD: dict[str, dict] = {
    "R1": {"manifest_sha256": MANIFEST, "current_build": "22631", "ubr": "4460", "pending_restart": False},
    "R2": {"width": 1920, "height": 1080, "cx": 1920, "cy": 1080, "bits": 32, "frequency": 60, "dpi": 96,
           "monitors": 1, "awareness": "PER_MONITOR_AWARE_V2"},
    "R3": {"smoothing": True, "type": "FE_FONTSMOOTHINGCLEARTYPE", "gamma": 2200, "contrast": 1200,
           "orientation": "rgb"},
    "R4": {"time_zone": "Eastern Standard Time", "layout": "0x0409", "layout_count": 1, "caps": False, "num": True,
           "scroll": False, "locale": "en-US", "decimal": ".", "thousands": ",", "short_date": "M/d/yyyy"},
    "R5": {"sleep_never": True, "display_never_off": True, "hibernate": False, "screensaver": False,
           "lock_screen": False, "lock_window": False, "updates_deferred": True, "auto_restart": False,
           "pending_restart": False, "focus_assist": "alarms_only", "toasts": False},
    "R6": {"input_desktop": "Default", "console": True, "active": True, "rdp": False, "locked": False,
           "foreground_hwnd": 0x1234, "probe": DESKTOP},
    "R7": {"rig_free_bytes": 60 * GB, "shard_bytes": 3 * GB, "control_free_bytes": 600 * GB, "run_bytes": 9 * GB,
           "ntp_offset_s": 0.2},
    "B1": {"artifacts": {"candidate": "sha256:" + "c" * 64, "base": "sha256:" + "b" * 64}},
    "B2": {"auto_update_disabled": True},
    "B3": {"vmoptions_sha256": vmoptions_sha256(VMOPTS), "jre_release_sha256": "sha256:" + "2" * 64,
           "java2d_pinned": "software"},
    "H1": {"click_received": True, "chord_received": True},
    "H2": {"installed": True, "event_received": True, "removed": True, "latencies_ms": [0.2] * 1000,
           "processes": ["svchost.exe", "thinkorswim.exe"]},
    "H3": {"probe": probe_pattern(), "desktop": DESKTOP, "awareness": "PER_MONITOR_AWARE_V2"},
    "H4": {"drops": ["untokened_injected", "blocked_key_dialog"]},
    "H5": {"file_flag": True, "named_pipe": True},
    "H6": {"masked": True, "egress_logged": True},
    "H7": {"db_local": True, "artifacts_local": True, "quick_check": "ok", "wal": True, "lock": True,
           "writable": True, "free_bytes": 60 * GB, "shard_bytes": 3 * GB, "config_commit_ok": True,
           "lint_hash_ok": True},
    "H8": {"watchdog_pid": 4100, "orchestrator_pid": 4000, "watchdog_alive": True, "watchdog_age_s": 1.5,
           "heartbeat_read_back": True},
    "H9": {"labels": {"Last": "Last", "Bid": "Bid"}, "jab_answers": True},
    "H10": {"vmoptions": VMOPTS, "error_dir_writable": True, "dump_dir_writable": True, "wer_localdumps": True},
    "N1": {"hosts": {"login.example": {"reachable": True, "chain_trusted": True},
                     "md.example": {"reachable": True, "chain_trusted": True}}},
    "N2": {"models": {"claude-reviewer": True}, "message_ok": True, "agent_ok": True, "cache_read_tokens": 2048},
    "N3": {"quote_s": 0.8},
    "N4": {"healthy": True, "dataset_id": "ds-2026-11-02"},
    "N5": {"accepted": True, "bytes": 2 * 1024 ** 2},
    "N6": {"reachable": True, "unresolved": []},
    "N7": {"unreachable": []},
    "G1": {"lint_errors": 0, "commit_ok": True, "lint_hash_ok": True, "ruling_ok": True, "templates_ok": True},
    "G2": {"files": {"ws.tier-a.active-trader": "sha256:" + "a" * 64}},
    "G3": {"catalogs": {c: "f" * 64 for c in CATALOGS}},
    "G4": {"unresolved": [], "list_24h_sha256": "sha256:" + "4" * 64},
    "G5": {"result": "pass"},
}


def read(item: str, **changes) -> PreflightRead:
    return PreflightRead(item, {**GOOD[item], **changes})


def ev(item: str, pins: PreflightPins = PINS, **changes):
    return evaluate(item, read(item, **changes), pins)


def test_every_good_read_passes():
    assert set(GOOD) == set(PREFLIGHT_ITEMS)
    for item in PREFLIGHT_ITEMS:
        assert ev(item).passed, (item, ev(item).causes)


# -- R items -----------------------------------------------------------------------------------------------------
def test_r1_manifest_and_build_and_pending_restart():
    r = ev("R1", manifest_sha256="sha256:" + "9" * 64)
    assert not r.passed and r.causes[0].startswith("manifest:")
    assert ev("R1", ubr="4500").causes[0].startswith("windows_build:")
    assert ev("R1", pending_restart=True).causes == ("pending_restart: a restart is pending",)
    assert not ev("R1", PreflightPins()).passed                        # nothing pinned: fails, never passes


def test_r2_dpi_120_fails_and_the_reader_must_be_per_monitor_v2():
    assert any("GetDpiForSystem" in c for c in ev("R2", dpi=120).causes)
    assert any("awareness" in c for c in ev("R2", awareness="SYSTEM_AWARE").causes)
    assert any("GetSystemMetrics" in c for c in ev("R2", cx=1600).causes)


def test_r3_r4_r5():
    assert any("gamma" in c for c in ev("R3", gamma=1800).causes)
    assert not ev("R3", PreflightPins()).passed
    assert any("caps lock" in c for c in ev("R4", caps=True).causes)
    assert any("layout count" in c for c in ev("R4", layout_count=2).causes)
    assert any("decimal" in c for c in ev("R4", decimal=",").causes)
    assert any("pending_restart" in c for c in ev("R5", pending_restart=True).causes)


def test_r6_uniform_probe_and_rdp_fail():
    assert ev("R6", probe=np.zeros((4, 4, 3), np.uint8)).causes == ("the probe screenshot is uniform",)
    assert any("RDP" in c for c in ev("R6", rdp=True).causes)
    assert any("null" in c for c in ev("R6", foreground_hwnd=0).causes)


def test_r7_from_the_floors():
    assert any("30.0 GB" in c for c in ev("R7", rig_free_bytes=25 * GB, shard_bytes=15 * GB).causes)
    assert any("NTP" in c for c in ev("R7", ntp_offset_s=2.1).causes)


# -- B items -----------------------------------------------------------------------------------------------------
def test_b1_b2_b3():
    assert any("base installer" in c for c in ev("B1", artifacts={"candidate": "sha256:" + "c" * 64}).causes)
    assert not ev("B2", auto_update_disabled=False).passed
    branch_b = replace(PINS, install_branch="B", builds=("main-1", "branch-2"))
    assert evaluate("B2", PreflightRead("B2", {"snapshot_build_id": "main-1"}), branch_b).passed
    assert not evaluate("B2", PreflightRead("B2", {"snapshot_build_id": "main-0"}), branch_b).passed
    pins = replace(PINS, b3_references={"manifest": B3_REF, "pair": {**B3_REF, "java2d_pipeline": "d3d"}})
    assert ev("B3", pins).causes == ("java2d_pinned 'software' differs from the pair's 'd3d'",)
    assert not ev("B3", replace(PINS, b3_references={})).passed


# -- H items -----------------------------------------------------------------------------------------------------
def test_h2_latency_recorder_and_sample_count():
    worst = ev("H2", latencies_ms=[0.2] * 999 + [25.0])
    assert any("20 ms hook timeout" in c for c in worst.causes)
    p999 = ev("H2", latencies_ms=[0.2] * 998 + [1.2, 1.2])
    assert any("p99.9 1.20 ms" in c for c in p999.causes)
    assert any("1000 needed" in c for c in ev("H2", latencies_ms=[0.2] * 999).causes)
    assert any("Recorder" in c for c in ev("H2", processes=["Sentinel-Recorder.exe"]).causes)


def test_h3_rejects_a_uniform_desktop_and_a_wrong_pattern():
    assert ev("H3", desktop=np.full((8, 8, 3), 7, np.uint8)).causes == (
        "the desktop probe is uniform (locked or headless console)",)
    bad = probe_pattern().copy()
    bad[0, 0, 0] ^= 1
    assert any("byte for byte" in c for c in ev("H3", probe=bad).causes)
    assert frame_uniform(np.zeros((2, 2), np.uint8)) and not frame_uniform(DESKTOP)


def test_h4_uses_the_real_gateway_and_needs_exactly_two_drops():
    assert h4_expected_drops() == ("blocked_key_dialog", "untokened_injected")
    assert not ev("H4", drops=["blocked_key_dialog"]).passed
    assert not ev("H4", drops=["blocked_key_dialog", "untokened_injected", "no_token"]).passed


def test_h5_h6_h8_h9():
    assert ev("H5", named_pipe=False).causes == ("named_pipe did not round-trip",)
    assert not ev("H6", egress_logged=False).passed
    assert any("Orchestrator's" in c for c in ev("H8", watchdog_pid=4000).causes)
    assert any("watchdog.json" in c for c in ev("H8", watchdog_age_s=12.0).causes)
    assert any("'Bid' read as 'Bld'" in c for c in ev("H9", labels={"Bid": "Bld"}).causes)
    assert not ev("H9", jab_answers=False).passed
    assert ev("H9", replace(PINS, jab_branch="B"), jab_answers=None).passed


def test_h10_names_the_missing_crash_flag_and_checks_the_hash():
    text = VMOPTS.replace("-XX:+CreateCoredumpOnCrash\n", "")
    r = ev("H10", vmoptions=text)
    assert any("-XX:+CreateCoredumpOnCrash is missing" in c for c in r.causes)
    assert any("is not the template's" in c for c in r.causes)
    assert any("WER" in c for c in ev("H10", wer_localdumps=False).causes)


def test_h7_from_a_corrupt_store_aborts_the_run(tmp_path):
    db = tmp_path / "run.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x07" * 4096)
    usage = namedtuple("U", "total used free")(1000 * GB, 0, 1000 * GB)
    sp = storage_preflight(db, tmp_path / "arts", role="rig", checkpoints=100, mean_capture_bytes=MB,
                           disk_usage=lambda _p: usage)
    h7 = merge_reads(sp.as_read("H7"), PreflightRead("H7", {"config_commit_ok": True, "lint_hash_ok": True}))
    r = evaluate("H7", h7, PINS)
    assert not r.passed and any("quick_check" in c for c in r.causes)


def test_h7_passes_on_a_healthy_store(tmp_path):
    usage = namedtuple("U", "total used free")(1000 * GB, 0, 900 * GB)
    with Store(tmp_path / "shard" / "run.db") as _s:
        sp = storage_preflight(tmp_path / "shard" / "run.db", tmp_path / "shard", role="rig", checkpoints=100,
                               mean_capture_bytes=MB, disk_usage=lambda _p: usage)
        h7 = merge_reads(sp.as_read("H7"), PreflightRead("H7", {"config_commit_ok": True, "lint_hash_ok": True}))
        assert evaluate("H7", h7, PINS).passed
    assert not evaluate("H7", sp.as_read("H7"), PINS).passed               # the config checks are required


# -- N and G items -----------------------------------------------------------------------------------------------
def test_n_items():
    assert any("unreachable" in c for c in ev("N1", hosts={"login.example": {"reachable": True,
                                                                             "chain_trusted": True}}).causes)
    assert any("trusted proxy CA" in c for c in ev("N1", hosts={
        "login.example": {"reachable": True, "chain_trusted": False},
        "md.example": {"reachable": True, "chain_trusted": True}}).causes)
    assert any("zero cache read" in c for c in ev("N2", cache_read_tokens=0).causes)
    assert not ev("N3", quote_s=5.0).passed
    assert not ev("N4", dataset_id="ds-other").passed
    pins_b = replace(PINS, data_fixture_branch="B")
    assert evaluate("N4", PreflightRead("N4", {"ondemand_available": True}), pins_b).passed
    assert not evaluate("N4", PreflightRead("N4", {"ondemand_available": False}), pins_b).passed
    assert not ev("N5", bytes=1000).passed
    assert any("vault://x" in c for c in ev("N6", unresolved=["vault://x"]).causes)
    assert ev("N7", unreachable=["jira.example"]).causes == ("jira.example unreachable",)


def test_g_item_reads(tmp_path):
    assert evaluate("G1", g1_read(lint_errors=0, commit_ok=True, lint_hash_ok=True, ruling_ok=True,
                                  templates_ok=True), PINS).passed
    assert not evaluate("G1", g1_read(lint_errors=2, commit_ok=True, lint_hash_ok=True, ruling_ok=True,
                                      templates_ok=True), PINS).passed
    assert any("file missing" in c for c in ev("G2", files={}).causes)
    resolves = {"AAPL": True, "/MES": True, "XYZQ": False}
    g4 = g4_read(resolves, date(2026, 11, 10), lambda s, a, b: resolves[s] and (b - a).days == 90,
                 list_24h_sha256="sha256:" + "4" * 64)
    assert g4.values["horizon_end"] == "2027-02-08"
    assert evaluate("G4", g4, PINS).causes == ("no exchange session resolved for XYZQ through the 90-day horizon",)
    assert not evaluate("G4", replace(g4, values={**g4.values, "unresolved": [], "list_24h_sha256": None}),
                        PINS).passed
    assert evaluate("G5", g5_read("fingerprint_mismatch:display"), PINS).causes == ("fingerprint_mismatch:display",)


def test_g3_versions_change_when_mutants_change(tmp_path):
    for rel in CATALOGS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(EXAMPLE / rel, tmp_path / rel)
    before = catalog_versions(tmp_path)
    assert all(before.values()) and evaluate("G3", g3_read(tmp_path), PINS).passed
    (tmp_path / "canaries" / "mutants.yaml").write_text((EXAMPLE / "canaries" / "mutants.yaml").read_text() + "\n")
    after = catalog_versions(tmp_path)
    assert after["canaries/mutants.yaml"] != before["canaries/mutants.yaml"]
    assert after["popups/catalog.yaml"] == before["popups/catalog.yaml"]
    pinned = replace(PINS, catalogs={"canaries/mutants.yaml": before["canaries/mutants.yaml"]})
    assert not evaluate("G3", g3_read(tmp_path), pinned).passed
    (tmp_path / "quarantine.yaml").unlink()
    assert any("quarantine.yaml missing" in c for c in evaluate("G3", g3_read(tmp_path), PINS).causes)


# -- reads that cannot pass ----------------------------------------------------------------------------------------
def test_unreadable_and_missing_values_fail():
    r = evaluate("R2", PreflightRead("R2", {}, readable=False, detail="EnumDisplaySettings failed"), PINS)
    assert not r.passed and r.causes == ("unreadable: EnumDisplaySettings failed",)
    values = dict(GOOD["R4"])
    del values["scroll"]
    assert evaluate("R4", PreflightRead("R4", values), PINS).causes == ("scroll not read",)
    with pytest.raises(ValueError):
        evaluate("R9", PreflightRead("R9", {}), PINS)
    with pytest.raises(ValueError):
        evaluate("R1", read("R2"), PINS)


def test_a_blocking_control_is_named():
    r = ev("H1", click_received=False, blocking_control="AppLocker", screenshot_sha256="sha256:" + "5" * 64)
    assert r.control == "AppLocker" and "blocked by AppLocker" in r.causes and r.screenshot_sha256


# -- the retry rule and the report ---------------------------------------------------------------------------------
class Script:
    """A read function that serves GOOD, with scripted reads per item and attempt."""

    def __init__(self, scripted: dict[str, list[PreflightRead]] | None = None):
        self.scripted = scripted or {}
        self.calls: list[str] = []

    def __call__(self, item: str) -> PreflightRead:
        self.calls.append(item)
        queue = self.scripted.get(item)
        return queue.pop(0) if queue else read(item)


def test_all_pass_needs_no_wait_and_logs_32_attempts():
    clock = ManualClock()
    t0 = clock.monotonic()
    run = run_preflight(Script(), clock, PINS, run_id="run-01", shard_id="run-01/1")
    assert all(run.report.items.values()) and len(run.attempts) == 32 and clock.monotonic() == t0
    assert run.dpi_awareness == "PER_MONITOR_AWARE_V2" and check_provisioned(run.report).passed
    assert len(run.run_health()) == 32


def test_a_failed_item_retried_once_after_60_s_passes_with_two_attempts():
    clock = ManualClock()
    t0 = clock.monotonic()
    script = Script({"R6": [read("R6", locked=True)]})
    run = run_preflight(script, clock, PINS, run_id="run-01")
    assert run.report.items["R6"] and clock.monotonic() - t0 == 60.0
    r6 = [a for a in run.attempts if a.item == "R6"]
    assert [(a.attempt, a.passed) for a in r6] == [(1, False), (2, True)]
    assert r6[1].at > r6[0].at and "locked" in r6[0].detail
    assert script.calls.count("R6") == 2 and script.calls.count("R5") == 1
    health = {row["item"]: row for row in run.run_health()}
    assert len(health["R6"]["attempts"]) == 2


def test_a_failure_twice_is_env_error_preflight_item():
    script = Script({"R2": [read("R2", dpi=120), read("R2", dpi=120)]})
    run = run_preflight(script, ManualClock(), PINS, run_id="run-01")
    res = check_provisioned(run.report)
    assert not res.passed and res.unreached.gate == "preflight.R2" and res.run.status == "complete_with_env_errors"
    assert "GetDpiForSystem" in run.report.detail["R2"]


def test_a_wrong_dpi_awareness_aborts_with_dpi_awareness():
    bad = read("H3", awareness="SYSTEM_AWARE")
    run = run_preflight(Script({"H3": [bad, bad]}), ManualClock(), PINS, run_id="run-01")
    assert run.dpi_awareness == "SYSTEM_AWARE" and not run.report.items["H3"]
    res = check_provisioned(run.report, dpi_awareness=run.dpi_awareness)
    assert (res.run.status, res.run.reason) == ("aborted_harness", "dpi_awareness")


def test_an_h_failure_reports_only_its_control_for_the_hardening_finding():
    bad = read("H1", click_received=False, blocking_control="CrowdStrike")
    run = run_preflight(Script({"H1": [bad, bad]}), ManualClock(), PINS, run_id="run-01")
    assert run.report.detail == {"H1": "CrowdStrike"}
    res = check_provisioned(run.report)
    assert res.run.reason == "preflight.H1" and res.findings[0].kind == "hardening"
    plain = read("H8", watchdog_alive=False)
    run2 = run_preflight(Script({"H8": [plain, plain]}), ManualClock(), PINS, run_id="run-01")
    assert "H8" not in run2.report.detail and not check_provisioned(run2.report).findings


def test_n2_zero_cache_read_is_the_degraded_mode():
    bad = read("N2", cache_read_tokens=0)
    run = run_preflight(Script({"N2": [bad, bad]}), ManualClock(), PINS, run_id="run-01")
    res = check_provisioned(run.report)
    assert res.passed and res.alert.rule == "preflight_or_cost" and "degraded" in res.detail


def test_g5_mismatch_flows_to_not_comparable_and_g1_to_config_invalid():
    g5 = g5_read("fingerprint_mismatch:cleartype,display")
    run = run_preflight(Script({"G5": [g5, g5]}), ManualClock(), PINS, run_id="run-01")
    res = check_provisioned(run.report)
    assert run.report.detail["G5"] == "fingerprint_mismatch:cleartype,display"
    assert res.outcomes[0].outcome.outcome == "not_comparable" and "cleartype" in res.outcomes[0].outcome.detail
    g1 = read("G1", lint_errors=3)
    run = run_preflight(Script({"G1": [g1, g1]}), ManualClock(), PINS, run_id="run-01")
    assert check_provisioned(run.report).run.reason == "config_invalid"


# -- the simulated rig's reads ---------------------------------------------------------------------------------------
def test_simulated_reads_pass_every_item_against_the_pins_and_play_overrides_in_order():
    sim = SimulatedPreflightReads(PINS, vmoptions=VMOPTS, overrides={"R6": [{"locked": True}]})
    clock = ManualClock()
    run = run_preflight(sim.read, clock, PINS, run_id="run-01")
    assert all(run.report.items.values()) and [a.passed for a in run.attempts if a.item == "R6"] == [False, True]
    assert isinstance(sim, PreflightReads)
    unpinned = run_preflight(SimulatedPreflightReads(PreflightPins()).read, ManualClock(), PreflightPins(),
                             run_id="run-01")
    assert not unpinned.report.items["R1"] and not unpinned.report.items["H10"]   # nothing passes by default


# -- combining the rig's reads with the harness's ------------------------------------------------------------------
def test_combine_takes_g_items_locally_and_merges_r7():
    rig_calls: list[str] = []

    def rig(item: str) -> PreflightRead:
        rig_calls.append(item)
        if item == "R7":
            return PreflightRead("R7", {"rig_free_bytes": 60 * GB, "shard_bytes": 3 * GB, "ntp_offset_s": 0.1})
        return read(item)
    local = {"G5": lambda: g5_read("pass"),
             "R7": lambda: PreflightRead("R7", {"control_free_bytes": 600 * GB, "run_bytes": 9 * GB})}
    fn = combine(rig, local)
    assert fn("G5").values == {"result": "pass"} and "G5" not in rig_calls
    assert evaluate("R7", fn("R7"), PINS).passed and "R7" in rig_calls
    with pytest.raises(ValueError):
        merge_reads(PreflightRead("R7", {"run_bytes": 1}), PreflightRead("R7", {"run_bytes": 2}))
    with pytest.raises(ValueError, match="read on the rig"):
        combine(rig, {"R2": lambda: read("R2")})
    assert not merge_reads(PreflightRead("H7", {}, readable=False, detail="x"), PreflightRead("H7", {})).readable
