"""The Recorder's save-time checks (5.16 Failure outcomes, 11.5, 6.9, 6.12), `sentinel.recorder.save`."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sentinel import synth
from sentinel.capture.redaction import SyntheticOcr
from sentinel.lint.rules import lint
from sentinel.recorder import core, save
from sentinel.store.store import Store

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
SURFACE = "trade.order-entry.stock"
FLOW = "flow.trade.order-ticket.stock.recorded"
TITLE = "Main@thinkorswim"
SECRET = "Pa55word!"
SURFACE_FILE = f"registry/surfaces/trade/{SURFACE}.yaml"


def layout(*, twins: bool = False, shown: str | None = None) -> dict:
    kids = [{"type": "button", "name": "single", "label": "Single", "rect": [10, 10, 70, 22]},
            {"type": "button", "name": "oco", "label": "OCO", "rect": [90, 10, 60, 22]},
            {"type": "text", "name": "qty", "text": "Qty", "rect": [10, 50, 40, 16]},
            {"type": "button", "name": "confirm", "label": "Confirm and Send", "style": "primary",
             "rect": [300, 200, 170, 24]}]
    if twins:
        kids.append({"type": "button", "name": "single2", "label": "Single", "rect": [200, 10, 70, 22]})
    if shown:
        kids.append({"type": "text", "name": "leak", "text": shown, "rect": [10, 120, 120, 16]})
    return {"type": "window", "name": "main", "title": TITLE, "children": [
        {"type": "panel", "name": "ticket", "title": "Order Entry", "rect": [20, 40, 520, 260], "children": kids}]}


def screen(**kw) -> synth.Screen:
    return synth.render(layout(**kw), (640, 400))


def center(s: synth.Screen, name: str) -> tuple[int, int]:
    x, y, w, h = s.get(name).bbox
    return x + w // 2, y + h // 2


HEADER = core.FlowHeader(
    flow_id=FLOW, title="Recorded ticket flow", tier=1, owner="tos-trade-ui", persona="swing_trader", surface=SURFACE,
    workspace="ws.tier-a.swing-trader", fixture="fx.rig-01.paper",
    preconditions=({"id": "p1", "type": "precondition", "check": "paper_mode"},
                   {"id": "p2", "type": "precondition", "check": "workspace_loaded", "expect": "ws.tier-a.swing-trader"},
                   {"id": "p3", "type": "precondition", "check": "no_modal"},
                   {"id": "p4", "type": "precondition", "check": "working_orders", "expect": 0}),
    teardown=({"id": "t1", "type": "teardown", "action": "clear_ticket"},
              {"id": "t2", "type": "teardown", "action": "working_orders_check", "expect": 0},
              {"id": "t3", "type": "reset_input_state"}))


def record(s: synth.Screen, *, host: str = "authoring-01", typed: str = "100") -> core.Recording:
    """The owner clicks Single, then the Qty field (an existing control), types and checkpoints (2.6.3: the ticket's
    pre-input checkpoint comes first)."""
    sess = core.RecordingSession(host)
    qx, qy, _, _ = s.get("qty").bbox
    sess.action(core.RecordedEvent("checkpoint", surface_id=SURFACE, window_title=TITLE))
    sess.action(core.RecordedEvent("click", point=center(s, "single"), window_title=TITLE, surface_id=SURFACE), s.image)
    sess.action(core.RecordedEvent("click", point=(qx + 60, qy + 7), window_title=TITLE, surface_id=SURFACE,
                                   control_id="qty-field"), s.image)
    for ch in typed:
        sess.key(ch)
    sess.action(core.RecordedEvent("type", text=typed, commit="tab", window_title=TITLE))
    sess.action(core.RecordedEvent("checkpoint", surface_id=SURFACE, window_title=TITLE))
    return sess.recording()


@pytest.fixture
def cfg(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def emit(rec: core.Recording, cfg: Path, s: synth.Screen, **kw) -> save.SaveResult:
    kw.setdefault("lease", lambda refs: {r: SECRET for r in refs})
    return save.check_and_emit(rec, kw.pop("header", HEADER), config_root=cfg, ocr=SyntheticOcr.from_screen(s), **kw)


def test_a_clean_recording_is_saved_and_its_output_passes_the_lint(cfg):
    s = screen()
    r = emit(record(s), cfg, s)
    assert r.status == "saved" and r.violations == () and r.incident is None
    tpl = f"flows/tier-1/templates/{FLOW}/dark-1920x1080-100/{SURFACE}.single.png"
    assert r.written == tuple(sorted((f"flows/tier-1/{FLOW}.yaml", tpl, SURFACE_FILE)))
    doc = yaml.safe_load((cfg / f"flows/tier-1/{FLOW}.yaml").read_text())
    assert doc["origin"] == {"kind": "recorder"} and doc["steps"][2]["target"] == f"{SURFACE}.single"
    text = (cfg / SURFACE_FILE).read_text()
    assert text.startswith((EXAMPLE / SURFACE_FILE).read_text().split("controls:")[0])      # the head is kept
    assert f"- {{id: single, template: {tpl}, ocr_text: Single" in text
    assert (cfg / tpl).read_bytes()[:4] == b"\x89PNG"
    assert lint(cfg).errors == []


def test_a_template_not_unique_on_its_own_screenshot_rejects_the_save(cfg):
    s = screen(twins=True)
    before = (cfg / SURFACE_FILE).read_text()
    r = emit(record(s), cfg, s)
    assert r.status == "rejected" and "step s03: its template is not unique" in r.reasons[0]
    assert not (cfg / f"flows/tier-1/{FLOW}.yaml").exists() and (cfg / SURFACE_FILE).read_text() == before


def test_the_leased_secret_in_the_keystroke_log_deletes_the_recording_as_an_s1_incident(cfg, tmp_path):
    s = screen()
    rec = record(s, typed=SECRET)
    rec.secret_refs.append("login.password")
    with Store(tmp_path / "central.db") as st:
        r = emit(rec, cfg, s, store=st, at="2026-11-10T15:00:00Z")
        row = st.query("SELECT severity, trigger, run_id FROM incidents")[0]
    assert r.status == "deleted" and r.incident is not None and r.incident.incident == "S1"
    assert r.incident.actions == ("rotate_credential",) and r.incident.alert.is_safety
    assert r.incident.evidence["rotate"] == ["login.password"]
    assert {f.kind for f in r.incident.findings} == {"secret_leaked"} and r.incident.findings[0].severity == "S1"
    assert {h["location"] for h in r.incident.evidence["hits"]} >= {"keystroke log"}
    assert SECRET not in repr(r) and SECRET not in str(r.incident.evidence)
    assert (row["severity"], row["run_id"]) == ("S1", None) and row["trigger"].startswith("recorder: plaintext of")
    assert r.incident_id is not None
    assert rec.events == [] and rec.keystrokes == [] and rec.screenshots == {}                # deleted unsaved
    assert not (cfg / f"flows/tier-1/{FLOW}.yaml").exists()


def test_the_secret_read_back_by_ocr_from_a_screenshot_deletes_the_recording(cfg):
    s = screen(shown=SECRET)
    rec = record(s)
    rec.secret_refs.append("login.password")
    secret = bytearray(SECRET.encode())
    r = emit(rec, cfg, s, lease=lambda refs: {"login.password": secret})
    assert r.status == "deleted"
    assert sorted(h["location"] for h in r.incident.evidence["hits"]) == ["screenshot shot-0001 (OCR)",
                                                                          "screenshot shot-0002 (OCR)"]
    assert not any(secret)                                                    # the leased buffer is zeroed


def test_a_keystroke_captured_while_paused_deletes_the_recording(cfg):
    s = screen()
    rec = record(s)
    rec.breaches.append("keystroke log grew by 1 while paused (hotkey)")
    r = emit(rec, cfg, s)
    assert r.status == "deleted" and r.incident.incident == "S1"
    assert "keystrokes were captured while the Recorder was paused" in r.incident.findings[0].description


def test_a_recording_from_a_rig_host_is_refused_and_a_failed_lease_keeps_the_recording(cfg):
    s = screen()
    rec = record(s)
    rec.host = "tos-rig-02"
    assert emit(rec, cfg, s).status == "refused"
    rec.host = "authoring-01"
    assert emit(rec, cfg, s, rig_hostnames=["authoring-01"]).status == "refused"
    rec.secret_refs.append("login.password")

    def down(refs):
        raise ConnectionError("vault unreachable")
    r = emit(rec, cfg, s, lease=down)
    assert r.status == "refused" and r.reasons[0].startswith("secret_scan_unavailable") and rec.events
    partial = bytearray(b"other-secret")
    assert emit(rec, cfg, s, lease=lambda refs: {"login.pin": partial}).status == "refused"
    assert not any(partial)                                                   # a partial lease is zeroed too


def test_an_order_dialog_title_needs_the_owners_order_stage_answer(cfg):
    s = screen()
    sess = core.RecordingSession("authoring-01")
    sess.action(core.RecordedEvent("checkpoint", surface_id=SURFACE, window_title=TITLE))
    sess.action(core.RecordedEvent("click", point=center(s, "confirm"), window_title=TITLE, surface_id=SURFACE,
                                   control_id="confirm-and-send"), s.image)
    sess.action(core.RecordedEvent("checkpoint", surface_id="dialog.order-confirmation",
                                   window_title="Order Confirmation",
                                   checkpoint={"target_window": {"kind": "dialog", "title_regex": "^Order Confirmation"}}))
    rec = sess.recording()
    hdr = core.FlowHeader(**{**HEADER.__dict__, "teardown": (
        {"id": "t1", "type": "teardown", "action": "close_dialog", "target": "dialog.order-confirmation.title-close"},
        {"id": "t2", "type": "teardown", "action": "clear_ticket"},
        {"id": "t3", "type": "teardown", "action": "working_orders_check", "expect": 0},
        {"id": "t4", "type": "reset_input_state"})})
    r = emit(rec, cfg, s, header=hdr, order_dialog_regexes=[r"^Order Confirmation"])
    assert r.status == "needs_answer" and "step s03 opened 'Order Confirmation'" in r.reasons[0]
    no = emit(rec, cfg, s, header=hdr, order_dialog_regexes=[r"^Order Confirmation"], order_stage={"s03": False})
    assert no.status == "lint_failed" and any("without order_stage: true" in v for v in no.violations)
    yes = emit(rec, cfg, s, header=hdr, order_dialog_regexes=[r"^Order Confirmation"], order_stage={"s03": True})
    assert yes.flow.doc["steps"][2]["order_stage"] is True
    assert not any("without order_stage" in v for v in yes.violations)


def test_a_recording_that_fails_the_lint_writes_nothing(cfg):
    s = screen()
    hdr = core.FlowHeader(**{**HEADER.__dict__, "preconditions": ({"id": "p1", "type": "precondition",
                                                                    "check": "paper_mode"},)})
    r = emit(record(s), cfg, s, header=hdr)
    assert r.status == "lint_failed" and any("L-FLW-12" in v for v in r.violations)
    assert not (cfg / f"flows/tier-1/{FLOW}.yaml").exists()


def test_controls_are_appended_to_the_controls_block_and_comments_are_kept():
    text = "id: a.b\ncontrols:  # the controls\n  - {id: x, template: t.png}\n  # a comment\nsettle: {max_wait_ms: 1}\n"
    out = save.add_controls(text, [{"id": "y", "template": "u.png", "click_offset": {"dx": 1, "dy": 0}}])
    assert out == ("id: a.b\ncontrols:  # the controls\n  - {id: x, template: t.png}\n"
                   "  - {id: y, template: u.png, click_offset: {dx: 1, dy: 0}}\n  # a comment\nsettle: {max_wait_ms: 1}\n")
    assert save.add_controls("id: a.b", [{"id": "y", "template": "u.png"}]) == \
        "id: a.b\ncontrols:\n  - {id: y, template: u.png}\n"
    assert yaml.safe_load(out)["controls"][1]["click_offset"] == {"dx": 1, "dy": 0}
