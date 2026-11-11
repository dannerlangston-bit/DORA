"""The Phase 0 decision rules DR-01, DR-02, DR-04, DR-05 and `sentinel phase0 decide` (11.8; `sentinel.phase0`)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel import branches, phase0
from sentinel.lint.repo import load_yaml_text
from sentinel.phase0 import (AACheckpoint, ControlRead, Dr01Evidence, Dr02Evidence, Dr04Evidence, Dr05Evidence,
                             LaunchRead, SurfaceDump)

EXAMPLE_BRANCHES = Path(__file__).resolve().parents[1] / "config" / "example" / "phase0" / "branches.yaml"
DAY = date(2026, 11, 11)


def control(i: int, exposed: bool = True) -> ControlRead:
    box = (10 * i, 20, 40, 18)
    if not exposed:
        return ControlRead(f"c{i}", f"Control {i}", "push button", (box[0] + 3, box[1], box[2], box[3]), box)
    return ControlRead(f"c{i}", f"Control {i}", "push button", (box[0] + 2, box[1] - 1, box[2] - 2, box[3] + 2), box)


def dump(surface: str, exposed: int, seconds: float = 1.2, total: int = 100) -> SurfaceDump:
    return SurfaceDump(surface, tuple(control(i, i < exposed) for i in range(total)), seconds)


def all_dumps(**override: tuple[int, float]) -> tuple[SurfaceDump, ...]:
    """The five DR-01 surfaces at 90 of 100 controls exposed in 1.2 s, unless `override` says (exposed, seconds)."""
    return tuple(dump(s, *override.get(s, (90, 1.2))) for s in phase0.DR01_SURFACES)


# -- DR-01 ------------------------------------------------------------------------------------------------------

def test_dr01_is_a_when_all_five_surfaces_expose_80_percent_within_2_px_in_under_2_s():
    v = phase0.decide_dr01(Dr01Evidence(True, all_dumps()))
    assert (v.record, v.key, v.branch) == ("DR-01", "jab", "A")
    assert v.detail["jab_panels"] == phase0.DR01_SURFACES
    assert "order_ticket: 90 percent of 100 visible controls exposed, dump 1.20 s (pass)" in v.evidence


def test_dr01_at_79_percent_on_one_surface_is_b_with_jab_optional_on_the_rest():
    v = phase0.decide_dr01(Dr01Evidence(True, all_dumps(option_chain=(79, 1.2))))
    assert v.branch == "B"
    assert "option_chain" not in v.detail["jab_panels"] and len(v.detail["jab_panels"]) == 4


def test_dr01_slow_dump_missing_module_missing_surface():
    assert phase0.decide_dr01(Dr01Evidence(True, all_dumps(monitor_grid=(100, 2.0)))).branch == "B"
    absent = phase0.decide_dr01(Dr01Evidence(False))
    assert absent.branch == "B" and "absent" in absent.evidence[0]
    assert phase0.decide_dr01(Dr01Evidence(None)).branch is None
    short = phase0.decide_dr01(Dr01Evidence(True, all_dumps()[:4]))
    assert short.branch is None and short.evidence == ("no dump for active_trader_ladder",)
    assert not ControlRead("x", None, "label", (0, 0, 1, 1), (0, 0, 1, 1)).exposed()
    assert not SurfaceDump("order_ticket", (), 0.5).passed


# -- DR-02 ------------------------------------------------------------------------------------------------------

def test_dr02_written_yes_inside_week_4_is_a():
    v = phase0.decide_dr02(Dr02Evidence(True, date(2027, 1, 20), date(2027, 1, 29)))
    assert v.branch == "A" and v.detail["frozen_provider"] == "backend_fixture"


def test_dr02_with_17_of_20_settled_is_b_with_masked_live():
    v = phase0.decide_dr02(Dr02Evidence(False, launches_settled=(True,) * 17 + (False,) * 3, paused_identical=True))
    assert v.branch == "B" and v.detail["frozen_provider"] == "masked_live"
    assert "17 of 20 launches settled" in v.evidence[1]


def test_dr02_18_of_20_settled_and_identical_pauses_use_ondemand():
    settled = (True,) * 18 + (False,) * 2
    late = phase0.decide_dr02(Dr02Evidence(True, date(2027, 2, 10), date(2027, 1, 29), settled, True))
    assert late.branch == "B" and late.detail["frozen_provider"] == "ondemand"
    assert "after Phase 1 week 4" in late.evidence[0]
    assert phase0.decide_dr02(Dr02Evidence(False, launches_settled=settled, paused_identical=False)).detail[
        "frozen_provider"] == "masked_live"
    assert phase0.decide_dr02(Dr02Evidence(False, launches_settled=(True,) * 19)).detail[
        "frozen_provider"] == "masked_live"
    assert phase0.decide_dr02(Dr02Evidence(None)).branch is None


# -- DR-04 ------------------------------------------------------------------------------------------------------

def reads(n: int = 5, **change) -> tuple[LaunchRead, ...]:
    out = []
    for i in range(n):
        values = {"build_id": "1998.3.0", "jre_release": "17.0.9", "vmoptions_sha256": "ab" * 32, "dialogs": ()}
        if i == n - 1:
            values.update(change)
        out.append(LaunchRead(date(2026, 11, 2 + i // 3), **values))
    return tuple(out)


def test_dr04_unchanged_over_five_launches_on_two_days_is_a_and_pins_the_title_regex():
    v = phase0.decide_dr04(Dr04Evidence(True, reads(), title_regex=r"^thinkorswim build (\d+\.\d+\.\d+)$",
                                        launcher="javaw_child"))
    assert v.branch == "A" and v.detail == {"title_regex": r"^thinkorswim build (\d+\.\d+\.\d+)$",
                                            "launcher": "javaw_child"}


def test_dr04_a_changed_read_or_a_dialog_is_b_and_too_few_launches_undecided():
    assert phase0.decide_dr04(Dr04Evidence(True, reads(jre_release="17.0.10"))).branch == "B"
    assert phase0.decide_dr04(Dr04Evidence(True, reads(vmoptions_sha256=None))).branch == "B"
    dialog = phase0.decide_dr04(Dr04Evidence(True, reads(dialogs=("Update available",))))
    assert dialog.branch == "B" and dialog.evidence[-1] == "update or agreement dialogs: Update available"
    assert phase0.decide_dr04(Dr04Evidence(True, reads(4))).branch is None
    one_day = tuple(LaunchRead(date(2026, 11, 2), "b", "j", "v") for _ in range(5))
    assert phase0.decide_dr04(Dr04Evidence(True, one_day)).branch is None
    assert phase0.decide_dr04(Dr04Evidence(False)).branch == "B"
    with pytest.raises(ValueError):
        phase0.decide_dr04(Dr04Evidence(True, reads(), title_regex="(unclosed"))


# -- DR-05 ------------------------------------------------------------------------------------------------------

def calibration(zero_share: float = 0.96, worst_pct: float = 0.05, frozen: int = 100) -> Dr05Evidence:
    cps = []
    nonzero = round(frozen * (1 - zero_share))
    for i in range(frozen):
        ws = f"ws.tier-a.w{i % 5}"
        px, pct = (0, 0.0) if i >= nonzero else (12, worst_pct if i == 0 else 0.01)
        cps.append(AACheckpoint(f"census/monitor.panel-{i:03d}/{ws}", "frozen", px, pct))
    cps += [AACheckpoint(f"flow/trade.order-entry.stock/flow.starter-{k}/s1", "masked_live", 400, 0.8)
            for k in range(5)]
    return Dr05Evidence(tuple(cps))


def test_dr05_96_percent_zero_floor_frozen_and_a_max_of_0_05_percent_is_a():
    v = phase0.decide_dr05(calibration())
    assert v.branch == "A"
    assert v.evidence[1] == ("96 of 100 frozen checkpoints (96.0 percent) have zero diff pixels outside masks "
                             "(needs 95)")
    assert len(v.detail["fix_list"]) == 4


def test_dr05_one_checkpoint_at_0_2_percent_or_under_95_percent_zero_is_b():
    over = phase0.decide_dr05(calibration(worst_pct=0.2))
    assert over.branch == "B" and "largest frozen diff 0.2 percent on census/monitor.panel-000" in over.evidence[2]
    assert phase0.decide_dr05(calibration(zero_share=0.94)).branch == "B"
    assert phase0.decide_dr05(Dr05Evidence((), completed=False)).branch == "B"


def test_dr05_short_scope_is_undecided():
    small = Dr05Evidence(calibration().checkpoints[41:])           # 59 census checkpoints and the five flows
    assert phase0.decide_dr05(small).branch is None
    no_flows = Dr05Evidence(tuple(c for c in calibration().checkpoints if c.checkpoint_id.startswith("census/")))
    assert phase0.decide_dr05(no_flows).branch is None


# -- branches.yaml and the command ----------------------------------------------------------------------------------

UNDECIDED = (EXAMPLE_BRANCHES.read_text(encoding="utf-8")       # DR-04 in block style with a blank line after it
             .replace("pinned_install: {branch: A}   # DR-04 pipeline artifact with auto-update disabled",
                      "pinned_install:\n  branch: undecided\n  deadline: 2026-11-20\n")
             .replace("aa_floor: {branch: A}", "aa_floor: {branch: undecided, deadline: 2026-11-19}"))


def test_write_branch_records_the_decision_and_keeps_the_comments():
    assert "pinned_install:\n  branch: undecided" in UNDECIDED and "aa_floor: {branch: undecided" in UNDECIDED
    v = phase0.decide_dr05(calibration())
    out = phase0.write_branch(UNDECIDED, v, by="asmith", on=DAY)
    assert ("aa_floor: {branch: A, decided_by: asmith, decided_at: '2026-11-11'}         "
            "# DR-05 A/A floor near zero on frozen checkpoints") in out.splitlines()
    parsed = branches.parse(load_yaml_text(out))
    assert parsed.choices["aa_floor"].branch == "A" and parsed.choices["aa_floor"].decided_by == "asmith"
    pinned = phase0.decide_dr04(Dr04Evidence(True, reads(), title_regex=r"^thinkorswim (\d+)$"))
    out2 = phase0.write_branch(out, pinned, by="asmith", on=DAY)
    assert ("pinned_install: {branch: A, decided_by: asmith, decided_at: '2026-11-11', "
            "title_regex: ^thinkorswim (\\d+)$}") in out2.splitlines()
    assert out2.count("deadline: 2026-11-") == 0 and "\n\naa_floor:" in out2
    assert branches.parse(load_yaml_text(out2)).title_regex == r"^thinkorswim (\d+)$"


def test_write_branch_refuses_undecided_and_anonymous_verdicts():
    with pytest.raises(ValueError):
        phase0.write_branch(UNDECIDED, phase0.decide_dr02(Dr02Evidence(None)), by="asmith", on=DAY)
    with pytest.raises(ValueError):
        phase0.write_branch(UNDECIDED, phase0.decide_dr05(calibration()), by=" ", on=DAY)
    with pytest.raises(ValueError):
        phase0.write_branch("schema_version: 1\n", phase0.decide_dr05(calibration()), by="asmith", on=DAY)


def test_the_command_decides_and_writes_the_example_file(tmp_path, capsys):
    target = tmp_path / "branches.yaml"
    target.write_text(EXAMPLE_BRANCHES.read_text(encoding="utf-8"), encoding="utf-8")
    evidence = tmp_path / "dr05.yaml"
    evidence.write_text(yaml.safe_dump({"checkpoints": [c.__dict__ for c in calibration(worst_pct=0.2).checkpoints]}))
    code = phase0.main(["decide", "DR-05", "--evidence", str(evidence), "--branches", str(target), "--by", "asmith",
                        "--on", "2026-11-11"])
    assert code == 0
    assert capsys.readouterr().out.startswith("DR-05 aa_floor: branch B\n")
    assert branches.load(target).choices["aa_floor"].branch == "B"
    assert "# DR-05 A/A floor near zero on frozen checkpoints" in target.read_text(encoding="utf-8")


def test_the_command_reports_undecided_and_bad_input(tmp_path, capsys):
    evidence = tmp_path / "dr02.json"
    evidence.write_text(json.dumps({"written_yes": None}))
    assert phase0.main(["decide", "DR-02", "--evidence", str(evidence), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["branch"] is None
    assert phase0.main(["decide", "DR-02", "--evidence", str(tmp_path / "missing.yaml")]) == 2
    assert phase0.main(["decide", "DR-03", "--evidence", str(evidence)]) == 2
    capsys.readouterr()
    dr01 = tmp_path / "dr01.yaml"
    dr01.write_text(yaml.safe_dump({"module_present": True, "dumps": [
        {"surface": s, "dump_s": 0.8, "controls": [{"control_id": "c1", "name": "Buy", "role": "push button",
                                                     "jab_bounds": [10, 10, 40, 20],
                                                     "template_bounds": [11, 9, 40, 20]}]}
        for s in phase0.DR01_SURFACES]}))
    assert phase0.main(["decide", "DR-01", "--evidence", str(dr01)]) == 0
    assert capsys.readouterr().out.startswith("DR-01 jab: branch A")
    dr04 = tmp_path / "dr04.yaml"
    dr04.write_text(yaml.safe_dump({"artifact_delivered": True, "launches": [
        {"day": f"2026-11-0{2 + i // 3}", "build_id": "b", "jre_release": "j", "vmoptions_sha256": "v"}
        for i in range(5)]}))
    assert phase0.main(["decide", "DR-04", "--evidence", str(dr04)]) == 0
