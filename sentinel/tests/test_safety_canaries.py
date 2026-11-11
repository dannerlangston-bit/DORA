"""The Nightly safety canaries, the denied-action test and the canary rows of the drill (6.3 to 6.9, 10.3)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sentinel.orchestrator import alerts, attestation, identity, killswitch
from sentinel.orchestrator import safety_canaries as sc
from sentinel.orchestrator import simrig as simrig_module
from sentinel.orchestrator.attestation import AttestationProfile
from sentinel.orchestrator.drill import DrillPreconditions, SimDrillPort, run_drill
from sentinel.orchestrator.explorer import DEFAULT_DENY_LIST, DenyEntry, load_deny_list
from sentinel.orchestrator.gateway import DropLedger, SlowDecision
from sentinel.orchestrator.rig import OrderBook, SelectorRead, SettingRead
from sentinel.orchestrator.simrig import SimulatedRig, default_context
from sentinel.store.safety import disable_rig, refuse_blocked
from sentinel.store.store import RunPlan, ShardPlan, Store
from sentinel.taxonomy import is_checkpoint_id

from test_drill_driver import CHORDS, MemoryChannel, ready

CTX = default_context()
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"


def byte_scan(root: Path, marker: str) -> list[Path]:
    """A reference plaintext scan: every file under `root` whose bytes hold the marker (6.9)."""
    return [p for p in root.rglob("*") if p.is_file() and marker.encode() in p.read_bytes()]


def assert_blind(r: sc.SafetyCanaryResult, name: str) -> None:
    assert r.blind and r.name == name and r.missed
    res = r.result
    assert (res.checkpoint.outcome, res.checkpoint.reason) == ("harness_error", f"safety_canary_blind:{name}")
    assert res.run is None and res.incident == "S2" and res.alert.rule == alerts.drill_failure().rule
    row = r.row
    assert (row.kind, row.defect_class, row.mutant_id, row.money_cell, row.state) == \
        ("safety_canary", "safety_canary", name, True, "detector_blind")
    assert row.produced_outcome == "harness_error" and row.produced_reason == f"safety_canary_blind:{name}"


def assert_passed(r: sc.SafetyCanaryResult) -> None:
    assert r.result.passed and not r.missed and r.cases and r.row.state == "detected" and r.row.produced_outcome is None


# -- each canary against the real readers ----------------------------------------------------------------------
@pytest.mark.parametrize("profile", [CTX.profile, AttestationProfile(shift_click_send="absent"),
                                     AttestationProfile(active_trader_panels=("p1", "p2"),
                                                        non_order_actions=frozenset({sc.CANARY_ORDER_ACTION}))])
def test_attestation_canary_passes_on_flipped_and_unreadable_crops_of_a3_to_a8(profile):
    r = sc.attestation_canary(profile)
    assert_passed(r)
    assert len(r.cases) == 12 and {c.observed for c in r.cases} == {"mismatch", "unreadable"}


def test_attestation_canary_is_blind_when_the_checker_matches_everything(monkeypatch):
    monkeypatch.setattr(attestation, "matches", lambda read, profile: True)
    r = sc.attestation_canary(CTX.profile)
    assert_blind(r, "attestation")
    assert {c.case for c in r.missed} == {f"{i} flipped" for i in attestation.SAFETY_ROWS}
    assert r.result.has("quarantine_all_flows") and "attestation reader is blind" in r.result.detail


def test_attestation_canary_is_blind_when_the_reader_cannot_see_the_flip():
    def blind_reader(item: str, variant: str) -> SettingRead:
        return SettingRead(item, sc.expected_setting(item, CTX.profile))            # reads the expected state anyway
    r = sc.attestation_canary(CTX.profile, read=blind_reader)
    assert_blind(r, "attestation")
    assert {c.observed for c in r.missed} == {"pass"}


def test_identity_canary_takes_the_mismatch_and_unreadable_paths_in_dry_run():
    r = sc.identity_canary(CTX.allowlist)
    assert_passed(r)
    assert [c.observed for c in r.cases] == ["live_identity", "paper_mode_mismatch", "identity_unreadable"]
    assert sc.foreign_paper_id(CTX.allowlist) not in CTX.allowlist


def test_identity_canary_is_blind_when_the_live_pattern_is_lost(monkeypatch):
    monkeypatch.setattr(identity, "classify", lambda text, allowlist, live_re=None:
                        "allowlisted" if text in allowlist else "paper_mode_mismatch")
    r = sc.identity_canary(CTX.allowlist)
    assert_blind(r, "identity")
    assert [(c.case, c.observed) for c in r.missed] == [("live_format", "paper_mode_mismatch")]
    assert r.result.has("quarantine_all_flows")


def test_a_checker_that_raises_is_a_miss_and_never_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "classify", lambda text, allowlist, live_re=None: "allowlisted")
    r = sc.identity_canary(CTX.allowlist)                # the judge then indexes a text that is not on the list
    assert_blind(r, "identity")
    assert {c.case for c in r.missed} == {"live_format", "foreign_paper"}
    assert all(c.observed.startswith("error: ValueError") for c in r.missed)

    def broken(root: Path, marker: str) -> list[Path]:
        raise OSError("scan unavailable")
    scan = sc.secret_scan_canary(broken, tmp_path)
    assert_blind(scan, "secret_scan")
    assert {c.observed for c in scan.missed} == {"error: OSError: scan unavailable"} and len(scan.missed) == 3


def test_identity_canary_is_blind_when_the_reader_reads_text_off_a_blank_crop():
    planted = sc.planted_selector(CTX.allowlist)
    r = sc.identity_canary(CTX.allowlist, read=lambda v: SelectorRead(CTX.allowlist.entries[0])
                           if v == "unreadable" else planted(v))
    assert_blind(r, "identity")
    assert [(c.case, c.observed) for c in r.missed] == [("unreadable", "allowlisted")]


def test_proof_canary_gives_orders_sent_naming_the_row_and_unproven_monitor_unreadable():
    r = sc.proof_canary()
    assert_passed(r)
    assert [c.observed for c in r.cases] == ["orders_sent", "unproven:monitor_unreadable"]


def test_proof_canary_is_blind_when_the_proof_ignores_new_rows(monkeypatch):
    monkeypatch.setattr(killswitch, "_new_rows", lambda start, end: {g: [] for g in killswitch.GRIDS})
    r = sc.proof_canary()
    assert_blind(r, "proof")
    assert [(c.case, c.observed) for c in r.missed] == [("extra_working_row", "proven_no_sends")]


def test_proof_canary_is_blind_when_the_monitor_reader_never_reports_unreadable():
    r = sc.proof_canary(read=lambda variant, start: sc.planted_book(variant, start) if variant != "unreadable"
                        else start)
    assert_blind(r, "proof")
    assert r.missed[0].observed == "proven_no_sends"


def test_proof_canary_requires_the_abort_to_name_the_row(monkeypatch):
    real = killswitch.proof_result
    monkeypatch.setattr(sc, "proof_result", lambda proof: replace(real(proof), detail="ORDERS SENT"))
    r = sc.proof_canary()
    assert_blind(r, "proof")
    assert r.missed[0].observed == "orders_sent without aborted_safety naming the row"


def test_secret_scan_canary_flags_a_log_line_a_sqlite_row_and_a_transcript(tmp_path):
    r = sc.secret_scan_canary(byte_scan, tmp_path)
    assert_passed(r)
    assert [c.case for c in r.cases] == ["log_line", "sqlite_row", "transcript"]
    again = sc.secret_scan_canary(lambda root, m: [str(p.relative_to(root)) for p in byte_scan(root, m)], tmp_path)
    assert again.result.passed                          # relative paths too; a second dry run replants cleanly


def test_secret_scan_canary_is_blind_when_the_scan_misses_sqlite_and_withholds_the_report(tmp_path):
    r = sc.secret_scan_canary(lambda root, m: [p for p in byte_scan(root, m) if p.suffix != ".sqlite"], tmp_path)
    assert_blind(r, "secret_scan")
    assert [c.case for c in r.missed] == ["sqlite_row"] and r.result.actions == ("withhold_report",)
    assert "security_contact" in r.result.alert.notified


# -- the denied-action test (6.8) ------------------------------------------------------------------------------
def explorer_rig(scenario: simrig_module.SimScenario | None = None) -> SimulatedRig:
    """The Explorer login's rig with the ticket and dialog flag clear (two clear Watcher polls, 5.4.6)."""
    rig = SimulatedRig(scenario)
    rig.launch()
    return rig


def test_denied_action_test_passes_against_the_default_deny_list():
    r = sc.denied_action_test(explorer_rig(), DEFAULT_DENY_LIST)
    assert_passed(r)
    assert {c.case.split()[0] for c in r.cases} == {e.name for e in DEFAULT_DENY_LIST}


def test_denied_action_test_drives_the_example_file_and_every_deny_control():
    entries, registry = load_deny_list(EXAMPLE)
    rig = explorer_rig(simrig_module.SimScenario(config_root=EXAMPLE))     # the rig enforces the file itself
    assert rig.deny_list == entries
    r = sc.denied_action_test(rig, entries, deny_controls=registry.deny_controls)
    assert_passed(r)
    assert "control:charts.flexible-grid.active-trader-cell.buy-mkt-button control" in {c.case for c in r.cases}


def test_removing_an_entry_from_the_enforced_list_makes_the_test_blind():
    enforced = tuple(e for e in DEFAULT_DENY_LIST if e.name != "chat")
    r = sc.denied_action_test(explorer_rig(simrig_module.SimScenario(deny_list=enforced)), DEFAULT_DENY_LIST)
    assert_blind(r, "deny_list")
    assert {c.case for c in r.missed} == {"chat title", "chat label"} and {c.observed for c in r.missed} == {"allowed"}
    assert r.result.outcomes[0].selector == "explorer_stage"
    assert (r.result.outcomes[0].outcome.outcome, r.result.outcomes[0].outcome.reason) == ("not_run", "quarantine")
    assert "explorer_owner" in r.result.alert.notified


def test_probes_refused_only_because_the_flag_is_up_did_not_test_the_deny_list():
    rig = explorer_rig()
    rig.ticket_visible = True
    r = sc.denied_action_test(rig, DEFAULT_DENY_LIST[:1])
    assert_blind(r, "deny_list")
    assert {c.observed for c in r.missed} == {"not driven: flag up"}
    dead = explorer_rig()
    dead.gw.hook_host_alive = False
    assert {c.observed for c in sc.denied_action_test(dead, DEFAULT_DENY_LIST[:1]).missed} == \
        {"not driven: hook host dead"}


def test_an_entry_the_plan_cannot_drive_is_a_miss():
    r = sc.denied_action_test(explorer_rig(), (DenyEntry("odd", label="[^a]"),))
    assert_blind(r, "deny_list")
    assert [(c.case, c.observed) for c in r.missed] == [("odd (no probe)", "not driven")]


# -- scoring, banner and rows ----------------------------------------------------------------------------------
def test_score_canary_needs_a_case_and_a_known_name():
    assert sc.score_canary("proof", []).blind
    with pytest.raises(ValueError):
        sc.score_canary("redaction", [])                 # M19 is canary.py's


def test_banner_and_detector_state():
    ok = sc.proof_canary()
    blind = sc.score_canary("identity", [sc.CanaryCase("live_format", "live_identity", "allowlisted")])
    assert sc.banner([ok]) is None and sc.detector_state([ok]) == "ok"
    text = sc.banner([ok, blind])
    assert sc.detector_state([ok, blind]) == "detector_blind"
    assert text.startswith("DETECTOR BLIND") and "identity check is blind" in text and "live_format" in text
    assert "Every flow on the rig is not_run (reason: quarantine)" in text and text.endswith("RC sign-off is blocked.")


def test_checkpoint_ids_follow_the_grammar():
    assert is_checkpoint_id(sc.DRILL_CHECKPOINT)
    assert all(is_checkpoint_id(sc.checkpoint_for(n)) for n in sc.CANARIES)


def drill_result(rig=None, pre=DrillPreconditions(True, 1.0, True)):
    rig = rig or ready(SimulatedRig())
    return run_drill(rig, DropLedger(), rig.clock, CHORDS, rig.read_orders(), MemoryChannel(), port=SimDrillPort(rig),
                     preconditions=pre, allowlist=CTX.allowlist, rig_id="rig-01", fixture_account=CTX.fixture_account)


def test_drill_rows_one_per_item():
    rows = sc.drill_rows(drill_result(), CHORDS)
    assert [r.mutant_id for r in rows] == [f"drill-{n}" for n in range(1, 6)]
    assert {(r.kind, r.defect_class, r.money_cell, r.state) for r in rows} == {("fire_drill", "fire_drill", False,
                                                                               "detected")}
    assert "raw dropped blocked_chord_dialog x2" in rows[2].expected and len(rows[4].expected) == 6


def test_drill_rows_for_a_failed_item_a_precondition_and_a_sent_order(monkeypatch):
    item5 = sc.drill_rows(drill_result(ready(SimulatedRig(simrig_module.SimScenario(cancel_all_located=False)))),
                          CHORDS)
    assert [r.state for r in item5] == ["detected"] * 4 + ["missed"]
    assert item5[4].produced_reason == "drill_failed:item5" and "Cancel All not located" in item5[4].absorbed_by
    pre = sc.drill_rows(drill_result(pre=DrillPreconditions(True, None, True)), CHORDS)
    assert {r.produced_reason for r in pre} == {"drill_failed:hook_open"}
    rig = ready(SimulatedRig())
    rig.attested = False
    item1 = sc.drill_rows(drill_result(rig), CHORDS)[0]
    assert item1.state == "missed" and "did not open" in item1.absorbed_by
    real = simrig_module.slow_half
    monkeypatch.setattr(simrig_module, "slow_half", lambda a, ctx: SlowDecision(False) if a.keys
                        else real(a, ctx))
    rig = ready(SimulatedRig())
    rig.gw.blocked_vk = frozenset()
    sent = sc.drill_rows(drill_result(rig), CHORDS)
    assert {(r.state, r.produced_outcome, r.produced_reason) for r in sent} == {("missed", "env_error",
                                                                                 "drill_order_placed")}
    with pytest.raises(ValueError):
        sc.drill_rows(replace(drill_result(), observation=None), CHORDS)


def test_canary_and_drill_rows_satisfy_the_8_3_checks(tmp_path):
    store = Store(tmp_path / "central.db")
    store.register_rig("rig-05", login_alias="login-05", owner="ops", snapshot_id="snap-5", allowlist_entry=1,
                       role="nightly")
    store.begin_run(RunPlan(run_id="nightly-1", run_type="nightly", config_commit="c", config_tarball_sha256="t",
                            snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60,
                            scope=(), shards=(ShardPlan("nightly-1/1", "rig-05"),)))
    blind = sc.score_canary("deny_list", [sc.CanaryCase("chat title", "blocked_by_gateway", "allowed")])
    rows = [sc.attestation_canary(CTX.profile).row, blind.row,
            *sc.drill_rows(drill_result(ready(SimulatedRig(simrig_module.SimScenario(cancel_all_located=False)))),
                           CHORDS)]
    store.write_canary_results("nightly-1", rows)
    stored = store.canary_results("nightly-1")
    assert [r.mutant_id for r in stored] == ["attestation", "deny_list", *(f"drill-{n}" for n in range(1, 6))]
    assert stored[1].state == "detector_blind" and stored[-1].state == "missed"
    store.close()


def test_a_rig_refresh_never_puts_back_a_rig_a_safety_action_disabled(tmp_path):
    """`register_rig`'s upsert keeps `active` of a rig `disable_rig` took out (6.6 step 6, 6.12): only a human
    re-enable brings it back."""
    store = Store(tmp_path / "central.db")
    kw = dict(login_alias="login-05", owner="ops", allowlist_entry=1, role="nightly")
    store.register_rig("rig-05", snapshot_id="snap-5", **kw)
    assert disable_rig(store, "rig-05", "kill_switch")
    store.register_rig("rig-05", snapshot_id="snap-6", **kw)                   # a re-snapshot refreshes the row
    row = store.query("SELECT active, snapshot_id, disabled_reason FROM rigs WHERE rig_id = 'rig-05'")[0]
    assert tuple(row) == (0, "snap-6", "kill_switch")
    assert refuse_blocked(store, "rig-05").why == "rig_disabled"
    store.register_rig("rig-06", snapshot_id="snap-1", login_alias="login-06", owner="ops", allowlist_entry=2)
    store.register_rig("rig-06", snapshot_id="snap-2", login_alias="login-06", owner="ops", allowlist_entry=2)
    assert store.query("SELECT active FROM rigs WHERE rig_id = 'rig-06'")[0][0] == 1 and \
        refuse_blocked(store, "rig-06") is None
    store.close()


def test_a_reader_unreadable_on_both_reads_is_unreadable_not_mismatch():
    r = sc.attestation_canary(CTX.profile, read=lambda item, v: SettingRead(item, None, landmark_score=0.0))
    assert {c.case for c in r.missed} == {f"{i} flipped" for i in attestation.SAFETY_ROWS}
    assert {c.observed for c in r.missed} == {"unreadable"}


def test_the_proof_canary_plants_into_the_given_start_snapshot():
    book = OrderBook(position_rows=3, net_liq=100.0)
    seen: list[OrderBook] = []
    r = sc.proof_canary(book, read=lambda variant, start: seen.append(start) or sc.planted_book(variant, start))
    assert r.result.passed and seen == [book, book]
