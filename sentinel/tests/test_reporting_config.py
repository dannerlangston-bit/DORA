"""`policy/reporting.yaml` (4.16, 9.2, 9.9): the loader of report/reporting_config.py and L-XREF-21 as
lint/checks/reporting.py and the full lint report it."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel.lint import Repo, lint
from sentinel.lint.checks import reporting as rcheck
from sentinel.lint.repo import ConfigLoader
from sentinel.report import reporting_config as rc

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
REL = "policy/reporting.yaml"
TODAY = date(2026, 11, 10)


@pytest.fixture
def cfg(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def doc(root: Path) -> dict:
    return yaml.safe_load((root / REL).read_text())


def save(root: Path, data: dict) -> None:
    (root / REL).write_text(yaml.safe_dump(data, sort_keys=False))


def module(root: Path) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    rcheck.check(Repo.load(root), lambda rule, file, msg, path="", severity=None: out.append((rule, path, msg)),
                 0, TODAY)
    return out


def full(root: Path) -> list[tuple[str, str]]:
    return [(v.path, v.message) for v in lint(root, today=TODAY).violations if v.rule == "L-XREF-21"]


def example() -> dict:
    """The example as the loader reads it (dates kept as text)."""
    return yaml.load((EXAMPLE / REL).read_text(), Loader=ConfigLoader)  # noqa: S506 - a SafeLoader


# -- the loader ---------------------------------------------------------------------------------------------------

def test_the_example_loads_with_the_four_channels_and_every_switch_off():
    c = rc.load(EXAMPLE / REL)
    assert (c.jira_product, c.jira_harness) == ("TOS", "SENT")
    assert [c.channel(r) for r in rc.SLACK_ROLES] == ["#sentinel-oncall", "#sentinel-triage", "#sentinel-release",
                                                       "#sentinel-safety"]
    assert c.paging_service == "oncall-paging" and c.email == ("sentinel-digest@example.internal",)
    assert [c.auto_files(t) for t in rc.TIERS] == [False, False, False]
    assert c.check_mode(TODAY) == "informational"


def test_the_commit_status_uses_the_latest_dated_mode():
    d = example()
    d["check_mode_history"] = [{"mode": "informational", "since": "2026-01-05", "by": "asmith"},
                               {"mode": "blocking", "since": "2026-06-01", "by": "asmith"},
                               {"mode": "informational", "since": "2026-09-15", "by": "bkhan"}]
    c = rc.from_dict(d)
    assert c.check_mode(date(2026, 1, 4)) == "informational"           # before any entry: the 9.2 default
    assert c.check_mode(date(2026, 6, 1)) == "blocking"
    assert c.check_mode(date(2026, 9, 14)) == "blocking"
    assert c.check_mode(date(2026, 9, 15)) == "informational"
    assert c.mode_change(date(2026, 7, 1)) == rc.ModeChange("blocking", date(2026, 6, 1), "asmith")
    assert c.mode_change(date(2025, 12, 31)) is None


def test_two_changes_on_one_date_take_the_later_one_in_file_order():
    d = example()
    d["check_mode_history"] = [{"mode": "blocking", "since": "2026-06-01", "by": "asmith"},
                               {"mode": "informational", "since": "2026-06-01", "by": "asmith"}]
    assert rc.from_dict(d).check_mode(date(2026, 6, 1)) == "informational"
    assert rc.config_problems(d, today=TODAY) == []


def test_auto_file_defaults_to_off_on_every_tier():
    d = example()
    d["auto_file"] = {}
    c = rc.from_dict(d)
    assert c.auto_file == {1: False, 2: False, 3: False}
    d["auto_file"] = {"tier3": True}
    assert [rc.from_dict(d).auto_files(t) for t in rc.TIERS] == [False, False, True]
    assert rc.ReportingConfig((), "TOS", "SENT", {}, "p").auto_file == {1: False, 2: False, 3: False}


def test_the_loader_refuses_schema_and_content_problems():
    d = example()
    d["check_mode_history"][0]["mode"] = "advisory"
    with pytest.raises(rc.ReportingConfigError, match="advisory"):
        rc.from_dict(d)
    d = example()
    del d["slack"]["safety"]
    with pytest.raises(rc.ReportingConfigError, match="safety"):
        rc.from_dict(d)
    d = example()
    d["jira"]["product_project"] = "tos"
    with pytest.raises(rc.ReportingConfigError, match="does not match"):
        rc.from_dict(d)


def test_an_unreadable_file_is_a_config_error(tmp_path):
    with pytest.raises(rc.ReportingConfigError, match="cannot read"):
        rc.load(tmp_path / "missing.yaml")


def test_bad_arguments_are_refused():
    c = rc.from_dict(example())
    with pytest.raises(ValueError):
        c.auto_files(4)
    with pytest.raises(ValueError):
        c.channel("pager")


@pytest.mark.parametrize("history, path, needle", [
    ([], "check_mode_history", "at least one dated entry"),
    ([{"mode": "blocking", "since": "2026-06-01", "by": "asmith"},
      {"mode": "informational", "since": "2026-05-01", "by": "asmith"}], "check_mode_history[1].since", "date order"),
    ([{"mode": "informational", "since": "2026-12-01", "by": "asmith"}], "check_mode_history[0].since",
     "after today"),
    ([{"mode": "informational", "since": "2026-01-01", "by": " "}], "check_mode_history[0].by", "names the person"),
    ([{"mode": "informational", "since": "2026-01-01", "by": "reporter"}], "check_mode_history[0].by",
     "names the person"),
    ([{"mode": "informational", "since": "2026-02-30", "by": "asmith"}], "check_mode_history[0].since",
     "not a date"),
])
def test_mode_history_problems(history, path, needle):
    d = example()
    d["check_mode_history"] = history
    found = rc.config_problems(d, today=TODAY)
    assert [p for p, _ in found] == [path] and needle in found[0][1]


def test_a_future_entry_is_not_yet_in_force_at_run_time():
    d = example()
    d["check_mode_history"].append({"mode": "blocking", "since": "2027-01-04", "by": "asmith"})
    c = rc.from_dict(d)                                         # the runtime loader takes no today (choice 1)
    assert c.check_mode(TODAY) == "informational" and c.check_mode(date(2027, 1, 4)) == "blocking"


def test_jira_and_slack_content_problems():
    d = example()
    d["jira"] = {"product_project": "SENT", "harness_project": "SENT"}
    d["slack"] = {"on_call": "#sentinel-safety", "triage": "", "release": "#rel", "safety": "#safety"}
    found = rc.config_problems(d, today=TODAY)
    assert ("jira.harness_project", "the harness project differs from the product project: harness tickets never "
                                    "reach the product backlog (9.9)") in found
    assert ("slack.triage", "the triage channel is set (9.9: four channels)") in found
    assert ("slack.safety", "the safety channel is #sentinel-safety, not '#safety' (9.9)") in found
    assert not any(p == "slack.on_call" for p, _ in found)      # #safety is the safety channel here, not on_call's
    d["slack"]["safety"] = "#sentinel-safety"
    assert ("slack.on_call", "the on_call channel is not the safety channel: nothing but safety events is posted "
                             "there (9.9)") in rc.config_problems(d, today=TODAY)
    assert rc.config_problems([], today=TODAY) == [("", "policy/reporting.yaml is a mapping (4.16)")]


def test_auto_file_turned_on_lists_the_tiers_a_change_enables():
    off = example()
    on = example()
    on["auto_file"] = {"tier1": False, "tier2": True, "tier3": True}
    assert rc.auto_file_turned_on(off, on) == (2, 3)
    assert rc.auto_file_turned_on(on, on) == ()
    assert rc.auto_file_turned_on(None, on) == (2, 3)                # a new file that turns switches on
    assert rc.auto_file_turned_on(on, off) == ()


# -- the lint -----------------------------------------------------------------------------------------------------

def test_the_example_is_clean_and_the_rule_runs(cfg):
    assert module(cfg) == []
    report = lint(cfg, today=TODAY)
    assert "L-XREF-21" not in report.skipped
    assert full(cfg) == []


def test_lint_rejects_an_unknown_mode(cfg):
    d = doc(cfg)
    d["check_mode_history"][0]["mode"] = "advisory"
    save(cfg, d)
    found = full(cfg)
    assert len(found) == 1 and found[0][0] == "check_mode_history[0].mode" and "advisory" in found[0][1]


def test_lint_rejects_a_missing_safety_channel(cfg):
    d = doc(cfg)
    del d["slack"]["safety"]
    save(cfg, d)
    found = full(cfg)
    assert len(found) == 1 and found[0][0] == "slack" and "safety" in found[0][1]


def test_lint_reports_content_problems_with_their_paths(cfg):
    d = doc(cfg)
    d["slack"]["safety"] = "#alerts"
    d["jira"]["harness_project"] = "sent-1"
    d["check_mode_history"].append({"mode": "blocking", "since": "2026-05-01", "by": "asmith"})   # out of order
    save(cfg, d)
    assert sorted(p for p, _ in full(cfg)) == ["check_mode_history[1].since", "jira.harness_project", "slack.safety"]


def test_a_missing_file_is_an_error(cfg):
    (cfg / REL).unlink()
    assert module(cfg) == [("L-XREF-21", "", f"{REL} is missing; the Reporter reads its commit status mode, Jira "
                                            "projects and Slack channels there (4.16, 9.9)")]


def test_an_unparsable_file_reports_once(cfg):
    (cfg / REL).write_text("check_mode_history: [\n")
    assert module(cfg) == []                                     # rules.py reports the parse error
