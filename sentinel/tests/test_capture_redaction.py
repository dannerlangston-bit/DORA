"""The redaction stage of Capture (3.6, 5.7, 6.10, 12.7 H6): anchored boxes, the OCR sweep, the union fill, the
missing-landmark refusal, the redaction version and the self-test. Synthetic frames only; `SyntheticOcr` reads the
words drawn on them."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from sentinel.canary import plant_identifier
from sentinel.capture import (RedactionContext, RedactionError, RedactionList, Sweep, SyntheticOcr, h6_frame,
                              h6_self_test, redact_pair, redaction_version, sample_artifacts, verify)
from sentinel.diff.engine import diff_pair
from sentinel.diff.regions import Landmark, Rect, Region, RegionsFile
from sentinel.store.artifacts import ArtifactStore, RedactionRefused
from sentinel.synth import THEME, render, sample_layout

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
CID = "census/sidebar.account-info/ws.tier-a.active-trader"
BG = THEME["bg"]
FONT = cv2.FONT_HERSHEY_SIMPLEX
PATCH = np.random.default_rng(7).integers(0, 256, (24, 24, 3), dtype=np.uint8)
LM = "lm.account-header"
LIST = {"schema_version": 1, "owner": "security-contact", "landmarks": [{"id": LM, "template": "unused.png"}],
        "regions": [{"id": "rgn.redact.account-number", "type": "redaction", "landmark": LM,
                     "rect": {"dx": 30, "dy": 0, "w": 120, "h": 24}, "identity_field": True}]}
ANCHORS = RedactionList.from_dict(LIST, templates={LM: PATCH})
CTX = RedactionContext(run_id="run-r1", checkpoint_id=CID, tier=2)
ACCOUNT = "D-12345678"


def frame(lm_at=(20, 20), words=()):
    """A dark frame with the landmark patch at `lm_at` and each (text, (x, y)) drawn; returns the image and the
    drawn word boxes."""
    img = np.full((240, 400, 3), BG, np.uint8)
    if lm_at is not None:
        x, y = lm_at
        img[y:y + 24, x:x + 24] = PATCH
    placed = []
    for text, (x, y) in words:
        (tw, th), base = cv2.getTextSize(text, FONT, 0.5, 1)
        cv2.putText(img, text, (x, y + th), FONT, 0.5, (220, 220, 220), 1, cv2.LINE_AA)
        placed.append((text, Rect(x, y, tw, th + base)))
    return img, placed


def ocr_for(*placed_lists):
    return SyntheticOcr([w for placed in placed_lists for w in placed], (400, 240))


def region(img, r):
    return img[r.y:r.y + r.h, r.x:r.x + r.w]


def is_fill(img, r):
    return bool((region(img, r) == np.array(BG, np.uint8)).all())


# -- stage 1: anchored boxes --------------------------------------------------------------------------------------

def test_an_account_number_in_the_anchored_box_is_filled_on_both_sides_with_identical_pixels():
    base, pb = frame(words=[(ACCOUNT, (54, 24))])
    cand, pc = frame(words=[(ACCOUNT, (54, 24))])
    before = base.copy()
    res = redact_pair(base, cand, ANCHORS, ocr_for(pb, pc), BG, context=CTX)
    box = Rect(50, 20, 120, 24)
    assert res.stored_ok and res.terminal is None
    assert [(b.rect, b.identity_field, b.anchor_id) for b in res.boxes] == [(box, True, "rgn.redact.account-number")]
    assert is_fill(res.base, box) and is_fill(res.candidate, box)
    assert np.array_equal(region(res.base, box), region(res.candidate, box))
    assert dict(res.counts) == {"base": 1, "candidate": 1}
    assert res.findings == () and all(h.covered for h in res.hits)          # the sweep hit is inside stage 1
    assert res.header is not None and res.header.version == res.version
    assert np.array_equal(base, before)                                   # the raw frame is not touched


def test_the_union_of_both_sides_boxes_is_applied_to_both():
    base, _ = frame(lm_at=(20, 20))
    cand, _ = frame(lm_at=(20, 100))                                      # the panel moved on the candidate
    res = redact_pair(base, cand, ANCHORS, ocr_for(), BG)
    rects = {b.rect for b in res.boxes}
    assert rects == {Rect(50, 20, 120, 24), Rect(50, 100, 120, 24)}
    for img in (res.base, res.candidate):
        assert all(is_fill(img, r) for r in rects)


def test_a_redaction_landmark_found_on_neither_side_adds_no_box():
    base, _ = frame(lm_at=None)
    cand, _ = frame(lm_at=None)
    res = redact_pair(base, cand, ANCHORS, ocr_for(), BG)
    assert res.stored_ok and res.boxes == () and np.array_equal(res.base, base)


@pytest.mark.parametrize("missing", ["base", "candidate"])
def test_a_redaction_landmark_missing_on_one_side_stores_nothing(tmp_path, missing):
    base, pb = frame(lm_at=None if missing == "base" else (20, 20), words=[(ACCOUNT, (54, 24))])
    cand, pc = frame(lm_at=None if missing == "candidate" else (20, 20), words=[(ACCOUNT, (54, 24))])
    res = redact_pair(base, cand, ANCHORS, ocr_for(pb, pc), BG, context=CTX)
    t = res.terminal
    assert (t.outcome, t.reason, t.side) == ("landmark_missing", f"redaction_landmark:{LM}", missing)
    assert res.base is None and res.candidate is None and res.header is None and not res.stored_ok
    arts = ArtifactStore(tmp_path / "shard", "run-r1/1")
    _, png = cv2.imencode(".png", base)
    with pytest.raises(RedactionRefused):                                 # no header: the store refuses the frame
        arts.put(png.tobytes(), name="capture.png", kind="capture", media_type="image/png", redaction=res.header)
    assert not any(p.is_file() for p in (tmp_path / "shard").rglob("*"))


# -- stage 2: the OCR sweep ----------------------------------------------------------------------------------------

def test_an_account_number_outside_the_anchors_is_blacked_out_with_a_redaction_miss():
    base, pb = frame(words=[(ACCOUNT, (200, 150))])
    cand, pc = frame(words=[(ACCOUNT, (200, 150))])
    res = redact_pair(base, cand, ANCHORS, ocr_for(pb, pc), BG, context=CTX)
    word = pb[0][1]
    assert res.masked(word) and is_fill(res.base, word) and is_fill(res.candidate, word)
    assert [(f.kind, f.side, f.tier, f.detail) for f in res.findings] == [("redaction_miss", "both", 2, None)]
    f = res.findings[0]
    assert f.evidence["crop"] == list(word.as_tuple()) and f.evidence["pattern"] == "paper_id"
    assert f.run_id == "run-r1" and f.checkpoint_id == CID
    assert ACCOUNT not in json.dumps(f.to_dict(), default=str)            # the finding never carries the id
    sweep_box = next(b for b in res.boxes if b.anchor_id is None)
    assert not sweep_box.identity_field                                   # stage-2 boxes count toward the budget
    assert dict(res.counts) == {"base": 2, "candidate": 2}


def test_a_candidate_only_hit_is_a_tier_one_suspected_leak_and_a_base_only_hit_is_not():
    base, pb = frame(words=[("alt 87654321", (60, 200))])
    cand, pc = frame(words=[(ACCOUNT, (200, 150))])
    res = redact_pair(base, cand, ANCHORS, ocr_for(pb, pc), BG, context=CTX)
    got = sorted((f.side, f.tier, f.detail, f.evidence["pattern"]) for f in res.findings)
    assert got == [("base", 2, None, "live_id"), ("candidate", 1, "candidate_only", "paper_id")]
    assert res.masked(pc[0][1]) and res.masked(pb[0][1])
    assert is_fill(res.base, pc[0][1]) and is_fill(res.candidate, pb[0][1])   # the union reaches both sides


def test_the_m19_planted_identifier_is_masked_with_a_reader_per_side():
    """12.5 M19: the canary plants 9 digits in the candidate; per-side readers allow captures of different sizes."""
    base, _ = frame()
    cand = np.pad(base, ((0, 0), (0, 20), (0, 0)), mode="edge")             # the candidate window is wider
    box = (200, 150, 110, 20)
    mutant = plant_identifier(cand, box, np.random.default_rng(20260101))
    readers = {"base": SyntheticOcr([], (400, 240)),
               "candidate": SyntheticOcr([("418026573", Rect(*box))], (420, 240))}
    res = redact_pair(base, mutant, ANCHORS, readers, BG, context=CTX)
    assert res.masked(Rect(*box)) and is_fill(res.candidate, Rect(*box))
    assert [(f.side, f.tier, f.evidence["pattern"]) for f in res.findings] == [("candidate", 1, "live_id")]


def test_a_redacted_frame_re_read_has_zero_hits():
    """6.10 Proof: an id at an anchor and one at a random spot are both masked; the re-OCR finds nothing."""
    base, pb = frame(words=[(ACCOUNT, (54, 24)), ("D-55501234", (230, 180))])
    cand, pc = frame(words=[(ACCOUNT, (54, 24)), ("D-55501234", (230, 180))])
    ocr = ocr_for(pb, pc)
    assert len(verify(cand, ocr)) == 2
    res = redact_pair(base, cand, ANCHORS, ocr, BG, context=CTX)
    assert [f.evidence["crop"] for f in res.findings] == [list(pb[1][1].as_tuple())]   # only the random one
    assert verify(res.base, ocr) == () and verify(res.candidate, ocr) == ()


@pytest.mark.parametrize("words, kinds", [
    ([("acct", (10, 10)), ("d-1234567", (60, 10))], []),                  # the paperMoney pattern is upper case D-
    ([("D-1234567", (10, 10))], ["paper_id"]),
    ([("12345678", (10, 10))], ["live_id"]),
    ([("123456789", (10, 10))], ["live_id"]),
    ([("1234567", (10, 10)), ("1234567890123", (10, 40)), ("12345678.25", (10, 70)), ("1,234,567", (10, 100))], []),
    ([("ops.desk@example.com", (10, 10))], ["email"]),
    ([("(555)", (10, 10)), ("123-4567", (60, 10))], ["phone"]),           # one hit across two words of a line
    ([("555.123.4567", (10, 10))], ["phone"]),
    ([("JANE", (10, 10)), ("Q", (60, 10)), ("TRADER", (80, 10))], ["display_name"]),
    ([("Trader", (10, 100))], ["display_name"]),                          # a name token alone
    ([("PX-0042", (10, 10))], ["literal_id"]),                            # a fixture id in its own format
])
def test_sweep_patterns(words, kinds):
    img, placed = frame(lm_at=None, words=words)
    hits = verify(img, ocr_for(placed), Sweep(literals=("px-0042",), display_name="Jane Q Trader"))
    assert sorted(h.kind for h in hits) == kinds
    if kinds == ["phone"] and len(placed) == 2:
        assert hits[0].rect == Rect.bounding([placed[0][1], placed[1][1]])


def test_a_sweep_box_over_a_money_cell_ends_the_checkpoint_mask_budget_exceeded():
    """6.10: egress safety beats comparability; the Diff Engine refuses to diff around the box."""
    base, pb = frame(words=[("D-24680135", (54, 84))])
    cand, pc = frame(words=[("D-24680135", (54, 84))])
    res = redact_pair(base, cand, ANCHORS, ocr_for(pb, pc), BG, context=CTX)
    qty = Region("rgn.qty", "diff", LM, Rect(30, 60, 140, 30), money_cell=True)
    reg = RegionsFile(CID, [Landmark(LM, PATCH)], [qty])
    out = diff_pair(res.base, res.candidate, reg, redactions=res.boxes)
    assert (out.terminal.outcome, out.terminal.reason) == ("harness_error", "mask_budget_exceeded")
    clean = diff_pair(res.base, res.candidate, reg, redactions=[b for b in res.boxes if b.identity_field])
    assert clean.terminal is None


# -- configuration, version, self-test and sample ------------------------------------------------------------------

def test_the_redaction_list_is_validated():
    bad = json.loads(json.dumps(LIST))
    bad["regions"][0]["type"] = "mask"
    with pytest.raises(RedactionError, match="type redaction"):
        RedactionList.from_dict(bad, templates={LM: PATCH})
    bad = json.loads(json.dumps(LIST))
    bad["regions"][0]["landmark"] = "lm.nowhere"
    with pytest.raises(RedactionError, match="not declared"):
        RedactionList.from_dict(bad, templates={LM: PATCH})
    with pytest.raises(RedactionError, match="not readable"):
        RedactionList.from_dict(LIST, template_root=EXAMPLE)
    with pytest.raises(RedactionError, match="empty"):
        Sweep(literals=(" ",))


def test_the_example_redaction_list_loads_with_its_templates():
    red = RedactionList.load(EXAMPLE / "redaction" / "anchors.yaml")
    assert red.owner == "security-contact" and red.landmarks and red.regions
    assert all(r.identity_field for r in red.regions if "account" in r.id)


def test_the_redaction_version_follows_the_list_and_patterns_but_not_the_rig_literals():
    v = redaction_version(ANCHORS)
    assert v.startswith("redaction-1-") and v == redaction_version(ANCHORS, Sweep(literals=("D-111",)))
    moved = json.loads(json.dumps(LIST))
    moved["regions"][0]["rect"]["w"] = 160
    assert redaction_version(RedactionList.from_dict(moved, templates={LM: PATCH})) != v
    assert redaction_version(ANCHORS, Sweep(patterns=(("paper_id", r"D-\d+"),))) != v
    base, pb = frame()
    assert redact_pair(base, base.copy(), ANCHORS, ocr_for(pb), BG).version == v


def test_h6_self_test_passes_and_fails_on_a_blind_reader():
    ok = h6_self_test(THEME)
    assert ok.passed and ok.boxes == 1 and ok.version == redaction_version()

    class Blind:
        def words(self, image):
            return []
    blind = h6_self_test(THEME, Blind())
    assert not blind.passed and "not boxed" in blind.detail
    img, (text, rect) = h6_frame(THEME)
    assert text.startswith("D-") and not is_fill(img, rect)


def test_the_post_run_ocr_sample_is_five_percent_and_deterministic():
    shas = [f"{i:064x}" for i in range(100)]
    s = sample_artifacts(shas, "run-r1")
    assert len(s) == 5 and s == sample_artifacts(list(reversed(shas)), "run-r1") and set(s) <= set(shas)
    assert s != sample_artifacts(shas, "run-r2")
    assert len(sample_artifacts(shas[:3], "run-r1")) == 1 and sample_artifacts([], "run-r1") == ()
    with pytest.raises(ValueError):
        sample_artifacts(shas, "run-r1", 0.0)


def test_synthetic_ocr_reads_a_synth_screen_and_nothing_once_filled():
    screen = render(sample_layout(), (640, 400))
    ocr = SyntheticOcr.from_screen(screen)
    words = ocr.words(screen.image)
    assert words and all(w.text for w in words)
    first = words[0].rect
    img = screen.image.copy()
    img[first.y:first.y + first.h, first.x:first.x + first.w] = BG
    assert first not in {w.rect for w in ocr.words(img)}
