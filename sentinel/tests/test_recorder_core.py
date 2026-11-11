"""The Recorder's core and anchor patches (5.16, 11.5, 12.1 Recorder and Anchor patch, 5.5), `sentinel.recorder.core`
and `sentinel.recorder.anchor_patch`."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from sentinel import synth
from sentinel.capture.redaction import SyntheticOcr
from sentinel.executor.locator import Branches
from sentinel.lint import schemas
from sentinel.lint.anchors import AnchorLintReport, AnchorMiss
from sentinel.lint.repo import Repo
from sentinel.lint.rules import lint
from sentinel.recorder import anchor_patch as ap
from sentinel.recorder import core
from sentinel.records import Finding

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
SURFACE = "trade.order-entry.stock"
FLOW = "flow.trade.order-ticket.stock.recorded"
TITLE = "Main@thinkorswim"


def layout(*, twins: bool = False) -> dict:
    kids = [{"type": "button", "name": "single", "label": "Single", "rect": [10, 10, 70, 22]},
            {"type": "button", "name": "oco", "label": "OCO", "rect": [90, 10, 60, 22]},
            {"type": "text", "name": "qty", "text": "Qty", "rect": [10, 50, 40, 16]},
            {"type": "button", "name": "confirm", "label": "Confirm and Send", "style": "primary",
             "rect": [300, 200, 170, 24]}]
    if twins:
        kids.append({"type": "button", "name": "single2", "label": "Single", "rect": [200, 10, 70, 22]})
    return {"type": "window", "name": "main", "title": TITLE, "children": [
        {"type": "panel", "name": "ticket", "title": "Order Entry", "rect": [20, 40, 520, 260], "children": kids}]}


@pytest.fixture
def scr() -> synth.Screen:
    return synth.render(layout(), (640, 400))


def center(s: synth.Screen, name: str) -> tuple[int, int]:
    x, y, w, h = s.get(name).bbox
    return x + w // 2, y + h // 2


def header(**kw) -> core.FlowHeader:
    base = dict(flow_id=FLOW, title="Recorded ticket flow", tier=1, owner="tos-trade-ui", persona="swing_trader",
                surface=SURFACE, workspace="ws.tier-a.swing-trader", fixture="fx.rig-01.paper")
    return core.FlowHeader(**{**base, **kw})


@pytest.fixture(scope="module")
def registry() -> dict:
    return {d.data["id"]: d.data for d in Repo.load(EXAMPLE).of("surface")}


def ticket_events(s: synth.Screen) -> list[core.RecordedEvent]:
    qx, qy, _, _ = s.get("qty").bbox
    return [core.RecordedEvent("checkpoint", surface_id=SURFACE, window_title=TITLE),
            core.RecordedEvent("click", "shot-1", center(s, "single"), TITLE, SURFACE),
            core.RecordedEvent("click", "shot-1", (qx + 60, qy + 7), TITLE, SURFACE, control_id="qty-field"),
            core.RecordedEvent("type", text="100", commit="tab", window_title=TITLE),
            core.RecordedEvent("checkpoint", surface_id=SURFACE, window_title=TITLE)]


# -- the flow ----------------------------------------------------------------------------------------------------

def test_an_event_log_becomes_an_anchored_flow_with_40x20_templates_at_the_click_points(scr, registry):
    rec = core.recorded_events_to_flow(ticket_events(scr), {"shot-1": scr.image}, header=header(),
                                       ocr=SyntheticOcr.from_screen(scr), existing=registry)
    assert rec.not_unique == () and rec.problems == () and rec.prompts == ()
    assert schemas.validate("flow", rec.doc) == []
    steps = rec.doc["steps"]
    assert [s["type"] for s in steps] == ["reset_input_state", "checkpoint", "click_named", "click_named",
                                          "type_fixture", "checkpoint"]
    assert steps[1]["checkpoint_id"] == f"flow/{SURFACE}/{FLOW}/s02" and steps[1]["regions"] == "default"
    assert steps[2]["target"] == f"{SURFACE}.single"
    assert steps[3]["target"] == steps[4]["target"] == f"{SURFACE}.qty-field"       # an existing control: no new anchor
    assert steps[4]["value"] == "100" and rec.doc["origin"] == {"kind": "recorder"}
    assert "agent_fallback_rate" not in yaml.safe_dump(rec.doc)                         # SD-55: derived, not written
    [control] = rec.controls[SURFACE]
    assert control["ocr_text"] == "Single" and "jab" not in control
    path = control["template"]
    assert path == f"flows/tier-1/templates/{FLOW}/dark-1920x1080-100/{SURFACE}.single.png" and list(rec.templates) == [path]
    tpl = cv2.cvtColor(cv2.imdecode(np.frombuffer(rec.templates[path], np.uint8), 1), cv2.COLOR_BGR2RGB)
    assert tpl.shape[:2] == (20, 40)
    meta = rec.steps[1]
    x, y, w, h = meta.anchor.rect
    assert np.array_equal(tpl, scr.image[y:y + h, x:x + w])
    cx, cy = x + w / 2 + control["click_offset"]["dx"], y + h / 2 + control["click_offset"]["dy"]
    assert abs(cx - center(scr, "single")[0]) <= 1 and abs(cy - center(scr, "single")[1]) <= 1
    assert meta.new_control and meta.window_title == TITLE and meta.anchor.source == "label"
    assert meta.search_region is not None and meta.search_region[2] > w


def test_a_template_not_unique_on_its_own_screenshot_is_listed_and_jab_kept_only_on_branch_a(registry):
    twins = synth.render(layout(twins=True), (640, 400))
    ev = [core.RecordedEvent("click", "shot-1", center(twins, "single"), TITLE, SURFACE,
                             jab={"name": "Single", "role": "push button"})]
    rec = core.recorded_events_to_flow(ev, {"shot-1": twins.image}, header=header(),
                                       ocr=SyntheticOcr.from_screen(twins), existing=registry)
    assert rec.not_unique == ("s02",)
    assert all(not s.unique for s in rec.steps[0].suggestions)
    assert "ocr_text" not in rec.controls[SURFACE][0]                                # the label reads twice
    one = synth.render(layout(), (640, 400))
    a = core.recorded_events_to_flow([ev[0]], {"shot-1": one.image}, header=header(), existing=registry,
                                     ocr=SyntheticOcr.from_screen(one), branches=Branches(jab="A"))
    assert a.controls[SURFACE][0]["jab"] == {"name": "Single", "role": "push button"}


def test_suggestions_put_the_label_first_and_score_on_the_screenshot(scr):
    s = core.suggest_anchors(scr.image, center(scr, "confirm"), SyntheticOcr.from_screen(scr).words(scr.image))
    assert s[0].source == "label" and s[0].label == "Confirm and Send" and s[0].unique
    assert "near" in {x.source for x in s} <= {"label", "point", "near"}   # a point on the label centre is one
    blank = np.full((100, 100, 3), 40, np.uint8)
    flat = core.suggest_anchors(blank, (50, 50))
    assert flat and not any(x.unique for x in flat)                                   # a flat crop matches anywhere


def test_secret_steps_record_only_the_vault_reference(scr, registry):
    sess = core.RecordingSession("authoring-01", login_titles=[r"^Logon"])
    x, y, w, h = scr.get("single").bbox
    sess.action(core.RecordedEvent("click", point=center(scr, "single"), window_title="Logon to thinkorswim",
                                   surface_id=SURFACE), scr.image)
    sess.focus("Logon to thinkorswim")
    assert sess.paused and sess.pause_reason == "login_dialog"
    for ch in "hunter2":
        assert not sess.key(ch)
    sess.secret("login.password", commit="enter")
    sess.focus(TITLE)
    rec = sess.recording()
    assert rec.keystrokes == [] and rec.secret_refs == ["login.password"] and rec.breaches == []
    flow = core.recorded_events_to_flow(rec.events, rec.screenshots, header=header(flow_class="login"),
                                        existing=registry, ocr=SyntheticOcr.from_screen(scr))
    step = flow.doc["steps"][-1]
    assert step == {"id": "s03", "type": "secret", "target": f"{SURFACE}.single", "vault_ref": "login.password",
                    "commit": "enter"}
    assert "hunter2" not in yaml.safe_dump(flow.doc)
    with pytest.raises(ValueError, match="vault reference"):
        core.RecordedEvent("secret")


def test_the_step_that_opens_an_order_dialog_gets_an_order_stage_prompt(scr, registry):
    ev = [core.RecordedEvent("checkpoint", surface_id=SURFACE, window_title=TITLE),
          core.RecordedEvent("click", "shot-1", center(scr, "confirm"), TITLE, SURFACE, control_id="confirm-and-send"),
          core.RecordedEvent("checkpoint", surface_id="dialog.order-confirmation", window_title="Order Confirmation",
                             checkpoint={"target_window": {"kind": "dialog", "title_regex": "^Order Confirmation"}})]
    rec = core.recorded_events_to_flow(ev, {"shot-1": scr.image}, header=header(), existing=registry,
                                       order_dialog_regexes=[r"^Order Confirmation"])
    assert [(p.step_id, p.title) for p in rec.prompts] == [("s03", "Order Confirmation")]
    assert "order_stage" in rec.prompts[0].text()
    assert rec.doc["surfaces"] == [SURFACE, "dialog.order-confirmation"]
    assert rec.answer({}) == list(rec.prompts) and "order_stage" not in rec.doc["steps"][2]
    assert rec.answer({"s03": True}) == [] and rec.doc["steps"][2]["order_stage"] is True


def test_ids_avoid_existing_controls_and_a_type_without_a_click_is_a_problem(scr, registry):
    reg = {SURFACE: dict(registry[SURFACE], controls=list(registry[SURFACE]["controls"]) +
                         [{"id": "single", "template": "x.png"}])}
    rec = core.recorded_events_to_flow([core.RecordedEvent("click", "shot-1", center(scr, "single"), TITLE, SURFACE),
                                        core.RecordedEvent("click", "shot-1", center(scr, "oco"), TITLE, SURFACE,
                                                           control_id="Bad Id")],
                                       {"shot-1": scr.image}, header=header(), existing=reg,
                                       ocr=SyntheticOcr.from_screen(scr))
    assert [c["id"] for c in rec.controls[SURFACE]] == ["single-2", "target"]
    bad = core.recorded_events_to_flow([core.RecordedEvent("type", text="1")], {}, header=header(), existing=reg)
    assert bad.problems == ("step s02: type with no focused field (no click before it)",)
    assert core.slug("Confirm and Send") == "confirm-and-send"


def test_keys_and_the_wheel_become_their_4_7_steps(scr, registry):
    ev = [core.RecordedEvent("press", key="escape"), core.RecordedEvent("key_chord", keys=("ctrl", "a")),
          core.RecordedEvent("wheel", "shot-1", center(scr, "single"), TITLE, SURFACE, notches=-2),
          core.RecordedEvent("wheel", notches=1)]
    rec = core.recorded_events_to_flow(ev, {"shot-1": scr.image}, header=header(), existing=registry,
                                       ocr=SyntheticOcr.from_screen(scr))
    assert rec.doc["steps"][1:] == [{"id": "s02", "type": "press", "key": "escape"},
                                    {"id": "s03", "type": "key_chord", "keys": ["ctrl", "a"]},
                                    {"id": "s04", "type": "wheel", "at": f"{SURFACE}.single", "notches": -2},
                                    {"id": "s05", "type": "wheel", "at": None, "notches": 1}]
    assert rec.problems == ("step s05: wheel with no point",)


# -- the session ------------------------------------------------------------------------------------------------

def test_the_recorder_refuses_a_rig_host():
    for host in ("tos-rig-01", "TOS-RIG-sim"):
        with pytest.raises(core.RecorderRefused, match="rig pattern"):
            core.RecordingSession(host)
    with pytest.raises(core.RecorderRefused):
        core.refuse_rig_host("lab-pc-7", rig_hostnames=["LAB-PC-7"])
    with pytest.raises(core.RecorderRefused, match="host name"):
        core.refuse_rig_host("")
    core.refuse_rig_host("authoring-01")


def test_a_paused_session_writes_no_keystroke_and_takes_no_screenshot(scr):
    sess = core.RecordingSession("authoring-01", branches=Branches(jab="A"))
    assert sess.key("a") and sess.action(core.RecordedEvent("click", point=(5, 5)), scr.image)
    sess.focus("Main@thinkorswim", role="password text")
    assert sess.pause_reason == "password_field"
    assert not sess.key("b") and not sess.action(core.RecordedEvent("click", point=(6, 6)), scr.image)
    sess.focus("Main@thinkorswim", role="text")
    assert not sess.paused
    sess.pause_hotkey()
    sess.focus("Main@thinkorswim", role="text")
    assert sess.pause_reason == "hotkey"                                    # only the hotkey ends a hotkey pause
    sess.pause_hotkey()
    rec = sess.recording()
    assert rec.keystrokes == ["a"] and list(rec.screenshots) == ["shot-0001"] and rec.breaches == []
    off = core.RecordingSession("authoring-01")                             # Branch B: no JAB role to pause on
    off.focus("Main@thinkorswim", role="password text")
    assert not off.paused
    with pytest.raises(core.RecorderRefused, match="paused"):
        off.secret("login.password")


def test_a_keystroke_log_that_grows_while_paused_is_a_breach_and_wipe_deletes_everything(scr):
    sess = core.RecordingSession("authoring-01", login_titles=[r"^Logon"])
    sess.action(core.RecordedEvent("click", point=(5, 5)), scr.image.copy())
    sess.focus("Logon to thinkorswim")
    sess.rec.keystrokes.append("x")                                         # a hook that wrote while paused
    sess.focus(TITLE)
    rec = sess.recording()
    assert rec.breaches == ["keystroke log grew by 1 while paused (login_dialog)"]
    shot = rec.screenshots["shot-0001"]
    rec.wipe()
    assert rec.events == [] and rec.screenshots == {} and rec.keystrokes == [] and not shot.any()


# -- anchor patches and re-anchor mode ---------------------------------------------------------------------------

LIMIT = "flow.trade.order-ticket.stock.limit"


@pytest.fixture
def cfg(tmp_path) -> Path:
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    return root


def _crop_png(scr: synth.Screen, name: str) -> bytes:
    cx, cy = center(scr, name)
    return bytes(cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(scr.image[cy - 10:cy + 10, cx - 20:cx + 20]),
                                                   cv2.COLOR_RGB2BGR))[1])


def rescued(png: bytes | None, step: str = "s04", target: str = f"{SURFACE}.qty-field") -> Finding:
    ev = {"step_id": step, "target": target, "final_target": {"x": 120, "y": 140} if png else None,
          "rung_results": [{"rung": "image", "result": "locator_failed(image)"}], "agent_trace": []}
    if png:
        sha = hashlib.sha256(png).hexdigest()
        ev.update(crop_sha256=sha, artifacts=[sha])
    return Finding(finding_id="fnd-1", run_id="run-1", kind="locator_failure", first_seen_run_id="run-1",
                   description=f"scripted locator broke at step {step}",
                   checkpoint_id=f"flow/{SURFACE}/{LIMIT}/s06", step_id=step, evidence=ev)


def test_an_anchor_patch_replaces_the_template_and_offset_of_exactly_one_steps_control(cfg, scr):
    flow = yaml.safe_load((cfg / f"flows/tier-1/{LIMIT}.yaml").read_text())
    png = _crop_png(scr, "qty")
    p = ap.anchor_patch(rescued(png), flow, cfg, crop_png=png, screenshot=scr.image)
    assert (p.flow_id, p.step_id, p.surface_id, p.control_id) == (LIMIT, "s04", SURFACE, "qty-field")
    sha = hashlib.sha256(png).hexdigest()
    assert p.new_template == f"registry/templates/trade/qty-field.{sha[:8]}.png" and p.new_offset == (0, 0)
    assert p.old_template == "registry/templates/trade/qty-field.png" and p.final_target == (120, 140)
    assert p.unique is True
    changed = [ln for ln in p.diff().splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert len(changed) == 2 and "qty-field" in changed[0] and "qty-field" in changed[1]
    assert "click_offset: {dx: 0, dy: 0}" in changed[1] and p.new_template in changed[1]
    assert sorted(p.apply(cfg)) == sorted([p.surface_path, p.new_template])
    assert (cfg / p.new_template).read_bytes() == png
    others = {s["target"] for s in flow["steps"] if s.get("target") and s["id"] != "s04"}
    reg = {d.data["id"]: d.data for d in Repo.load(cfg).of("surface")}
    for t in others:                                                        # every other control is untouched
        sid, cid = t.rsplit(".", 1)
        before = next(c for c in yaml.safe_load((EXAMPLE / f"registry/surfaces/{sid.split('.')[0]}/{sid}.yaml")
                                                 .read_text())["controls"] if c["id"] == cid)
        assert next(c for c in reg[sid]["controls"] if c["id"] == cid) == before
    assert lint(cfg, today=None).errors == []
    with pytest.raises(ValueError, match="changed since"):
        p.apply(cfg)


def test_no_patch_without_a_rescue_and_checks_on_the_finding(cfg, scr):
    flow = yaml.safe_load((cfg / f"flows/tier-1/{LIMIT}.yaml").read_text())
    assert ap.anchor_patch(rescued(None), flow, cfg) is None                # a failed ladder: re-record
    png = _crop_png(scr, "qty")
    with pytest.raises(ValueError, match="hash"):
        ap.anchor_patch(rescued(png), flow, cfg, crop_png=png + b"x")
    other = Finding(finding_id="f", run_id="r", kind="app_crash", first_seen_run_id="r", description="x")
    with pytest.raises(ValueError, match="locator_failure"):
        ap.anchor_patch(other, flow, cfg, crop_png=png)
    with pytest.raises(ValueError, match="not a registry control"):
        ap.anchor_patch(rescued(png, target="nope.control"), flow, cfg, crop_png=png)
    tiny = bytes(cv2.imencode(".png", np.zeros((10, 40, 3), np.uint8))[1])
    with pytest.raises(ValueError, match="not given"):
        ap.anchor_patch(rescued(tiny), flow, cfg)
    assert ap.anchor_patch(rescued(tiny), flow, cfg, crop_png=tiny) is None   # clipped below 16 px


def test_reanchor_mode_lists_rescued_steps_with_patches_and_lint_misses_to_rerecord(cfg, scr):
    flow = yaml.safe_load((cfg / f"flows/tier-1/{LIMIT}.yaml").read_text())
    png = _crop_png(scr, "qty")
    miss = AnchorMiss("control", f"flow/{SURFACE}/{LIMIT}/s06", f"{SURFACE}.price-field",
                      "registry/templates/trade/price-field.png", "tos-trade-ui", "below_floor", 0.4, 0.1, 0.92,
                      flow_id=LIMIT, step_id="s05")
    items = ap.reanchor_items([rescued(png)], {LIMIT: flow}, cfg, crops={hashlib.sha256(png).hexdigest(): png},
                              lint=AnchorLintReport((miss,), 1))
    assert [(i.step_id, i.action, i.source) for i in items] == [("s04", "accept_or_rerecord", "locator_failure"),
                                                                ("s05", "rerecord", "anchor_lint")]
    assert items[0].final_target == (120, 140) and items[0].template == "registry/templates/trade/qty-field.png"
