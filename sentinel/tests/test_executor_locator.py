"""The locator ladder (5.5): JAB, image anchor and OCR rungs on synthetic screens, the retry, the agent rung's gate,
the rescue cap and the locator_failure finding."""
from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from sentinel import synth
from sentinel.capture.redaction import OcrWord
from sentinel.diff.regions import Rect
from sentinel.executor.locator import (OCR_UPSCALE, Branches, Ladder, LadderContext, RescueCap, RescueOutcome,
                                       RescueRequest, WindowCapture, agent_disabled, ladder, prepare_ocr,
                                       target_from_control)
from sentinel.jabtree import UNREAD
from sentinel.orchestrator.gateway import Flag
from sentinel.orchestrator.ports import AccessNode, Frame, Locator
from sentinel.records import StepOutcome

ORIGIN = (1000, 500)                                  # the window's position on the virtual screen
LAYOUT = {"type": "window", "name": "w", "children": [
    {"type": "button", "name": "b1", "label": "Delete", "rect": [20, 20, 90, 24]},
    {"type": "button", "name": "b2", "label": "Delete", "rect": [200, 20, 90, 24]},
    {"type": "text", "name": "qty", "text": "Qty", "rect": [20, 80, 60, 16]},
    {"type": "text", "name": "price", "text": "Price", "rect": [120, 80, 60, 16]},
    {"type": "button", "name": "send", "label": "Confirm and Send", "style": "primary", "rect": [180, 104, 130, 24]}]}
SCREEN = synth.render(LAYOUT, (320, 140))
BLANK = np.full_like(SCREEN.image, 14)
A_ON = Branches("A", "A")


def tpl(name: str, pad: int = 2) -> np.ndarray:
    return SCREEN.crop(name, pad=pad)


TEMPLATES = {"anchors/send.png": tpl("send.label"), "anchors/delete.png": tpl("b1"), "anchors/qty.png": tpl("qty")}


class TemplateOcr:
    """A `WordOcr` stand-in: finds each text `sentinel.synth` drew in the (3x, inverted) image it is given by
    matching that text's own crop prepared the same way, so it works on any region without knowing it."""

    def __init__(self, names=("b1.label", "qty", "price")):
        self.items = []
        for n in names:
            x, y, w, h = SCREEN.get(n).bbox
            crop = np.ascontiguousarray(SCREEN.image[y - 1:y + h + 1, x - 1:x + w + 1])
            self.items.append((SCREEN.get(n).text, prepare_ocr(crop, Rect(0, 0, w + 2, h + 2))))
        self.calls: list[np.ndarray] = []

    def words(self, image):
        self.calls.append(image)
        out = []
        for text, t in self.items:
            th, tw = t.shape[:2]
            if th > image.shape[0] or tw > image.shape[1]:
                continue
            res = cv2.matchTemplate(np.ascontiguousarray(image), t, cv2.TM_CCOEFF_NORMED)
            while True:
                _, best, _, (x, y) = cv2.minMaxLoc(res)
                if best < 0.9:
                    break
                out.append(OcrWord(text, Rect(int(x), int(y), tw, th)))
                res[max(0, y - th // 2):y + th // 2 + 1, max(0, x - tw // 2):x + tw // 2 + 1] = 0
        return out


class FakeJab:
    def __init__(self, root: AccessNode | None, up: bool = True):
        self.root, self.up = root, up

    def available(self) -> bool:
        return self.up

    def tree(self, hwnd: int):
        return self.root

    def focus_owner(self, hwnd: int):
        return None


class Rescuer:
    def __init__(self, outcome: RescueOutcome):
        self.outcome = outcome
        self.requests: list[RescueRequest] = []

    def rescue(self, request: RescueRequest) -> RescueOutcome:
        self.requests.append(request)
        return self.outcome


def capture(image=None, sha="sha-first", regions=None) -> WindowCapture:
    return WindowCapture(SCREEN.image if image is None else image, ORIGIN, regions or {}, sha, hwnd=7)


def ctx(*, retry_image=None, rescuer=None, flag=Flag.CLEAR, cap=None, **kw) -> LadderContext:
    calls = []

    def retry():
        calls.append(1)
        return capture(retry_image, sha="sha-retry")

    c = LadderContext("s05", kw.pop("step_type", "click_named"), cap or RescueCap(), retry, lambda: flag,
                      templates=TEMPLATES, rescuer=rescuer, **kw)
    c.retry_calls = calls                                     # type: ignore[attr-defined]
    return c


def send_target(**kw):
    return {"name": "dialog.ticket.send", "image": {"template": "anchors/send.png", "offset": [0, 0], **kw}}


# -- rung 2 ------------------------------------------------------------------------------------------------
def test_unique_template_is_accepted_on_rung_2_with_offset_in_screen_pixels():
    target = send_target(offset=[10, -2])
    r = ladder(target, capture(), None, None, A_ON, ctx())
    assert r.status == "passed" and r.rung == "image" and r.attempts == 1
    x, y, w, h = SCREEN.get("send.label").bbox
    t = tpl("send.label")
    cx, cy = x - 2 + t.shape[1] / 2, y - 2 + t.shape[0] / 2
    assert r.point == (round(ORIGIN[0] + cx + 10), round(ORIGIN[1] + cy - 2))
    (img,) = r.rung_results
    assert img.score >= 0.99 and img.evidence["second"] <= img.score - 0.05
    assert img.crop.shape[:2] == (20, 40)                    # the 40x20 crop at the match
    assert r.consequence is None and r.finding is None


def test_below_the_floor_is_locator_failed_image():
    r = ladder(send_target(), capture(BLANK), None, None, A_ON, ctx(retry_image=BLANK))
    assert [x.result for x in r.rung_results][:2] == ["locator_failed(image)", "locator_failed(image)"]
    assert r.rung_results[0].evidence["cause"] == "below_floor" and r.rung_results[0].score < 0.92


def test_a_second_peak_within_005_is_ambiguous_and_falls_to_ocr():
    target = {"name": "ticket.delete", "image": {"template": "anchors/delete.png", "offset": [0, 0]},
              "ocr": {"text": "Delete", "region": "left", "offset": [0, 0]}}
    ocr = TemplateOcr()
    r = ladder(target, capture(regions={"left": Rect(0, 0, 160, 60)}), ocr, None, A_ON, ctx())
    img, text = r.rung_results
    assert img.result == "locator_ambiguous(image)" and img.evidence["second"] > img.score - 0.05
    assert text.result == "accepted" and r.rung == "ocr" and r.status == "passed"
    lx, ly, lw, lh = SCREEN.get("b1.label").bbox
    assert abs(r.point[0] - (ORIGIN[0] + lx + lw / 2)) <= 2 and abs(r.point[1] - (ORIGIN[1] + ly + lh / 2)) <= 2
    assert text.evidence["words"]                              # the word boxes considered


def test_two_ocr_matches_are_locator_ambiguous_ocr():
    target = {"name": "ticket.delete", "ocr": {"text": "delete"}}
    r = ladder(target, capture(), TemplateOcr(), None, A_ON, ctx())
    assert r.rung_results[0].result == "locator_ambiguous(ocr)"
    assert sum(1 for w in r.rung_results[0].evidence["words"] if w["ratio"] >= 0.9) == 2


def test_ocr_reads_the_region_upscaled_3x_and_inverted():
    region = Rect(10, 70, 100, 40)
    prepared = prepare_ocr(SCREEN.image, region)
    assert prepared.shape[:2] == (40 * OCR_UPSCALE, 100 * OCR_UPSCALE)
    small = cv2.resize(prepared, (100, 40), interpolation=cv2.INTER_AREA)
    assert np.abs(small.astype(int) - (255 - SCREEN.image[70:110, 10:110].astype(int))).mean() < 8
    ocr = TemplateOcr()
    r = ladder({"name": "t.qty", "ocr": {"text": "Qty", "region": "r", "offset": [30, 0]}},
               capture(regions={"r": region}), ocr, None, A_ON, ctx())
    assert ocr.calls[0].shape[:2] == (40 * OCR_UPSCALE, 100 * OCR_UPSCALE)
    x, y, w, h = SCREEN.get("qty").bbox
    assert r.rung == "ocr" and abs(r.point[0] - (ORIGIN[0] + x + w / 2 + 30)) <= 2


def test_fuzzy_ocr_and_multi_word_targets():
    r = ladder({"name": "t", "ocr": {"text": "Prise", "fuzzy": 0.75}}, capture(), TemplateOcr(), None, A_ON, ctx())
    assert r.rung == "ocr" and r.rung_results[0].score < 1.0
    two = TemplateOcr(("qty", "price"))
    r2 = ladder({"name": "t", "ocr": {"text": "Qty Price"}}, capture(), two, None, A_ON, ctx())
    assert r2.rung == "ocr"


def test_a_declared_region_the_capture_cannot_resolve_fails_the_rung():
    r = ladder(send_target(region="nowhere"), capture(), None, None, A_ON, ctx(retry_image=BLANK))
    assert r.rung_results[0].evidence["cause"] == "region_unresolved"


# -- rung 1 ------------------------------------------------------------------------------------------------
def jab_tree(*nodes: AccessNode) -> AccessNode:
    return AccessNode("frame", "Main", (ORIGIN[0], ORIGIN[1], 320, 140), frozenset({"visible"}), (
        AccessNode("panel", "OrderEntryPanel", (ORIGIN[0], ORIGIN[1], 320, 140), frozenset({"visible"}), (
            AccessNode("panel", "Fields", None, frozenset({"visible"}), nodes),)),))


QTY_NODE = AccessNode("text", "Qty", (ORIGIN[0] + 80, ORIGIN[1] + 76, 40, 20), frozenset({"visible", "showing"}))
JAB_TARGET = {"name": "t.qty", "jab": {"role": "text", "name": "Qty", "path": "OrderEntryPanel/.../Qty"},
              "image": {"template": "anchors/send.png"}}


def test_jab_rung_accepts_a_unique_visible_context_inside_the_window():
    r = ladder(JAB_TARGET, capture(), None, FakeJab(jab_tree(QTY_NODE)), A_ON, ctx())
    assert r.rung == "jab" and r.point == (ORIGIN[0] + 100, ORIGIN[1] + 86) and len(r.rung_results) == 1


@pytest.mark.parametrize("nodes,up,cause", [
    ((), True, "not_found"),
    ((QTY_NODE, QTY_NODE), True, "ambiguous"),
    ((AccessNode("text", "Qty", None, frozenset({"visible"})),), True, "stale"),
    ((AccessNode("text", "Qty", (ORIGIN[0] + 80, ORIGIN[1] + 76, 40, 20)),), True, "invisible"),
    ((AccessNode("text", "Qty", (ORIGIN[0] + 300, ORIGIN[1] + 76, 40, 20), frozenset({"visible"})),), True,
     "outside_window"),
    ((QTY_NODE,), False, "bridge_unavailable"),
])
def test_jab_rung_failures_fall_through_to_the_image_rung(nodes, up, cause):
    r = ladder(JAB_TARGET, capture(), None, FakeJab(jab_tree(*nodes), up), A_ON, ctx())
    first, second = r.rung_results[:2]
    assert first.result == "locator_failed(jab)" and first.evidence["cause"] == cause
    assert second.rung == "image" and r.rung == "image"


@pytest.mark.parametrize("tree", [
    jab_tree(QTY_NODE, UNREAD),                                         # the unread sibling may be its twin
    AccessNode("frame", "Main", (ORIGIN[0], ORIGIN[1], 320, 140), frozenset({"visible"}),
               (jab_tree(QTY_NODE).children[0], UNREAD)),               # or hold one in its unread subtree
])
def test_a_jab_match_is_not_unique_when_the_walk_lost_a_context(tree):
    """K2 (5.5 "found, unique", jabtree choice 5, fail closed): a context the bridge could not read may be the
    target's twin or hold it, so one match in a tree with an UNREAD placeholder is not proven unique."""
    r = ladder(JAB_TARGET, capture(), None, FakeJab(tree), A_ON, ctx())
    first = r.rung_results[0]
    assert first.result == "locator_failed(jab)" and first.evidence == {"cause": "incomplete_tree", "unread": 1}
    assert r.rung == "image"


def test_branch_b_records_rung1_skipped():
    r = ladder(JAB_TARGET, capture(), None, FakeJab(jab_tree(QTY_NODE)), Branches("B", "A"), ctx())
    assert [x.to_record()["result"] for x in r.rung_results] == ["rung1_skipped", "accepted"]
    assert r.rung == "image"
    undecided = ladder(JAB_TARGET, capture(), None, None, {"jab": {"branch": "undecided"}}, ctx())
    assert undecided.rung_results[0].result == "rung1_skipped"


# -- retry, rung 4 and the cap ------------------------------------------------------------------------------
def test_success_on_the_retry_is_passed_after_retry_and_keeps_the_first_screenshot():
    c = ctx(retry_image=SCREEN.image)
    r = ladder(send_target(), capture(BLANK), None, None, A_ON, c)
    assert r.status == "passed_after_retry" and r.attempts == 2 and r.pre_screenshot == "sha-first"
    assert c.retry_calls == [1]
    assert [(x.attempt, x.result) for x in r.rung_results] == [(1, "locator_failed(image)"), (2, "accepted")]


def exhausted(branches=A_ON, **kw):
    """A ladder whose scripted rungs fail on both attempts."""
    c = ctx(retry_image=BLANK, **kw)
    return ladder(send_target(), capture(BLANK), None, None, branches, c), c


@pytest.mark.parametrize("change,cause", [
    ({"target": {"agent_fallback": False}}, "target_forbids"),
    ({"flow_agent_fallback": "disabled"}, "flow_forbids"),
    ({"step_type": "secret"}, "secret_step"),
    ({"flow_class": "login"}, "login_flow"),
    ({"teardown": True}, "teardown_step"),
    ({"post_login": False}, "before_post_login"),
    ({"agent_available": False}, "agent_unavailable"),
    ({"branches": Branches("A", "B")}, "computer_use_branch_B"),
    ({"branches": Branches("A", "undecided")}, "computer_use_branch_undecided"),
    ({"rescuer": None}, "no_rescuer"),
    ({"flag": Flag.SET}, "flag_not_clear"),
    ({"flag": Flag.UNKNOWN}, "flag_not_clear"),
    ({"cap": RescueCap(max_steps=0)}, "rescue_cap_spent"),
])
def test_every_disabling_condition_skips_rung_4(change, cause):
    rescuer = Rescuer(RescueOutcome(True, point=(1100, 520)))
    branches = change.pop("branches", A_ON)
    target = {**send_target(), **change.pop("target", {})}
    kw = {"rescuer": rescuer, **change}
    c = ctx(retry_image=BLANK, **kw)
    r = ladder(target, capture(BLANK), None, None, branches, c)
    agent = r.rung_results[-1]
    assert agent.result == "agent_failed(disabled)" and agent.evidence["cause"] == cause
    assert r.status == "failed" and rescuer.requests == [] and c.cap.used == 0


def test_an_exhausted_ladder_fails_the_step_and_the_checkpoint():
    r, _ = exhausted(rescuer=None)
    assert r.status == "failed" and r.rung == "none" and r.attempts == 2
    res = r.consequence
    assert res.step == "failed" and res.checkpoint.outcome == "landmark_missing"
    assert res.checkpoint.reason == "locator:s05" and res.has("end_flow")
    (rest,) = res.outcomes
    assert rest.selector == "rest_of_flow" and rest.outcome.outcome == "not_run" and rest.outcome.reason == "step_failed:s05"
    assert r.finding.kind == "locator_failure" and "s05" in r.finding.description
    assert [x["result"] for x in r.evidence["rung_results"]] == [
        "locator_failed(image)", "locator_failed(image)", "agent_failed(disabled)"]


def test_a_rescue_is_passed_via_agent_fallback_with_its_finding():
    rescuer = Rescuer(RescueOutcome(True, point=(ORIGIN[0] + 245, ORIGIN[1] + 116), actions=4, seconds=12.0,
                                    trace=({"tool": "click_named", "name": "Confirm and Send", "why": "label moved"},)))
    c = ctx(retry_image=BLANK, rescuer=rescuer)
    r = ladder(send_target(), capture(BLANK), None, None, A_ON, c)
    assert r.status == "passed_via_agent_fallback" and r.rung == "agent" and r.rescued
    (req,) = rescuer.requests
    assert (req.step_id, req.target, req.max_actions, req.max_seconds, req.pre_screenshot) == (
        "s05", "dialog.ticket.send", 25, 90.0, "sha-first")
    assert c.cap.used == 1
    assert r.finding.kind == "locator_failure" and r.finding.description == "scripted locator broke at step s05"
    ev = r.evidence
    assert ev["final_target"] == {"x": ORIGIN[0] + 245, "y": ORIGIN[1] + 116}
    assert [x.get("score") is not None for x in ev["rung_results"][:2]] == [True, True]   # every rung's score
    assert ev["pre_screenshot"] == "sha-first" and ev["agent_trace"][0]["why"] == "label moved"
    assert ev["crop_sha256"] == hashlib.sha256(r.crop_png).hexdigest() and ev["artifacts"] == [ev["crop_sha256"]]
    crop = cv2.imdecode(np.frombuffer(r.crop_png, np.uint8), cv2.IMREAD_UNCHANGED)
    assert crop.shape[:2] == (20, 40)
    assert r.to_locate_result().finding_kind == "locator_failure"


@pytest.mark.parametrize("outcome,code", [
    (RescueOutcome(False, "refusal"), "agent_failed(refusal)"),
    (RescueOutcome(False, "abort_flag"), "agent_failed(abort_flag)"),
    (RescueOutcome(True, point=(1100, 520), actions=26), "agent_failed(cap)"),
    (RescueOutcome(True, point=(1100, 520), seconds=90.5), "agent_failed(cap)"),
])
def test_a_failed_or_overlong_rescue_fails_the_step(outcome, code):
    r, c = exhausted(rescuer=Rescuer(outcome))
    assert r.rung_results[-1].result == code and r.status == "failed"
    assert r.consequence.checkpoint.reason == "locator:s05" and c.cap.used == 1


def test_the_26th_rescue_is_disabled_and_the_cap_is_a_run_health_line():
    cap = RescueCap()
    rescuer = Rescuer(RescueOutcome(True, point=(1100, 520)))
    for _ in range(25):
        r, _c = exhausted(rescuer=rescuer, cap=cap)
        assert r.status == "passed_via_agent_fallback"
    assert cap.used == 25 and len(rescuer.requests) == 25
    r, _c = exhausted(rescuer=rescuer, cap=cap)
    assert r.rung_results[-1].evidence["cause"] == "rescue_cap_spent" and r.status == "failed"
    assert len(rescuer.requests) == 25
    assert r.health_line == "agent rescue cap reached after 25 steps; later locator failures are failed"


def test_rescue_cap_reads_policy_budgets():
    cap = RescueCap.from_budgets({"rescue": {"max_actions": 10, "max_seconds": 30, "max_rescued_steps_per_run": 3}})
    assert (cap.max_steps, cap.max_actions, cap.max_seconds) == (3, 10, 30.0)
    assert RescueCap.from_budgets({}) == RescueCap()
    assert RescueCap().health_line() is None


def test_rescue_outcomes_use_the_closed_failure_codes():
    with pytest.raises(ValueError):
        RescueOutcome(False, "gave_up")
    with pytest.raises(ValueError):
        RescueOutcome(True)


# -- records and the protocol --------------------------------------------------------------------------------
def test_step_outcome_fields_make_a_valid_step_record():
    r, _ = exhausted(rescuer=None)
    rec = StepOutcome("run-1", "flow.x", "s05", **r.step_outcome_fields())
    assert rec.locator_rung == "none" and rec.attempt == 2 and rec.pre_screenshot == "sha-first"
    ok = ladder(send_target(), capture(), None, None, A_ON, ctx())
    assert StepOutcome("run-1", "flow.x", "s05", **ok.step_outcome_fields()).locator_rung == "image"


def test_ladder_is_a_locator_over_a_frame():
    lad = Ladder(None, None, A_ON, ctx(), regions={})
    assert isinstance(lad, Locator)
    res = lad.locate(send_target(), Frame(SCREEN.image, ORIGIN, 1.0))
    assert res.rung == "image" and res.point is not None and res.rung_results[0]["result"] == "accepted"
    assert lad.last is not None and lad.last.status == "passed"


def test_a_single_attempt_ladder_misses_once_without_retry_rescue_or_finding():
    """Choice 9: the emitter owns the step's retry and rung 4, so one locate is one scripted attempt."""
    rescuer = Rescuer(RescueOutcome(True, point=(1, 1)))
    c = ctx(retry_image=SCREEN.image, rescuer=rescuer)
    lad = Ladder(None, None, A_ON, c, regions={}, single_attempt=True)
    res = lad.locate(send_target(), Frame(BLANK, ORIGIN, 1.0))
    assert res.point is None and res.rung == "none" and res.finding_kind is None
    assert [r["result"] for r in res.rung_results] == ["locator_failed(image)"]
    assert c.retry_calls == [] and rescuer.requests == [] and lad.last.attempts == 1 and lad.last.consequence is None
    hit = lad.locate(send_target(), Frame(SCREEN.image, ORIGIN, 1.0))
    assert hit.rung == "image" and lad.last.status == "passed"
    two = Ladder(None, None, A_ON, c, regions={}).locate(send_target(), Frame(BLANK, ORIGIN, 1.0))
    assert two.rung == "image" and c.retry_calls == [1]                             # the default mode retries itself


def test_branches_come_from_the_phase0_file():
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/example/phase0/branches.yaml").read_text())
    assert Branches.from_doc(doc) == Branches("A", "A")
    assert Branches.from_doc({}) == Branches("undecided", "undecided")


def test_agent_disabled_is_none_when_everything_allows_rung_4():
    c = ctx(rescuer=Rescuer(RescueOutcome(True, point=(1, 1))))
    assert agent_disabled(send_target(), A_ON, c) is None


def test_a_registry_control_becomes_a_ladder_target():
    t = target_from_control("trade.order-entry.stock", {"id": "qty-field", "jab": {"name": "Qty", "role": "text"},
                                                        "template": "anchors/qty.png", "ocr_text": "Qty",
                                                        "click_offset": {"dx": 64, "dy": 0}}, region="r")
    assert t["name"] == "trade.order-entry.stock.qty-field"
    assert t["image"] == {"template": "anchors/qty.png", "offset": [64, 0], "region": "r"}
    assert t["ocr"] == {"text": "Qty", "offset": [64, 0], "region": "r"} and t["jab"] == {"role": "text", "name": "Qty"}
