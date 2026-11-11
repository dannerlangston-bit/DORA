"""Phase 0 branches for the runtime and their lint (4.16 Phase 0 reads, 6.13, 7.14, 11.8, L-XREF-15)."""
from __future__ import annotations

import copy
import re
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel import branches as br
from sentinel.capture.redaction import DEFAULT_PATTERNS
from sentinel.invariants import ScreenModel
from sentinel.lint import lint
from sentinel.lint.checks import branches as branches_lint
from sentinel.lint.repo import Repo, load_yaml_text
from sentinel.lint.rules import BRANCH_KEYS, check_modules
from sentinel.orchestrator.attestation import ABSENT, AttestationProfile
from sentinel.orchestrator.identity import Allowlist, classify

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
BRANCHES = "phase0/branches.yaml"
ALLOW = "gateway/dismiss_allowlist.yaml"
TODAY = date(2026, 11, 10)
BASE = load_yaml_text((EXAMPLE / BRANCHES).read_text(encoding="utf-8"))
PIN = "read_on_build: main-1998.2.0-4f1c9a2\nread_by: system-owner\nread_at: '2026-10-05T14:00:00Z'\n"


def data(**changes) -> dict:
    """The example file with top-level keys replaced; `safety={n: {...}}` replaces those items."""
    d = copy.deepcopy(BASE)
    safety = changes.pop("safety", {})
    d.update(changes)
    d["safety"] = [dict(s, **safety.get(s["item"], {})) for s in d["safety"]]
    return d


def undecided(deadline: str | None = "2026-11-20") -> dict:
    return {"branch": "undecided", **({"deadline": deadline} if deadline else {})}


# -- the file and its resolution (11.8) --------------------------------------------------------------------------

def test_the_example_loads_with_its_recorded_branches():
    b = br.load(EXAMPLE / BRANCHES)
    assert {k: b.branch(k) for k in BRANCH_KEYS} == {k: "A" for k in BRANCH_KEYS}
    assert b.run_health_lines(TODAY) == () and b.batch_api == "undecided" and b.model_id is None
    assert sorted(b.safety) == list(br.SAFETY_ITEMS) and all(not s.read for s in b.safety.values())


def test_the_records_and_their_defaults_are_the_11_8_table():
    assert set(br.DEFAULTS) == set(BRANCH_KEYS) and list(br.RECORD_IDS.values()) == [f"DR-0{i}" for i in range(1, 8)]
    b = br.parse(data(**{k: undecided() for k in BRANCH_KEYS}))
    assert {k: b.branch(k) for k in BRANCH_KEYS} == {"jab": "B", "data_fixture": "B", "login_mfa": "B",
                                                    "pinned_install": "B", "aa_floor": "B", "session_rule": "A",
                                                    "computer_use": "B"}


def test_an_undecided_record_takes_its_default_and_says_so_in_run_health():
    b = br.parse(data(data_fixture=undecided("2026-11-08"), login_mfa=undecided("2026-11-20")))
    c = b.choices["data_fixture"]
    assert c.undecided and c.resolved == "B" and c.record == "DR-02" and c.deadline == date(2026, 11, 8)
    assert c.past_deadline(TODAY) and not c.past_deadline(date(2026, 11, 8))
    assert not b.choices["login_mfa"].past_deadline(TODAY)
    assert b.run_health_lines(TODAY) == (
        "phase 0 DR-02 data_fixture undecided: default branch B in force (deadline 2026-11-08 passed)",
        "phase 0 DR-03 login_mfa undecided: default branch B in force (deadline 2026-11-20)")


@pytest.mark.parametrize("change,path", [
    ({"jab": undecided(None)}, "jab.deadline"),
    ({"pinned_install": {"branch": "A", "title_regex": "Main@thinkorswim [build"}}, "pinned_install.title_regex"),
    ({"version_read": {"about_regex": "(build"}}, "version_read.about_regex"),
    ({"version_read": {"title_regex": "a"}, "pinned_install": {"branch": "A", "title_regex": "b"}},
     "pinned_install.title_regex"),
    ({"safety": {3: {"item": 11}}}, "safety[2].item"),
    ({"safety": {3: {"item": 4}}}, "safety[3].item"),
    ({"safety": {5: {"result": "different", "observed": ["paper: ^P-[0-9]+$"]}}}, "safety[4].observed"),
    ({"safety": {5: {"result": "different", "observed": ["paper: ^P-[0-9]+$", "live: ([0-9]"]}}},
     "safety[4].observed.live"),
])
def test_what_the_notation_cannot_say_the_loader_refuses(change, path):
    d = data(**change)
    assert [p for p, _ in br.problems(d)] == [path]
    with pytest.raises(br.BranchesError) as exc:
        br.parse(d)
    assert [p for p, _ in exc.value.problems] == [path]


def test_a_schema_violation_or_a_missing_file_is_refused(tmp_path):
    with pytest.raises(br.BranchesError, match="jab"):
        br.parse(data(jab={"branch": "C"}))
    with pytest.raises(br.BranchesError, match="missing"):
        br.load(tmp_path / BRANCHES)


def test_the_title_regex_is_read_from_either_record():
    pinned = br.parse(data(pinned_install={"branch": "A", "title_regex": r"^Main@thinkorswim \[build (\d+)\]$"}))
    assert pinned.gates().title_regex == r"^Main@thinkorswim \[build (\d+)\]$"
    both = br.parse(data(version_read={"title_regex": "x", "about_regex": "y"},
                         pinned_install={"branch": "A", "title_regex": "x"}))
    assert (both.gates().title_regex, both.gates().about_regex) == ("x", "y")


# -- gates (DR-01 to DR-06) -------------------------------------------------------------------------------------

def test_dr03_b_sends_a_challenge_to_the_recovery_flow_and_a_fails_at_once():
    assert br.parse(data(login_mfa={"branch": "B"})).gates().challenge_enters_recovery
    assert not br.parse(data()).gates().challenge_enters_recovery
    assert br.parse(data(login_mfa=undecided())).gates().challenge_enters_recovery          # default B


def test_dr02_b_without_ondemand_turns_frozen_checkpoints_masked_live():
    a, b = br.parse(data()).gates(), br.parse(data(data_fixture={"branch": "B"})).gates()
    assert a.frozen_provider == "backend_fixture" and b.frozen_provider == "ondemand"
    assert a.data_mode("frozen", ondemand_available=False) == "frozen"
    assert b.data_mode("frozen", ondemand_available=False) == "masked_live"
    assert b.data_mode("frozen", ondemand_available=True) == "frozen"
    assert b.data_mode("masked_live", ondemand_available=True) == "masked_live"


def test_dr01_picks_the_screen_model_branch_and_dr04_to_dr06_their_rules():
    g = br.parse(data(jab=undecided(), pinned_install={"branch": "B"}, aa_floor={"branch": "B"},
                      session_rule={"branch": "B"})).gates()
    assert ScreenModel(g.bounds_source, (10, 10)).branch == "B"
    assert g.update_expected and g.upgrade_flow_every_rc and not g.thresholds_from_aa and not g.second_login_ends_first
    a = br.parse(data()).gates()
    assert ScreenModel(a.bounds_source, (10, 10)).branch == "A"
    assert not a.update_expected and not a.upgrade_flow_every_rc and a.thresholds_from_aa and a.second_login_ends_first


# -- safety items of 6.13 ----------------------------------------------------------------------------------------

NEW_IDS = {5: {"result": "different", "observed": ["paper: ^PM[0-9]{6}$", "live: ^L[0-9]{9}$"]}}


def test_item_5_b_replaces_the_id_formats_for_identity_and_redaction():
    allow = Allowlist("rig-01", ("PM123456",), "0" * 64)
    assumed, read = br.parse(data()).identity(), br.parse(data(safety=NEW_IDS)).identity()
    assert not assumed.replaced and assumed.paper.fullmatch("D-20481133") and assumed.live.fullmatch("12345678SCHW")
    assert classify("L123456789", allow, live_re=assumed.live) == "paper_mode_mismatch"
    assert classify("L123456789", allow, live_re=read.live) == "live_identity"
    assert read.paper.fullmatch("PM123456") and not read.paper.fullmatch("D-20481133")
    assert assumed.sweep_patterns(DEFAULT_PATTERNS) == DEFAULT_PATTERNS
    swept = dict(read.sweep_patterns(DEFAULT_PATTERNS))
    assert re.search(swept["paper_id"], "account PM123456 open") and not re.search(swept["paper_id"], "XPM123456")
    assert re.search(swept["live_id"], "(L123456789)") and swept["email"] == dict(DEFAULT_PATTERNS)["email"]


def test_items_2_and_3_b_make_a3_and_a4_absent_and_a9_run_twice():
    profile = AttestationProfile()
    att = br.parse(data()).attestation()
    assert (att.order_confirmation, att.shift_click_send, att.a9_runs) == (True, False, 1)
    att = br.parse(data(safety={2: {"result": "different"}, 3: {"result": "different"}})).attestation()
    assert (att.order_confirmation, att.shift_click_send, att.a9_runs) == (ABSENT, ABSENT, 2)
    applied = att.apply(profile)
    assert (applied.order_confirmation, applied.shift_click_send, applied.a9_runs) == (ABSENT, ABSENT, 2)


def test_item_6_picks_the_drill_ticket_and_item_9_the_recovery_cancel_all():
    default = br.parse(data()).drill()                                                       # 11.8: branch B ticket
    assert (default.side, default.limit_from_market, default.stray_send_row) == ("buy", -1, "Working")
    assert not default.cancel_all_confirmed
    read = br.parse(data(safety={6: {"result": "confirmed"}, 9: {"result": "confirmed"}})).drill()
    assert (read.side, read.limit_from_market, read.stray_send_row) == ("sell_to_open", 1, "Canceled")
    assert read.cancel_all_confirmed
    assert br.parse(data(safety={6: {"result": "different"}})).drill().side == "buy"


def test_item_10_backs_only_observed_dismiss_labels_and_item_4_chart_drags():
    unread = br.parse(data()).gateway()
    assert unread.dismiss_labels(["Delete", "Edit"]) == frozenset() and unread.chart_drag_order_stage
    read = br.parse(data(safety={10: {"result": "confirmed", "observed": ["Delete"]},
                                 4: {"result": "confirmed"}})).gateway()
    assert read.dismiss_labels(["Delete", "Edit"]) == {"Delete"} and not read.chart_drag_order_stage


def test_safety_item_branches_and_their_conservative_defaults():
    b = br.parse(data(safety={1: {"result": "different"}, 8: {"result": "confirmed"}}))
    assert b.safety_item(1).branch == "B" and b.safety_item(8).branch == "A"
    assert b.safety_item(7).branch is None
    assert {n: b.safety_item(n).branch for n in (2, 3, 4, 5, 6, 9, 10)} == \
        {2: "A", 3: "A", 4: "B", 5: "A", 6: "B", 9: "B", 10: "B"}
    sparse = br.parse(data() | {"safety": []})
    assert sparse.safety_item(6) == br.SafetyItem(6) and sparse.drill().side == "buy"


# -- DR-07 and the reviewer pins (3.15) ---------------------------------------------------------------------------

def test_dr07_gates_the_agent_rung_explorer_reviewer_and_batch_api():
    pending = br.parse(data(computer_use=undecided(), reviewer={"model_id": "claude-x"})).models()
    assert not (pending.reviewer_enabled or pending.agent_rung_allowed or pending.explorer_allowed)
    b = br.parse(data(computer_use={"branch": "B"}, reviewer={"model_id": "claude-x"})).models()
    assert b.reviewer_enabled and not b.agent_rung_allowed and not b.explorer_allowed
    a = br.parse(data(reviewer={"model_id": "claude-x"}, batch_api="on")).models()
    assert a.reviewer_enabled and a.agent_rung_allowed and a.explorer_allowed and a.batch_api_enabled
    unpinned = br.parse(data()).models()
    assert not (unpinned.reviewer_enabled or unpinned.agent_rung_allowed or unpinned.batch_api_enabled)


# -- L-XREF-15 (lint/checks/branches.py) -------------------------------------------------------------------------

def run(tmp_path: Path, files: dict[str, object]) -> list[tuple[str, str, str, str, str]]:
    """Write `files` (text, or data dumped as YAML) into an empty tree and run the module."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else yaml.safe_dump(content, sort_keys=False))
    out: list[tuple[str, str, str, str, str]] = []

    def add(rule: str, file: str, message: str, path: str = "", severity: str | None = None) -> None:
        out.append((rule, severity or "error", file, path, message))

    branches_lint.check(Repo.load(tmp_path), add, 0, TODAY)
    return out


def test_the_example_is_clean_and_the_rule_runs():
    for phase in (0, 1):
        report = lint(EXAMPLE, phase=phase, today=TODAY)
        assert not [v for v in report.violations if v.rule == "L-XREF-15"]
        assert "L-XREF-15" not in report.skipped
    assert branches_lint in check_modules()


def test_undecided_needs_a_deadline_and_warns_past_it(tmp_path):
    found = run(tmp_path, {BRANCHES: data(jab=undecided(None), aa_floor=undecided("2026-11-01"),
                                          login_mfa=undecided("2026-12-01"))})
    assert [(s, p) for _, s, _, p, _ in found] == [("error", "jab.deadline")]
    found = run(tmp_path, {BRANCHES: data(aa_floor=undecided("2026-11-01"), login_mfa=undecided("2026-12-01"))})
    assert [(s, p) for _, s, _, p, _ in found] == [("warn", "aa_floor")]
    assert "default branch B is in force" in found[0][4]


def test_an_off_schema_file_is_left_to_rules_py(tmp_path):
    assert run(tmp_path, {BRANCHES: data(jab={"branch": "C"})}) == []
    assert run(tmp_path, {}) == []


def test_the_dismiss_allowlist_agrees_with_item_10(tmp_path):
    allow = f"schema_version: 1\nlabels: [Delete]\n{PIN}"
    found = run(tmp_path, {BRANCHES: data(), ALLOW: allow})
    assert [(f, p) for _, _, f, p, _ in found] == [(ALLOW, "labels")]
    observed = data(safety={10: {"result": "confirmed", "observed": ["Edit"]}})
    assert [(f, p) for _, _, f, p, _ in run(tmp_path, {BRANCHES: observed, ALLOW: allow})] == [(ALLOW, "labels[0]")]
    ok = data(safety={10: {"result": "confirmed", "observed": ["Delete", "Edit"]}})
    assert run(tmp_path, {BRANCHES: ok, ALLOW: allow}) == []
    assert run(tmp_path, {BRANCHES: data(), ALLOW: "schema_version: 1\nlabels: []\n"}) == []


def test_the_settings_profile_agrees_with_items_2_and_3(tmp_path):
    profile = (EXAMPLE / "profiles" / "profile.app-settings.sentinel-pinned.yaml").read_text()
    rel = "profiles/profile.app-settings.sentinel-pinned.yaml"
    assert run(tmp_path, {BRANCHES: data(), rel: profile}) == []
    found = run(tmp_path, {BRANCHES: data(safety={3: {"result": "different"}}), rel: profile})
    assert [(f, p) for _, _, f, p, _ in found] == [(rel, "settings.order_confirmation")]
    absent = profile.replace("shift_click_send: false", "shift_click_send: absent")
    found = run(tmp_path, {BRANCHES: data(), rel: absent})
    assert [(f, p) for _, _, f, p, _ in found] == [(rel, "settings.shift_click_send")]
    assert run(tmp_path, {BRANCHES: data(safety={2: {"result": "different"}}), rel: absent}) == []


def test_item_5_b_account_ids_follow_the_recorded_format(tmp_path):
    allow = (EXAMPLE / "gateway" / "allowlist.yaml").read_text()
    fixture = "schema_version: 1\naccounts:\n  - {account_id: PM123456}\n  - {account_id: D-1}\n"
    found = run(tmp_path, {BRANCHES: data(safety=NEW_IDS), "gateway/allowlist.yaml": allow,
                           "fixtures/fx.rig-01.paper.yaml": fixture})
    assert [(f, p) for _, _, f, p, _ in found] == [("gateway/allowlist.yaml", "rigs[0].account_ids[0]"),
                                                   ("gateway/allowlist.yaml", "rigs[0].account_ids[1]"),
                                                   ("fixtures/fx.rig-01.paper.yaml", "accounts[1].account_id")]
    assert run(tmp_path, {BRANCHES: data(), "gateway/allowlist.yaml": allow}) == []


def test_the_full_lint_reports_the_problems_and_the_deadline(tmp_path):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    (root / BRANCHES).write_text(yaml.safe_dump(data(aa_floor=undecided("2026-11-01"),
                                                     version_read={"title_regex": "(build"})))
    found = {(v.rule, v.severity, v.path) for v in lint(root, today=TODAY).violations if v.rule == "L-XREF-15"}
    assert ("L-XREF-15", "error", "version_read.title_regex") in found
    root2 = tmp_path / "cfg2"
    shutil.copytree(EXAMPLE, root2)
    (root2 / BRANCHES).write_text(yaml.safe_dump(data(aa_floor=undecided("2026-11-01"))))
    found = {(v.severity, v.path, v.message[:24]) for v in lint(root2, today=TODAY).violations
             if v.rule == "L-XREF-15"}
    assert ("warn", "aa_floor", "DR-05 aa_floor is still ") in found
