"""Export and Copy All steps (4.7 export, 5.4.10, 5.10, SD-32): the menu clicks, the typed path and its guarded
Enter only under a clear flag, the timeout, the clipboard read then cleared, and what the Oracle makes of it."""
from __future__ import annotations

import pytest

from sentinel.executor.export import POLL_S, ExportResult, run_export
from sentinel.executor.primitives import plan
from sentinel.oracle import GroundTruthReader, evaluate_assertions
from sentinel.orchestrator.gateway import VK_RETURN, Flag, SafetyContext, slow_half
from sentinel.orchestrator.ports import ClipboardPort
from sentinel.records import StepOutcome

HEADERS = ["Symbol", "Qty", "P/L Open"]
CSV = "Symbol,Qty,P/L Open\nAAPL,100,\"1,150.00\"\n/MESZ26,1,-76.25\n"
FILE_STEP = {"id": "s09", "type": "export", "target": "monitor.position-statement.grid", "kind": "file",
             "path": "C:\\sentinel\\exports\\run-1\\ps.csv", "headers": HEADERS, "timeout_ms": 2000}
COPY_STEP = {"id": "s10", "type": "export", "target": "monitor.position-statement.grid", "kind": "copy_all",
             "headers": HEADERS, "timeout_ms": 1000}
MENU = ("monitor.position-statement.actions-menu", "monitor.position-statement.actions-menu.export-to-file")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Clipboard:
    def __init__(self, text: str | None = None):
        self.text, self.log = text, []

    def read_text(self) -> str | None:
        self.log.append("read")
        return self.text

    def clear(self) -> None:
        self.log.append("clear")
        self.text = None


class Files:
    def __init__(self, reads: list[str | None]):
        self.reads, self.paths = list(reads), []

    def read_text(self, path: str) -> str | None:
        self.paths.append(path)
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]


class Driver:
    """The executor's sub-step run: every sub-step goes through the real planner; outcomes can be scripted."""

    def __init__(self, outcomes: dict[str, str] | None = None, on: dict[str, object] | None = None):
        self.steps, self.actions, self.outcomes, self.on = [], [], outcomes or {}, on or {}

    def __call__(self, sub) -> str:
        self.steps.append(dict(sub))
        self.actions += plan(sub, {"target": (500, 300)}, Flag.CLEAR, 0.0)
        hook = self.on.get(sub["id"])
        if callable(hook):
            hook()
        return self.outcomes.get(sub["id"], "passed")


def export(step=FILE_STEP, *, files=None, clipboard=None, driver=None, flag=Flag.CLEAR, **kw) -> tuple:
    clock = Clock()
    files = files or Files([None, CSV, CSV])
    clipboard = clipboard or Clipboard()
    driver = driver or Driver()
    res = run_export(step, clipboard, files, drive=driver, flag=lambda: flag, clock=clock, sleep=clock.sleep,
                     menu=kw.pop("menu", MENU), **kw)
    return res, files, clipboard, driver, clock


def test_a_file_export_clicks_the_menu_types_the_path_with_a_guarded_enter_and_reads_the_file():
    res, files, _, driver, clock = export()
    assert res.status == "passed" and res.reason is None and res.produced and res.header_matches
    assert [s["id"] for s in driver.steps] == ["s09-menu-1", "s09-menu-2", "s09-path"]
    assert [s["target"] for s in driver.steps[:2]] == list(MENU)
    path_step = driver.steps[2]
    assert path_step == {"id": "s09-path", "type": "type_fixture", "value": FILE_STEP["path"], "commit": "enter"}
    typing = driver.actions[-1]
    assert typing.keys == (VK_RETURN,) and typing.declared_guarded                  # the explicit guarded Enter
    assert not slow_half(typing, SafetyContext(flag=Flag.CLEAR)).refused
    assert files.paths == [FILE_STEP["path"]] * 3 and clock.t == pytest.approx(2 * POLL_S)
    assert res.table.header == tuple(HEADERS) and res.source().column("Qty") == ("100", "1")


def test_the_file_chooser_enter_is_never_requested_while_the_flag_is_set():
    for flag in (Flag.SET, Flag.UNKNOWN):
        res, files, _, driver, _ = export(flag=flag)
        assert res.status == "failed" and res.reason == "export_unavailable"
        assert [s["id"] for s in driver.steps] == ["s09-menu-1", "s09-menu-2"] and files.paths == []
        assert not any(VK_RETURN in a.keys for a in driver.actions)


def test_no_file_within_the_timeout_is_export_timeout_and_its_assertions_are_oracle_unreadable():
    res, files, _, _, clock = export(files=Files([None]))
    assert res.status == "failed" and res.reason == "export_timeout" and not res.produced
    assert clock.t >= 2.0
    checkpoint = {"expect": [
        {"id": "pl_total", "kind": "export_equals", "region": "rgn.ps.pl-total", "export_column": "P/L Open"},
        {"id": "qty_total", "kind": "export_equals", "region": "rgn.ps.qty-total", "export_column": "Qty",
         "format": "int"},
        {"id": "header", "kind": "present", "region": "rgn.ps.header", "pattern": "Symbol"}]}
    reader = GroundTruthReader({"rgn.ps.pl-total": "1,073.75", "rgn.ps.qty-total": "101", "rgn.ps.header": "Symbol"})
    results = evaluate_assertions(checkpoint, {"ocr": reader, "export": res.source()}, {}, {})
    assert [(r.assertion_id, r.status) for r in results] == [("pl_total", "unreadable"), ("qty_total", "unreadable"),
                                                             ("header", "passed")]


def test_a_file_whose_header_differs_passes_the_step_and_is_a_harness_error_for_its_readers():
    wrong = CSV.replace("P/L Open", "P/L Day")
    res, *_ = export(files=Files([wrong, wrong]))
    assert res.status == "passed" and not res.header_matches
    (r,) = evaluate_assertions({"expect": [{"id": "pl", "kind": "export_equals", "region": "rgn.ps.pl-total",
                                            "export_column": "P/L Open"}]},
                               {"ocr": GroundTruthReader({"rgn.ps.pl-total": "1,073.75"}), "export": res.source()},
                               {}, {})
    assert (r.status, r.reason) == ("harness_error", "export_header_mismatch")


def test_a_file_is_read_once_it_stops_changing():
    partial = "Symbol,Qty,P/L Open\nAAPL,100"
    res, files, *_ = export(files=Files([partial, CSV, CSV]))
    assert res.text == CSV and len(res.table.rows) == 2


def test_copy_all_reads_the_clipboard_then_clears_it():
    clip = Clipboard("stale text from an earlier copy")
    driver = Driver(on={"s10-copy": lambda: setattr(clip, "text", CSV.replace(",", "\t"))})
    res, _, clip, driver, _ = export(COPY_STEP, clipboard=clip, driver=driver)
    assert res.status == "passed" and res.kind == "copy_all" and res.table.header == tuple(HEADERS)
    assert [s["id"] for s in driver.steps] == ["s10-focus", "s10-copy"]
    assert driver.steps[1]["keys"] == ["ctrl", "c"]
    assert clip.log[0] == "clear" and clip.log[-2:] == ["read", "clear"] and clip.text is None


def test_copy_all_with_nothing_on_the_clipboard_times_out_and_still_clears_it():
    res, _, clip, _, clock = export(COPY_STEP)
    assert res.status == "failed" and res.reason == "export_timeout" and clock.t >= 1.0
    assert clip.log[-1] == "clear" and not res.source().produced


def test_copy_all_never_requests_ctrl_c_while_the_flag_is_set():
    res, _, clip, driver, _ = export(COPY_STEP, flag=Flag.SET)
    assert res.reason == "export_unavailable" and [s["id"] for s in driver.steps] == ["s10-focus"]
    assert clip.log == ["clear", "clear"]


def test_a_failed_sub_step_is_export_unavailable_and_a_blocked_one_blocked_by_gateway():
    res, *_ = export(driver=Driver({"s09-menu-2": "failed"}))
    assert (res.status, res.reason) == ("failed", "export_unavailable")
    assert res.substeps == (("s09-menu-1", "passed"), ("s09-menu-2", "failed"))
    blocked, *_ = export(driver=Driver({"s09-path": "blocked_by_gateway"}))
    assert (blocked.status, blocked.reason) == ("blocked_by_gateway", None)
    rescued, *_ = export(driver=Driver({"s09-menu-1": "passed_via_agent_fallback"}))
    assert rescued.status == "passed"


def test_the_path_is_absolute_and_defaults_to_the_run_export_folder():
    step = {k: v for k, v in FILE_STEP.items() if k != "path"}
    res, files, *_ = export(step, export_dir="D:\\runs\\r1\\exports")
    assert res.path == "D:\\runs\\r1\\exports\\s09.csv" and files.paths[0] == res.path
    with pytest.raises(ValueError, match="absolute"):
        export({**FILE_STEP, "path": "exports\\ps.csv"})
    with pytest.raises(ValueError, match="export folder"):
        export(step)
    with pytest.raises(ValueError, match="menu"):
        export(menu=())


def test_step_shape_is_checked():
    with pytest.raises(ValueError, match="headers"):
        export({**FILE_STEP, "headers": []})
    with pytest.raises(ValueError, match="kind"):
        export({**FILE_STEP, "kind": "pdf"})
    with pytest.raises(ValueError):
        ExportResult("failed", "file", tuple(HEADERS), reason="timeout")


def test_copy_grid_is_copy_all_and_the_reason_reaches_the_step_record():
    res, *_ = export({k: v for k, v in COPY_STEP.items() if k != "kind"} | {"type": "copy_grid"})
    assert res.kind == "copy_all" and res.reason == "export_timeout"
    rec = StepOutcome("run-1", "flow.monitor.ps", "s10", **res.step_outcome_fields())
    assert (rec.status, rec.reason) == ("failed", "export_timeout")


def test_clipboard_fake_is_a_clipboard_port():
    assert isinstance(Clipboard(), ClipboardPort)
