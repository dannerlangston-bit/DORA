"""The popup sentinel wired into the simulated PR run (w2 wiring item 5): the catalog loaded from the config, its
order-dialog and order-event regexes in the Window Watcher's title sets, a sweep before every step and capture, the
foreground check before every input batch, dismissals through the gateway, WM_CLOSE for unknown windows, the popup
rate finding, and the catalog's `{schema_version, entries}` form with ids past 12.8 (SD-47, SD-95)."""
from __future__ import annotations

import dataclasses
from datetime import date


from sentinel.demo import EXAMPLE, STOCK_FLOW, wrong_number_scenario
from sentinel.lint.rules import lint as lint_repo
from sentinel.orchestrator.popups import load_catalog
from sentinel.orchestrator.simrig import SimScenario, SimulatedRig
from sentinel.pipeline.config import SentinelConfig
from sentinel.pipeline.orchestration import RigScript, catalog_titles
from sentinel.pipeline.run import run_pipeline
from sentinel.store import Store

TODAY = date(2026, 11, 10)
S06 = f"flow/trade.order-entry.stock/{STOCK_FLOW}/s06"


def _run(tmp_path, **script):
    sc = dataclasses.replace(wrong_number_scenario(TODAY), rig=RigScript(**script))
    r = run_pipeline(EXAMPLE, sc, tmp_path)
    with Store(tmp_path / "sentinel.db") as store:
        return r, store.step_events(r.run_id), store.findings(r.run_id)


def test_a_catalog_popup_is_dismissed_and_an_unknown_one_closed(tmp_path):
    r, events, findings = _run(tmp_path, popups=(("s04", "Message Center"), ("s05", "Strange Window")))
    pops = [(e.popup_title, e.reason, e.keys, e.step_id) for e in events if e.action == "popup"]
    assert pops == [("POP-06", "dismiss_key", "escape", "s04"), ("Strange Window", "unknown:wm_close", None, "s05")]
    escape = [e for e in events if e.action == "gateway_decision" and e.gate_decision == "emitted" and e.keys
              and "0x1B" in e.keys]
    assert escape                                       # the dismiss key went through the gateway
    assert {f.kind for f in findings} >= {"unknown_popup", "popup_rate_increase"}
    assert any(a["control"] == "popup" for a in r.alerts)
    assert {o.outcome for o in r.outcomes} == {"identical", "assertion_failed"}     # both windows gone: captured


def test_a_foreign_foreground_window_ends_the_step_with_env_error_occluded(tmp_path):
    r, _, _ = _run(tmp_path, foreign_foreground="Untitled - Notepad")
    occluded = [o for o in r.outcomes if o.reason == "occluded"]
    assert occluded and {o.outcome for o in occluded} == {"env_error"}
    assert r.status == "complete_with_env_errors"


def test_the_watchers_titles_come_from_the_catalog():
    cfg = SentinelConfig.load(EXAMPLE)
    titles = catalog_titles(cfg)
    rig = SimulatedRig(SimScenario(titles=titles))
    rig.dialog_open, rig.app_running = True, True
    rig.poll_flag()
    assert rig.watcher.classes.order_dialogs                    # POP-14's "^Order Confirmation" (7.8, 5.4.6)
    cat = load_catalog(EXAMPLE / "popups" / "catalog.yaml")
    import yaml
    raw = yaml.safe_load((EXAMPLE / "popups" / "catalog.yaml").read_text())
    assert cat.bare_list is False and raw["schema_version"] == 1 and "SD-66" in raw["entries"][4]["notes"]


def test_l_pop_01_accepts_ids_past_12_8_in_sequence(tmp_path):
    import shutil
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    path = root / "popups" / "catalog.yaml"
    extra = """  - popup_id: POP-24
    name: popup.extra
    class: dismissable
    title_regex: "(?i)extra"
    action: dismiss_key
    key: escape
    severity: info
    expected_phases: [running]
    owner: sentinel-ops
    status: unverified
    first_seen_build: main-1998.2.0-4f1c9a2
"""
    path.write_text(path.read_text() + extra.replace("POP-24", "POP-23") + extra.replace("extra", "later"))
    report = lint_repo(root, phase=0, today=TODAY)
    lines = [v.line() for v in report.violations if v.rule == "L-POP-01"]
    assert lines == [] or all("POP-24" not in x and "POP-23" not in x for x in lines), lines
    path.write_text(path.read_text().replace("POP-23", "POP-30"))
    report = lint_repo(root, phase=0, today=TODAY)
    assert any("POP-30" in v.line() for v in report.violations if v.rule == "L-POP-01")
