"""L-XREF-13 and the Explorer deny-list file: loader, registry checks and the denied-action plan (4.15, 4.16, 6.8)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel.lint import lint
from sentinel.orchestrator.explorer import (DEFAULT_DENY_LIST, DenyEntry, DenyListError, deny_entries,
                                            denied_action_plan, load_deny_list, match_deny, regex_sample,
                                            registry_index)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
DENY = "policy/explorer_deny_list.yaml"


@pytest.fixture
def cfg(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def edit(root: Path, change) -> None:
    p = root / DENY
    data = yaml.safe_load(p.read_text())
    change(data["entries"])
    p.write_text(yaml.safe_dump(data, sort_keys=False))


def xref13(root: Path) -> list[str]:
    return [f"{v.path}: {v.message}" for v in lint(root, today=TODAY).violations if v.rule == "L-XREF-13"]


def test_the_example_deny_list_lints_clean_and_the_rule_runs():
    report = lint(EXAMPLE, today=TODAY)
    assert [v for v in report.violations if v.rule == "L-XREF-13"] == []
    assert "L-XREF-13" not in report.skipped


@pytest.mark.parametrize("change,where,text", [
    (lambda e: e.append({"id": "ghost", "denied": "x", "why": "y", "control": "trade.active-trader.ladder.ghost"}),
     "entries[11].control", "is not a <surface>.<control> of the registry"),
    (lambda e: e.append({"id": "ghost-surface", "denied": "x", "why": "y", "surface": "trade.nowhere"}),
     "entries[11].surface", "surface trade.nowhere is not in the registry"),
    (lambda e: e.append({"id": "nothing", "denied": "x", "why": "y"}), "entries[11]", "has no detection"),
    (lambda e: e[0].update(label_regex="chat("), "entries[0].label_regex", "does not compile"),
    (lambda e: e[0].update(window_title_regex="[^a-z]"), "entries[0].window_title_regex", "cannot drive"),
    (lambda e: e[1].update(id="chat"), "entries[1].id", "entry id chat repeats"),
])
def test_l_xref_13_reports_each_problem(cfg, change, where, text):
    edit(cfg, change)
    found = xref13(cfg)
    assert len(found) == 1 and found[0].startswith(where) and text in found[0], found


def test_a_checkout_without_the_file_is_not_reported(cfg):
    (cfg / DENY).unlink()
    assert xref13(cfg) == []


def test_load_deny_list_reads_the_file_and_the_registry():
    entries, registry = load_deny_list(EXAMPLE)
    by = {e.name: e for e in entries}
    assert by["buy-mkt-button"].geometry == {"trade.active-trader.ladder.buy-mkt-button"}
    assert by["buy-mkt-button"].control_names == ("BUY MKT",) and by["chat"].title == "chat|support"
    assert set(registry.deny_controls) == {"dialog.order-confirmation.send-button",
                                           "trade.active-trader.ladder.buy-mkt-button",
                                           "trade.active-trader.ladder.flatten-button",
                                           "charts.flexible-grid.active-trader-cell.buy-mkt-button"}
    assert match_deny(title=None, labels=(), target_kind="trade.active-trader.ladder.flatten-button",
                      deny_list=entries) == "flatten-button"


def test_deny_entries_refuse_a_bad_file():
    registry = registry_index([{"id": "s", "controls": [{"id": "c", "ocr_text": "Go", "deny": True}]}])
    with pytest.raises(DenyListError, match="has no detection"):
        deny_entries({"entries": [{"id": "x"}]}, registry)
    with pytest.raises(DenyListError, match="no entries list"):
        deny_entries({"schema_version": 1}, registry)
    with pytest.raises(DenyListError, match="not a deny_entry with an id"):
        deny_entries({"entries": [{"label_regex": "go"}]}, registry)
    ok = deny_entries({"entries": [{"id": "c", "control": "s.c", "order_path": True}, {"id": "s", "surface": "s"}]},
                      registry)
    assert ok[0].order_path and ok[0].control_names == ("Go",) and ok[1].surface == "s"


def test_registry_index_skips_malformed_entries_and_merges_names():
    reg = registry_index([None, {"id": 3}, {"id": "s", "controls": [
        "junk", {"id": "a", "jab": {"name": "Send"}, "ocr_text": "Send"}, {"id": "b", "jab": "bad"}]}])
    assert reg.surfaces == {"s"} and reg.controls["s.a"].names == ("Send",) and reg.controls["s.b"].names == ()
    assert reg.deny_controls == {}


@pytest.mark.parametrize("pattern,sample", [
    ("^setup$|application settings", "setup"), ("log ?out|log off", "logout"), (r"^(buy|sell)\b|buy mkt", "buy"),
    (r"share|tos\.mx", "share"), ("[a-c]x", "ax"), (r"\d{2}", "00"), (r"\w+ \s?go", "a go"), ("a.c", "axc"),
    ("x*?y", "y"), ("[^a]", None), ("", None), ("(", None), (r"(?=a)a", None), (r"\bQ", "Q"),
])
def test_regex_sample(pattern, sample):
    assert regex_sample(pattern) == sample


def test_the_plan_drives_every_detection_and_every_unnamed_deny_control():
    entries = (DenyEntry("e", title="chat", label="share", geometry=frozenset({"s.c"}), surface="s", order_path=True,
                         control_names=("Go", "Go now")),
               DenyEntry("odd", label="[^a]"))
    plan = denied_action_plan(entries, {"s.c": ("Go",), "t.d": ("Flatten",)})
    assert [(p.entry, p.detection) for p in plan] == [("e", "title"), ("e", "label"), ("e", "geometry"),
                                                     ("e", "surface"), ("e", "order_path"), ("control:t.d", "control")]
    assert all(p.action.profile == "explorer" for p in plan)
    geo, drag, ctl = plan[2].action, plan[4].action, plan[5].action
    assert (geo.target_kind, geo.labels) == ("s.c", ("Go", "Go now"))
    assert (drag.kind, drag.target_kind, drag.end_kind) == ("drag", "splitter", "s.c")
    assert (ctl.target_kind, ctl.labels) == ("t.d", ("Flatten", "Flatten"))
    assert len(denied_action_plan(DEFAULT_DENY_LIST)) == 21


def test_a_surface_entry_hits_the_surface_and_its_controls_only():
    e = DenyEntry("s", surface="chat.room")
    assert e.hit(title=None, labels=(), target_kind="chat.room") and e.hit(title=None, labels=(), target_kind="chat.room.input")
    assert not e.hit(title=None, labels=(), target_kind="chat.roomy") and not e.hit(title=None, labels=(), target_kind=None)
