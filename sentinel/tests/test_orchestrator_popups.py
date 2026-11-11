"""Tests for the popup sentinel (4.10, 7.8, 12.8, 5.9, 2.7 INV-14), table-driven from the example catalog."""
import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from sentinel.invariants import CheckerConfig, Rect as InvRect, ScreenModel, SurfaceRules, UIComponent, Window, check
from sentinel.orchestrator.budgets import ReloginBudget
from sentinel.orchestrator.gateway import VK_ESCAPE
from sentinel.orchestrator.ports import Frame, WindowInfo
from sentinel.orchestrator.popups import (
    AGENT_DISMISS_LABELS, BuildCounts, CatalogError, PopupContext, PopupCounter, PopupSentinel, TemplateMatcher, decide,
    dismiss_action, error_title_patterns, foreground_check, load_catalog, order_dialog_regexes, order_event_regexes,
    parse_catalog, persisting, popup_event, popup_rate_findings, unexpected_windows, unknown_window, wait_expired,
)
from sentinel.orchestrator.windows import TitleSets, classify_windows

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "example" / "popups" / "catalog.yaml"
RAW = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
CAT = load_catalog(CATALOG_PATH)
RUN = PopupContext(phase="running", gate="running", flow_id="flow.watchlist.add", step_id="s2")


def win(title, hwnd=500, cls="SunAwtDialog", pid=4100, **kw):
    return WindowInfo(hwnd, pid, cls, title, **kw)


def entry(pid, catalog=CAT):
    return next(e for e in catalog.entries if e.popup_id == pid)


def variant(pid, **changes):
    """The example catalog with one entry changed (e.g. POP-12 enabled)."""
    raw = copy.deepcopy(RAW)
    next(e for e in raw if e["popup_id"] == pid).update(changes)
    return parse_catalog(raw)


def one(**fields):
    base = {"popup_id": "POP-30", "name": "popup.test", "class": "dismissable", "title_regex": "^Test",
            "action": "dismiss_key", "key": "escape", "severity": "info", "expected_phases": ["running"],
            "owner": "sentinel-ops", "status": "verified", "first_seen_build": "b1"}
    base.update(fields)
    return base


# -- the catalog (4.10, SD-47) ------------------------------------------------------------------------------------

def test_both_catalog_forms_parse_to_the_same_entries():
    assert CAT.bare_list and len(CAT.entries) == 22
    wrapped = parse_catalog({"schema_version": 1, "entries": RAW})
    assert not wrapped.bare_list and wrapped.schema_version == 1
    assert wrapped.entries == CAT.entries


@pytest.mark.parametrize("bad, words", [
    (one(**{"class": "nonsense"}), "class"),
    (one(action="close"), "action"),
    (one(key="enter"), "L-POP-02"),
    (one(key="space"), "L-POP-02"),
    (one(key="ctrl+enter"), "one named key"),
    (one(key="alt+f4"), "one named key"),
    (one(**{"class": "safety"}), "never emits input"),
    (one(**{"class": "app_failure"}, action="click", target="x"), "never emits input"),
    (one(action="abort", key=None), "requires on_abort"),
    (one(action="wait", key=None, on_abort={"outcome": "env_error", "gate": "running", "reason": "timeout"}),
     "wait_timeout_ms"),
    (one(action="click", key=None), "requires target"),
    (one(title_regex="(unclosed"), "does not compile"),
    (one(action="abort", key=None, on_abort={"outcome": "env_error", "gate": "running", "reason": "made_up"}),
     "env_error reason"),
    (one(action="abort", key=None, on_abort={"outcome": "aborted_run", "run_outcome": "aborted_harness"}),
     "needs a reason"),
    (one(expected_phases=["lunch"]), "expected_phases"),
    (one(popup_id="P-1"), "POP-nn"),
])
def test_unsafe_or_malformed_entries_are_refused(bad, words):
    with pytest.raises(CatalogError, match=words):
        parse_catalog([bad])


def test_repeated_ids_and_a_wrapper_without_version_are_refused():
    with pytest.raises(CatalogError, match="repeat"):
        parse_catalog([one(), one(name="popup.other")])
    with pytest.raises(CatalogError, match="schema_version"):
        parse_catalog({"entries": [one()]})


# -- matching (choices 1, 2) --------------------------------------------------------------------------------------

@pytest.mark.parametrize("title, phase, expected", [
    ("What's New in thinkorswim", "login", "POP-02"),
    ("Updating thinkorswim", "launch", "POP-01"),
    ("Message Center", "running", "POP-06"),
    ("Java Exception", "running", "POP-10"),
    ("Low Memory", "running", "POP-11"),
    ("Order Confirmation", "workspace_load", "POP-14"),     # no-input classes match in every phase
    ("Two-step verification", "running", "POP-21"),         # a relogin entry emits no input: any phase
    ("Trade Flash", "running", None),                       # POP-17 is disabled
    ("OnDemand prebuffering", "running", None),             # POP-12 is disabled in the example
    ("What's New in thinkorswim", "running", None),         # a dismiss entry outside its phases is unmatched
    ("Charts", "running", None),
])
def test_match_from_the_example_catalog(title, phase, expected):
    e = CAT.match(win(title), phase)
    assert (e.popup_id if e else None) == expected


def test_retired_entries_never_match_and_phase_is_checked():
    cat = variant("POP-06", retired=True, retired_reason="gone")
    assert cat.match(win("Message Center"), "running") is None
    with pytest.raises(ValueError):
        CAT.match(win("Message Center"), "lunch")


def test_safety_classes_win_over_earlier_dismiss_entries():
    cat = parse_catalog([one(title_regex="(?i)error"),
                         one(popup_id="POP-31", name="popup.err", **{"class": "app_failure"}, title_regex="(?i)^error",
                             action="abort", key=None, on_abort={"outcome": "invariant_violation",
                                                                 "invariant_id": "INV-14"})])
    assert cat.match(win("Error loading study"), "running").popup_id == "POP-31"


def test_template_half_of_the_match():
    cat = parse_catalog([one(template="popups/templates/t.png"), one(popup_id="POP-31", name="popup.b",
                                                                      title_regex="^Other")])
    assert cat.match(win("Test dialog"), "running", template_ok=lambda e, w: False) is None
    assert cat.match(win("Test dialog"), "running", template_ok=lambda e, w: True).popup_id == "POP-30"
    assert cat.match(win("Other"), "running", template_ok=lambda e, w: False).popup_id == "POP-31"   # no template
    assert cat.match(win("Test dialog"), "running").popup_id == "POP-30"                             # title decides


def test_template_matcher_searches_inside_the_window(tmp_path):
    rng = np.random.default_rng(7)
    tpl = rng.integers(0, 255, (20, 30), dtype=np.uint8)
    (tmp_path / "popups" / "templates").mkdir(parents=True)
    cv2.imwrite(str(tmp_path / "popups" / "templates" / "t.png"), tpl)
    img = np.full((300, 400, 3), 128, np.uint8)
    img[110:130, 210:240] = tpl[..., None]
    frame = Frame(img, (100, 0), 0.0)
    e = parse_catalog([one(template="popups/templates/t.png")]).entries[0]
    match = TemplateMatcher(tmp_path, frame)
    assert match(e, win("Test", rect=(300, 100, 100, 60)))           # covers screen x 300..400: the template at 310
    assert not match(e, win("Test", rect=(100, 0, 100, 60)))
    assert match(e, win("Test"))                                      # no bounds: the whole grab
    with pytest.raises(FileNotFoundError):
        TemplateMatcher(tmp_path, frame)(parse_catalog([one(template="missing.png")]).entries[0], win("Test"))


# -- which windows are popups (7.8 policy 1-2) --------------------------------------------------------------------

def test_unexpected_windows_skip_the_frame_target_detached_and_transient_windows():
    ws = [win("Main@thinkorswim", hwnd=1, cls="SunAwtFrame"), win("Order Entry", hwnd=2),
          win("", hwnd=3, cls="SunAwtWindow", owner_hwnd=1), win("", hwnd=4, cls="SunAwtWindow"),
          win("Message Center", hwnd=5), win("Hidden", hwnd=6, visible=False)]
    out = unexpected_windows(ws, expected_hwnds={1}, expected_titles=["^Order Entry$"])
    assert [w.hwnd for w in out] == [4, 5]


# -- the fixed action effects (4.10, 7.8, 12.8) -------------------------------------------------------------------

def test_dismissable_is_dismissed_with_its_key_and_counted():
    sentinel = PopupSentinel(CAT)
    ctx = PopupContext(phase="login", gate="authenticated")
    (hit,) = sentinel.sweep([win("What's New")], ctx)
    assert hit.new and hit.result.passed and hit.entry.popup_id == "POP-02"
    assert hit.result.evidence["input"] == {"kind": "key", "key": "escape"}
    assert dismiss_action(hit.entry).kind == "press" and dismiss_action(hit.entry).keys == (VK_ESCAPE,)
    assert sentinel.counter.counts["POP-02"] == 1
    with pytest.raises(ValueError):
        dismiss_action(entry("POP-04"))


def test_click_entries_name_their_target():
    cat = parse_catalog([one(action="click", key=None, target="shell.dialogs.whats-new.close",
                             template="popups/templates/w.png")])
    r = decide(cat.entries[0], RUN, win("Test"))
    assert r.passed and r.evidence["input"] == {"kind": "click", "target": "shell.dialogs.whats-new.close"}


def test_env_abort_ends_the_shard_with_the_named_gate():
    r = decide(entry("POP-04"), RUN, win("You are logged in from another location"))
    assert r.run.status == "complete_with_env_errors" and r.step == "failed"
    assert (r.checkpoint.outcome, r.checkpoint.gate, r.checkpoint.reason) == ("env_error", "authenticated",
                                                                              "session_kicked")
    assert r.unreached == r.checkpoint and r.has("teardown") and r.evidence["close"] == "wm_close"
    assert "accounts_owner" in r.alert.notified
    m = decide(entry("POP-09"), RUN, win("Scheduled maintenance"))
    assert (m.checkpoint.gate, m.checkpoint.reason) == ("connectivity", "maintenance")


def test_launcher_update_is_version_mismatch_then_version_changed_after_a_relaunch():
    launch = PopupContext(phase="launch", gate="launched")
    assert decide(entry("POP-01"), launch).checkpoint.reason == "version_mismatch"
    again = PopupContext(phase="launch", gate="launched", relaunched=True)
    assert decide(entry("POP-01"), again).checkpoint.reason == "version_changed_mid_run"


def test_app_error_dialog_is_inv14_never_dismissed():
    r = decide(entry("POP-10"), RUN, win("Java Exception"))
    assert r.checkpoint.outcome == "invariant_violation" and r.checkpoint.reason == "INV-14"
    assert r.evidence["input"] is None and r.evidence["capture_first"] and r.evidence["close"] == "wm_close"
    (rest,) = r.outcomes
    assert rest.selector == "rest_of_flow" and rest.outcome.reason == "step_failed:s2"
    assert r.step == "failed" and r.run is None


def test_flow_level_env_abort_for_other_classes():
    cat = parse_catalog([one(action="abort", key=None, on_abort={"outcome": "env_error", "reason": "timeout"})])
    r = decide(cat.entries[0], RUN, win("Test"))
    assert r.run is None and r.unreached is None
    assert (r.checkpoint.gate, r.checkpoint.reason) == ("running", "timeout")      # the gate in force
    assert r.outcomes[0].outcome.reason == "step_failed:s2" and r.has("end_flow")


def test_aborted_harness_ends_the_run():
    cat = parse_catalog([one(action="abort", key=None,
                             on_abort={"outcome": "aborted_run", "run_outcome": "aborted_harness",
                                       "reason": "start_failed"})])
    r = decide(cat.entries[0], RUN, win("Test"))
    assert (r.run.status, r.run.reason) == ("aborted_harness", "start_failed")
    assert (r.unreached.outcome, r.unreached.reason) == ("harness_error", "start_failed")


def test_unowned_order_confirmation_gets_no_input_and_the_kill_switch_after_30_s():
    sentinel = PopupSentinel(CAT)
    w = win("Order Confirmation")
    for _ in range(2):                                   # seen again: decided again, counted once, never input
        (hit,) = sentinel.sweep([w], RUN)
        assert hit.result.evidence["input"] is None and hit.result.evidence["close"] is None
        assert hit.result.evidence["wait_timeout_ms"] == 30000 and hit.result.run is None
    assert sentinel.counter.counts["POP-14"] == 1
    r = sentinel.wait_over(hit, RUN, cleared=False)
    assert (r.run.status, r.run.reason) == ("aborted_safety", "kill_switch")
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "running", "kill_switch")
    assert r.has("kill_switch") and r.has("quarantine_flow") and r.incident == "S2"
    assert r.evidence["trigger"] == "dialog_open_30s" and r.evidence["input"] is None
    assert sentinel.wait_over(hit, RUN, cleared=True) is None


def test_order_event_is_the_kill_switch_at_once():
    r = decide(entry("POP-22"), RUN, win("Order filled"))
    assert (r.run.status, r.run.reason) == ("aborted_safety", "kill_switch")
    assert r.evidence["close"] is None and r.evidence["trigger"] == "order_event_window"


def test_ondemand_waits_then_times_out_and_three_in_a_row_abandon_it():
    cat = variant("POP-12", status="verified")
    e = entry("POP-12", cat)
    setup = PopupContext(phase="workspace_load", gate="fixture_verified", flow_id="flow.chart", step_id="ondemand")
    r = decide(e, setup, win("OnDemand prebuffering"))
    assert r.passed and r.evidence["wait_timeout_ms"] == 90000
    first = wait_expired(e, setup)
    assert (first.checkpoint.gate, first.checkpoint.reason) == ("fixture_verified", "ondemand_timeout")
    assert [o.selector for o in first.outcomes] == ["rest_of_flow"] and first.run is None
    sentinel = PopupSentinel(cat)
    (hit,) = sentinel.sweep([win("OnDemand prebuffering")], setup)
    sentinel.wait_over(hit, setup, cleared=False)
    sentinel.wait_over(hit, setup, cleared=True)          # a success resets the streak
    assert sentinel.ondemand_timeouts == 0
    results = [sentinel.wait_over(hit, setup, cleared=False) for _ in range(3)]
    assert [r.evidence["ondemand_timeouts"] for r in results] == [1, 2, 3]
    frozen = [o for o in results[2].outcomes if o.selector == "frozen"]
    assert frozen and frozen[0].outcome.reason == "ondemand_timeout"
    assert not any(o.selector == "frozen" for o in results[1].outcomes)
    mid = decide(e, PopupContext(at_capture=True))
    assert (mid.checkpoint.outcome, mid.checkpoint.reason) == ("not_settled", "anti_anchor_present")


def test_session_expired_draws_on_the_relogin_budget_k():
    budget = ReloginBudget()
    ctx = PopupContext(relogin_budget=budget)
    results = [decide(entry("POP-05"), ctx, win("Session expired")) for _ in range(4)]
    assert all(r.passed and r.has("relaunch") for r in results[:3]) and budget.used == 3
    assert (results[3].run.status, results[3].run.reason) == ("complete_with_env_errors", "relogin_budget")
    assert results[3].unreached.reason == "relogin_budget" and results[3].evidence["popup_id"] == "POP-05"
    with pytest.raises(ValueError, match="re-login budget"):
        decide(entry("POP-05"), RUN)


def test_challenge_prompt_recovers_once_inside_login_only():
    login = PopupContext(phase="login", gate="authenticated")
    sentinel = PopupSentinel(CAT)
    (hit,) = sentinel.sweep([win("Enter verification code")], login)
    assert hit.result.passed and hit.result.evidence["handoff"] == "login_recovery"
    assert sentinel.challenge_recoveries == 1
    (during,) = sentinel.sweep([win("Enter verification code")], login)   # the recovery is still running
    assert not during.new and during.result is hit.result and sentinel.challenge_recoveries == 1
    sentinel.sweep([], login)
    (again,) = sentinel.sweep([win("Enter verification code", hwnd=501)], login)
    r = again.result
    assert (r.checkpoint.gate, r.checkpoint.reason) == ("authenticated", "authentication_challenge")
    assert r.run.status == "complete_with_env_errors" and {"login_owner", "accounts_owner"} <= set(r.alert.paged)
    assert decide(entry("POP-21"), RUN).checkpoint.reason == "authentication_challenge"   # anywhere else


def test_low_memory_is_a_memory_warning_and_a_proactive_relaunch():
    r = decide(entry("POP-11"), PopupContext(memory={"private_bytes": 3_000_000_000, "gdi_objects": 9100}),
               win("Low memory"))
    assert r.passed and r.evidence["proactive_relaunch"]
    (f,) = r.findings
    assert f.kind == "memory_warning" and json.loads(f.detail)["gdi_objects"] == 9100


def test_fixture_findings_unless_the_flow_expects_the_popup():
    assert decide(entry("POP-07"), RUN).evidence["fixture_finding"] == "fixture_drift"
    assert decide(entry("POP-15"), RUN).evidence["fixture_finding"] == "fixture_maintenance_needed"
    expects = PopupContext(flow_expects=frozenset({"POP-15"}))
    assert "fixture_finding" not in decide(entry("POP-15"), expects).evidence
    assert "fixture_finding" not in decide(entry("POP-06"), RUN).evidence


def test_os_window_over_the_frame_is_occlusion_with_a_hardening_finding():
    r = decide(entry("POP-19"), RUN, win("Windows Update", pid=999))
    assert (r.checkpoint.gate, r.checkpoint.reason) == ("running", "occluded") and r.run is None
    assert r.evidence["emit"] is False and r.evidence["close"] is None
    assert [f.kind for f in r.findings] == ["hardening"]
    early = decide(entry("POP-19"), PopupContext(gate="authenticated"), win("Windows Update", pid=999))
    assert early.checkpoint.gate == "authenticated"


def test_ignore_is_never_counted_or_closed():
    cat = parse_catalog([one(action="ignore", key=None, title_regex="^Detached chart")])
    sentinel = PopupSentinel(cat)
    for _ in range(2):
        (hit,) = sentinel.sweep([win("Detached chart")], RUN)
        assert hit.result.passed and hit.result.evidence["close"] is None
    assert not sentinel.counter.counts


# -- unknown and persisting windows (4.10, 7.8 policy 5) ----------------------------------------------------------

def test_unknown_window_under_order_stage_aborts_safety():
    ctx = PopupContext(flow_id="flow.stock.buy-limit", step_id="stage", order_stage=True, build_id="b2")
    r = unknown_window(win("Margin notice"), ctx, CAT)
    assert (r.run.status, r.run.reason) == ("aborted_safety", "unknown_dialog_in_order_stage")
    assert (r.unreached.gate, r.unreached.reason) == ("running", "unknown_dialog_in_order_stage")
    assert r.has("kill_switch") and r.evidence["close"] is None
    (f,) = r.findings
    assert f.kind == "unknown_popup" and json.loads(f.detail)["class"] == "safety"


def test_unknown_window_is_quarantined_and_dismissed_by_the_agent_or_wm_close():
    r = unknown_window(win("Rate this app"), PopupContext(flow_id="flow.x", build_id="main-2000.1.0-aaaaaaa"), CAT)
    assert r.has("quarantine_flow") and r.run is None and r.evidence["close"] == "agent_dismiss"
    assert r.evidence["dismiss_labels"] == list(AGENT_DISMISS_LABELS)
    assert {"Accept", "Confirm", "Send", "Yes", "Save"} <= set(r.evidence["refused_labels"])
    draft = json.loads(r.findings[0].detail)
    assert draft["popup_id"] == "POP-23" and draft["class"] == "dismissable"
    assert draft["seed_titles"] == ["Rate this app"]
    assert draft["first_seen_build"] == "main-2000.1.0-aaaaaaa" and draft["name"] == "popup.unknown-rate-this-app"
    parsed = parse_catalog([draft]).entries[0]                    # the draft is a catalog entry
    assert parsed.pattern.search("Rate this app") and not parsed.pattern.search("Rate this app now")
    off = unknown_window(win("Rate this app"), PopupContext(flow_id="flow.x", agent_enabled=False), CAT)
    assert off.evidence["close"] == "wm_close"
    outside = unknown_window(win("Rate this app"), PopupContext(phase="launch", gate="launched"), CAT)
    assert outside.actions == () and outside.alert is None        # no flow to quarantine


def test_unmatched_order_dialog_title_is_never_handed_to_the_agent_or_closed():
    cat = variant("POP-14", status="disabled")
    assert cat.match(win("Order Confirmation"), "running") is None
    r = unknown_window(win("Order Confirmation"), RUN, cat)
    assert r.evidence["close"] is None and "dismiss_labels" not in r.evidence and "refused_labels" not in r.evidence
    assert json.loads(r.findings[0].detail)["class"] == "safety"


def test_a_persisting_window_fails_the_step_or_is_not_settled_at_a_census():
    sentinel = PopupSentinel(CAT)
    w = win("Rate this app")
    (first,) = sentinel.sweep([w], RUN)
    (second,) = sentinel.sweep([w], RUN)
    assert first.new and not second.new
    r = second.result
    assert r.step == "failed" and r.has("reset_input_state") and r.has("end_flow") and not r.findings
    assert r.outcomes[0].outcome.reason == "step_failed:s2" and r.checkpoint.reason == "step_failed:s2"
    census = persisting(w, PopupContext(census=True))
    assert (census.checkpoint.outcome, census.checkpoint.reason) == ("not_settled", "unexpected_window:Rate this app")
    assert persisting(win(""), PopupContext(census=True)).checkpoint.reason == "unexpected_window:untitled"
    (dismissed,) = sentinel.sweep([win("Message Center", hwnd=9)], RUN)
    (still,) = sentinel.sweep([win("Message Center", hwnd=9)], RUN)
    assert dismissed.result.passed and still.result.step == "failed"


def test_a_window_that_leaves_and_comes_back_is_a_new_hit_and_unknown_titles_keep_their_draft_id():
    sentinel = PopupSentinel(CAT)
    sentinel.sweep([win("Message Center")], RUN)
    sentinel.sweep([], RUN)
    (back,) = sentinel.sweep([win("Message Center")], RUN)
    assert back.new and sentinel.counter.counts["POP-06"] == 2
    (a,) = sentinel.sweep([win("Rate this app", hwnd=7)], RUN)
    (b,) = sentinel.sweep([win("Weekly webinar", hwnd=8)], RUN)
    (c,) = sentinel.sweep([win("Rate this app", hwnd=10)], RUN)
    ids = [h.result.evidence["proposed_entry"]["popup_id"] for h in (a, b, c)]
    assert ids == ["POP-23", "POP-24", "POP-23"]
    loading = PopupSentinel(CAT)
    (first,) = loading.sweep([win("Loading 10%", hwnd=11)], RUN)
    (retitled,) = loading.sweep([win("Loading 20%", hwnd=11)], RUN)    # one appearance that retitled itself
    assert first.new and not retitled.new and sum(loading.counter.counts.values()) == 1


# -- foreground occlusion before an input batch (7.8 policy 1) ----------------------------------------------------

def test_foreign_foreground_blocks_the_batch():
    tree = {4100, 4101}
    assert foreground_check(win("Main", pid=4100), tree, RUN) is None
    r = foreground_check(win("Security alert", pid=77, process_name="defender.exe"), tree, RUN)
    assert (r.checkpoint.gate, r.checkpoint.reason) == ("running", "occluded") and r.evidence["emit"] is False
    assert "pid 77" in r.checkpoint.detail and r.findings[0].kind == "hardening"
    assert foreground_check(None, tree, RUN).checkpoint.reason == "occluded"
    before = foreground_check(win("Toast", pid=77), tree, PopupContext(phase="login", gate="authenticated"))
    assert before.checkpoint.gate == "authenticated"
    sentinel = PopupSentinel(CAT)
    first = sentinel.foreground(win("Security alert", pid=77, process_name="defender.exe"), tree, RUN)
    again = sentinel.foreground(win("Security alert", pid=78, process_name="defender.exe"), tree, RUN)
    assert first.alert is not None and again.alert is None and again.checkpoint.reason == "occluded"


# -- counts, step events and the rate finding (7.8 policy 3, 8.3) -------------------------------------------------

def _events(hits_by_side, run_id="run-1", start=1):
    out, seq = [], start
    for side, titles in hits_by_side.items():
        sentinel = PopupSentinel(CAT)
        for i, t in enumerate(titles):
            (hit,) = sentinel.sweep([win(t, hwnd=1000 + i)], RUN)
            out.append(popup_event(hit, run_id=run_id, shard_id=f"{run_id}-s1", side=side, seq=seq,
                                   wall_at="2026-11-10T22:05:00-05:00", mono_ms=seq * 10, flow_id="flow.x",
                                   step_id="s2", screenshot_sha256="ab" * 32))
            seq += 1
    return out


def test_popup_events_carry_the_hit():
    ev = _events({"candidate": ["Message Center", "Rate this app"]})
    assert ev[0].action == "popup" and ev[0].popup_title == "POP-06" and ev[0].keys == "escape"
    assert ev[0].reason == "dismiss_key" and ev[0].pre_hash == "ab" * 32 and ev[0].source == "orchestrator"
    assert ev[1].popup_title == "Rate this app" and ev[1].reason == "unknown:agent_dismiss"
    assert PopupCounter.from_events(ev).counts == {"POP-06": 1, "Rate this app": 1}


def test_more_hits_on_the_candidate_than_the_base_is_a_popup_rate_increase():
    ev = _events({"base": [], "candidate": ["Message Center"] * 3 + ["Rate this app"]})
    ev += [e for e in _events({"base": ["Low memory"]}, start=100)]
    base = BuildCounts.from_events("main-1998.2.0-4f1c9a2", ev, side="base", runs=1)
    cand = BuildCounts.from_events("main-1998.3.0-5a2b3c4", ev, side="candidate")
    assert cand.runs == 1 and cand.counts["POP-06"] == 3
    (f,) = popup_rate_findings([base, cand])                        # unknown titles are not catalog hits
    assert f.kind == "popup_rate_increase" and json.loads(f.detail)["popup_id"] == "POP-06"
    assert popup_rate_findings([cand]) == ()
    assert popup_rate_findings([BuildCounts("b1", 1, {"POP-06": 1}), BuildCounts("b1", 1, {"POP-06": 5})]) == ()


def test_the_rate_is_per_run():
    doubled = popup_rate_findings([BuildCounts("b1", 2, {"POP-06": 2}), BuildCounts("b2", 2, {"POP-06": 4})])
    assert [json.loads(f.detail)["hits"] for f in doubled] == [4]
    assert popup_rate_findings([BuildCounts("b1", 2, {"POP-06": 2}), BuildCounts("b2", 4, {"POP-06": 4})]) == ()
    assert popup_rate_findings([BuildCounts("b1", 1, {"POP-06": 3}), BuildCounts("b2", 1, {"POP-06": 1})]) == ()
    with pytest.raises(ValueError):
        BuildCounts.from_events("b3", [])


# -- catalog-derived title sets (INV-14, 5.4.6) -------------------------------------------------------------------

def test_error_title_patterns_come_from_the_app_failure_entries_and_fire_inv14():
    pats = error_title_patterns(CAT)
    assert pats == (entry("POP-10").title_regex, entry("POP-20").title_regex)
    assert error_title_patterns(variant("POP-20", status="disabled")) == (entry("POP-10").title_regex,)
    screen = InvRect(0, 0, 1920, 1080)
    main = Window("main", InvRect(0, 0, 1920, 1040), "main")

    def fired(title):
        dlg = Window("dlg", InvRect(800, 400, 300, 200), "owned", title=title, owner_screen=screen,
                     in_process_tree=True, error_template=True)
        model = ScreenModel("A", (480, 360), (UIComponent("frame", InvRect(0, 0, 480, 360), "panel"),),
                            windows=(main, dlg), virtual_screen=screen, pinned_frame_rect=main.rect)
        res = check(model, SurfaceRules("charts.charts", tier=2), config=CheckerConfig(error_title_patterns=pats))
        return [v.invariant_id for v in res.violations]

    assert fired("Java Exception") == ["INV-14"]
    assert fired("What's New") == []


def test_order_title_sets_feed_the_window_watcher():
    assert order_dialog_regexes(variant("POP-14", status="disabled")) == ("^Order Confirmation",)   # choice 13
    assert order_dialog_regexes(variant("POP-14", retired=True, retired_reason="x")) == ()
    titles = TitleSets.compile(order_dialog=order_dialog_regexes(CAT), order_event=order_event_regexes(CAT))
    classes = classify_windows([win("Order Confirmation", hwnd=1), win("Order filled", hwnd=2),
                                win("Message Center", hwnd=3)], titles, now=0.0)
    assert [w.window.hwnd for w in classes.order_dialogs] == [1] and classes.order_event_window
