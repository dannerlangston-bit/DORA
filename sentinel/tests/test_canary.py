"""Canary Service: recipes, deterministic generation and scoring (10.3, 12.5, 4.11; F7, F31, F58)."""
from __future__ import annotations

import dataclasses

import cv2
import numpy as np
import pytest

from sentinel import canary as C
from sentinel.records import CheckpointOutcome, Classification, Component, DiffSummary
from sentinel.taxonomy import TaxonomyError

FONT = cv2.FONT_HERSHEY_SIMPLEX
W, H = 400, 260
BOX = dict(
    label=(8, 14, 120, 22), price=(66, 44, 64, 22), grid=(8, 82, 120, 62), pl=(176, 14, 70, 40),
    chart=(260, 14, 130, 60), acct=(260, 90, 100, 20), field=(260, 124, 80, 22), parent=(136, 60, 30, 84),
    control=(140, 64, 18, 12), divider=(176, 70, 70, 1), strip=(176, 160, 160, 20),
    viewport=(176, 180, 160, 40), empty=(8, 160, 120, 70), buy=(176, 230, 40, 20), sell=(226, 230, 40, 20),
)
YELLOW = (0, 220, 220)
CONFIRM = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
POSITIONS = "census/monitor.activity.position-statement/ws.tier-a.active-trader"
FLEXGRID = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
TICKET = "flow/trade.order-entry.stock/flow.canary.ticket-stock/cp-ready"


def _fill(img, box, color):
    x, y, w, h = box
    img[y:y + h, x:x + w] = color


def canvas() -> np.ndarray:
    """A synthetic approved capture with one feature per recipe."""
    img = np.full((H, W, 3), 235, np.uint8)

    def text(s, org, scale=0.5, color=(20, 20, 20)):
        cv2.putText(img, s, org, FONT, scale, color, 1, cv2.LINE_8)

    text("Buying Power", (12, 30))
    text("100", (12, 60))
    text("150.25", (70, 60))
    for i, s in enumerate(["A 101", "B 102", "C 103", "D 104"]):
        text(s, (12, 94 + 14 * i), 0.4)
    text("+12.50", (180, 30), 0.5, (0, 160, 0))
    text("-3.10", (180, 48), 0.5, (0, 0, 200))
    x, y, _, _ = BOX["chart"]
    for k in range(8):
        cv2.rectangle(img, (x + 8 + 15 * k, y + 20 + (k % 3) * 5), (x + 14 + 15 * k, y + 45), (120, 120, 120), -1)
    pts = np.array([[x + 2 + 16 * k, y + 15 + int(10 * np.sin(k))] for k in range(8)], np.int32)
    cv2.polylines(img, [pts], False, YELLOW, 2, cv2.LINE_8)
    _fill(img, BOX["acct"], (0, 0, 0))
    _fill(img, BOX["field"], (250, 250, 250))
    text("100", (264, 140))
    _fill(img, BOX["parent"], (215, 215, 215))
    _fill(img, BOX["control"], (90, 90, 90))
    _fill(img, BOX["divider"], (120, 120, 120))
    _fill(img, BOX["strip"], (200, 200, 200))
    text("BUY MKT", (180, 175), 0.4)
    _fill(img, BOX["viewport"], (250, 250, 250))
    for k in range(5):
        img[184 + 8 * k, 180:332] = (170, 170, 170)
    _fill(img, BOX["empty"], (250, 250, 250))
    text("BUY", (180, 245), 0.4)
    text("SELL", (228, 245), 0.4)
    return img


BASE = canvas()
FRAME = (0, 0, W, H)


def rect(box, grow=(0, 0, 0, 0)):
    """(x0, y0, x1, y1) of a box, grown left, top, right, bottom."""
    x, y, w, h = box
    return x - grow[0], y - grow[1], x + w + grow[2], y + h + grow[3]


def union(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def moved(box, dx, dy):
    x, y, w, h = box
    return rect((x + dx, y + dy, w, h))


# class, box, recipe params, the rectangle the class may change (10.3: inside, or as the class requires around)
CASES = [
    ("erased_control", BOX["control"], {}, rect(BOX["control"])),
    ("swapped_digit", BOX["price"], {"position": "last"}, rect(BOX["price"])),
    ("swapped_label", BOX["buy"], {"source_box": BOX["sell"]}, rect(BOX["buy"])),
    ("color_swap", BOX["pl"], {}, rect(BOX["pl"])),
    ("shifted_panel", BOX["viewport"], {"dx": 12}, union(rect(BOX["viewport"]), moved(BOX["viewport"], 12, 0))),
    ("shifted_panel", BOX["viewport"], {}, rect(BOX["viewport"], (12, 12, 12, 12))),
    ("clipped_label", BOX["label"], {}, rect(BOX["label"])),
    ("clipped_label", BOX["label"], {"style": "ellipsis", "fraction": 0.3}, rect(BOX["label"])),
    ("overlapped_panels", BOX["strip"], {"layer_box": BOX["viewport"], "dy": -14},
     union(rect(BOX["viewport"]), moved(BOX["viewport"], 0, -14))),
    ("overlapped_panels", BOX["strip"], {"layer_box": BOX["viewport"]}, rect(BOX["viewport"], (0, 14, 0, 0))),
    ("missing_row", BOX["grid"], {}, rect(BOX["grid"])),
    ("header_change", BOX["price"], {}, rect(BOX["price"])),
    ("placeholder", BOX["field"], {}, rect(BOX["field"])),
    ("font_height", BOX["grid"], {}, rect(BOX["grid"])),
    ("scrollbar", BOX["empty"], {}, rect(BOX["empty"])),
    ("redaction", BOX["acct"], {}, rect(BOX["acct"])),
    ("sub_floor", BOX["divider"], {}, rect(BOX["divider"], (1, 1, 1, 1))),
    ("global_shift", FRAME, {}, rect(FRAME)),
    ("global_shift", FRAME, {"delta": 6}, rect(FRAME)),
    ("landmark_loss", BOX["label"], {}, rect(BOX["label"])),
    ("study_removed", BOX["chart"], {"color": list(YELLOW)}, rect(BOX["chart"])),
    ("aa_control", FRAME, {}, None),
    ("wrong_quantity", BOX["field"], {}, rect(BOX["field"])),
    ("blank_field", BOX["field"], {}, rect(BOX["field"])),
    ("gateway", BOX["field"], {}, rect(BOX["field"])),
    ("outside_parent", BOX["control"], {"parent_box": BOX["parent"]}, rect(BOX["control"], (6, 6, 6, 6))),
    ("window_offscreen", FRAME, {}, rect(FRAME)),
    ("misaligned_column", BOX["price"], {}, rect(BOX["price"])),
    ("error_dialog", BOX["empty"], {}, rect(BOX["empty"])),
]


def rng(n: int = 7) -> np.random.Generator:
    return C.mutant_rng(C.SEED, f"M{n:02d}")


def changed_rect(a, b):
    ch = np.any(a != b, axis=2)
    if not ch.any():
        return None
    ys, xs = np.nonzero(ch)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# -- recipes -----------------------------------------------------------------------------------------

def test_one_recipe_per_class_of_the_table():
    assert len(C.CLASSES) == 26 and set(C.RECIPES) == set(C.CLASSES)
    assert {c for c, *_ in CASES} | {"focus_order"} == set(C.CLASSES)


@pytest.mark.parametrize("cls,box,params,allowed", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_recipe_changes_only_its_target_and_is_deterministic(cls, box, params, allowed):
    base = BASE.copy()
    out = C.RECIPES[cls](base, box, rng(), **params)
    assert np.array_equal(base, BASE), "recipes are pure: the input frame is never modified"
    assert out.shape == BASE.shape and out.dtype == np.uint8
    got = changed_rect(out, BASE)
    if allowed is None:
        assert got is None
    else:
        assert got is not None, "the mutant must differ from the approved capture"
        assert allowed[0] <= got[0] and allowed[1] <= got[1] and got[2] <= allowed[2] and got[3] <= allowed[3], (got, allowed)
    assert np.array_equal(out, C.RECIPES[cls](BASE, box, rng(), **params))


def test_focus_order_takes_the_next_steps_after_frame():
    donor = BASE.copy()
    cv2.rectangle(donor, (258, 122), (341, 147), (255, 120, 0), 1)       # focus ring moved to the field
    out = C.swap_frame(BASE, FRAME, rng(), donor=donor)
    assert np.array_equal(out, donor) and changed_rect(out, BASE) == (258, 122, 342, 148)
    with pytest.raises(C.GenerationError):
        C.swap_frame(BASE, FRAME, rng(), donor=donor[:10])


def test_parameters_stay_in_the_spec_ranges():
    out = C.overlap_panels(BASE, BOX["strip"], rng(), layer_box=BOX["viewport"], dy=-14)
    assert np.array_equal(out[166:206, 176:336], BASE[180:220, 176:336])     # M12: 14 px up into the strip
    with pytest.raises(C.GenerationError, match="4 to 14"):
        C.overlap_panels(BASE, BOX["strip"], rng(), layer_box=BOX["viewport"], dy=-20)
    with pytest.raises(C.GenerationError, match="15 to 40"):
        C.clip_label(BASE, BOX["label"], rng(), fraction=0.5)
    red = C.global_shift(BASE, FRAME, rng(), delta=6)
    assert np.array_equal(red[..., :2], BASE[..., :2])
    assert np.array_equal(red[..., 2].astype(int), np.minimum(BASE[..., 2].astype(int) + 6, 255))
    one = C.global_shift(BASE, FRAME, rng())
    assert np.array_equal(one[:, 1:], BASE[:, :-1])                          # M21: 1 px right
    off = C.move_offscreen(BASE, FRAME, rng())
    assert np.array_equal(off[:, :200], BASE[:, 200:]) and not off[:, 200:].any()   # M30: x = -200
    with pytest.raises(C.GenerationError, match="exactly 1 px"):
        C.nudge_divider(BASE, BOX["divider"], rng(), dy=2)
    with pytest.raises(C.GenerationError, match="does not cross"):
        C.cross_parent(BASE, (145, 90, 10, 10), rng(), parent_box=BOX["parent"])


def test_glyph_transplant_swaps_one_digit_from_the_same_row():
    out = C.replace_glyph(BASE, BOX["price"], rng(), position="last")
    x0, y0, x1, y1 = changed_rect(out, BASE)
    assert 105 <= x0 and x1 <= 120 and BOX["price"][1] <= y0                # only the last digit's slot


def test_recipes_refuse_what_they_cannot_mutate():
    blank = np.full((H, W, 3), 235, np.uint8)
    for fn, box in [(C.channel_swap, BOX["pl"]), (C.replace_glyph, BOX["price"]), (C.remove_row, BOX["grid"])]:
        with pytest.raises(C.GenerationError):
            fn(blank, box, rng())
    with pytest.raises(C.GenerationError, match="degenerate"):
        C.erase(BASE, (390, 10, 20, 5), rng())


# -- catalog (4.11) ------------------------------------------------------------------------------

CATALOG = f"""
- mutant_id: M02
  name: mut.confirm.send-erased
  checkpoint_id: {CONFIRM}
  class: erased_control
  mode: image
  money_cell: true
  generator: {{op: erase, params: {{fill: background_sample}}, seed: 20260101}}
  region_ref: rgn.confirm.send-button
  expected: {{outcomes: [invariant_violation], invariant_id: INV-10}}
  owner: sentinel-canaries
- mutant_id: M03
  name: mut.confirm.qty-digit
  checkpoint_id: {CONFIRM}
  class: swapped_digit
  mode: image
  money_cell: true
  generator: {{op: replace_glyph, params: {{from: "0", to: "8", position: last}}, seed: 20260101}}
  region_ref: rgn.confirm.qty
  expected: {{outcomes: [assertion_failed], assertion_id: qty}}
  owner: sentinel-canaries
  description: Quantity 100 -> 108 by glyph transplant
- mutant_id: M09
  checkpoint_id: {POSITIONS}
  class: shifted_panel
  mode: image
  money_cell: false
  generator: {{op: translate}}
  region_ref: rgn.positions.grid
  expected: {{outcomes: [collateral]}}
  owner: sentinel-canaries
- mutant_id: M12
  name: mut.ladder.overlap-grid
  checkpoint_id: {FLEXGRID}
  class: overlapped_panels
  mode: image
  money_cell: false
  generator: {{op: translate, params: {{dy: -14, layer: rgn.ladder.viewport, fill: background_sample}}, seed: 20260101}}
  region_ref: rgn.ladder.order-strip
  expected: {{outcomes: [invariant_violation, collateral], invariant_id: INV-01}}
  owner: sentinel-canaries
- mutant_id: M25
  checkpoint_id: "*"
  class: aa_control
  mode: image
  money_cell: false
  generator: {{op: overlay}}
  expected: {{outcomes: [identical]}}
  owner: sentinel-canaries
- mutant_id: M26
  checkpoint_id: {TICKET}
  class: wrong_quantity
  mode: behavioral
  money_cell: true
  canary_flow: flow.canary.ticket-stock
  generator: {{op: type_value, params: {{value: "10"}}}}
  region_ref: rgn.ticket.qty
  expected: {{outcomes: [assertion_failed]}}
  owner: sentinel-canaries
"""

REGIONS = {
    CONFIRM: {"rgn.confirm.send-button": BOX["control"], "rgn.confirm.qty": BOX["price"]},
    POSITIONS: {"rgn.positions.grid": BOX["viewport"]},
    FLEXGRID: {"rgn.ladder.order-strip": BOX["strip"], "rgn.ladder.viewport": BOX["viewport"]},
}
APPROVED = {CONFIRM: BASE, POSITIONS: BASE, FLEXGRID: BASE}


def test_catalog_parses_the_4_11_shape():
    cat = C.parse_catalog(CATALOG)
    assert [e.mutant_id for e in cat.entries] == ["M02", "M03", "M09", "M12", "M25", "M26"]
    m12 = cat.entries[3]
    assert m12.defect_class == "overlapped_panels" and m12.money_cell is False          # F58: ladder is not a money cell
    assert m12.params["layer"] == "rgn.ladder.viewport" and m12.expected.outcomes == ("invariant_violation", "collateral")
    assert cat.entries[1].expected.assertion_id == "qty" and cat.entries[0].seed == C.SEED
    assert cat.entries[2].seed is None and cat.entries[4].region_ref is None
    assert len(cat.version) == 64 and all(e.catalog_version == cat.version for e in cat.entries)


@pytest.mark.parametrize("edit,match", [
    (("class: erased_control", "class: erased_button"), "26 classes"),
    (("mutant_id: M03", "mutant_id: M02"), "duplicate"),
    (("  canary_flow: flow.canary.ticket-stock\n", ""), "L-MUT-04"),
    (("- mutant_id: M02\n", "- mutant_id: M02\n  :\n"), "not valid YAML"),
    (("mode: behavioral", "mode: image"), "behavioral"),
    (("invariant_id: INV-10", "invariant_id: INV-99"), "L-MUT-06"),
    (("outcomes: [identical]", "outcomes: [detected]"), "checkpoint outcome"),
    (("op: erase,", "op: smudge,"), "generator.op"),
    (("owner: sentinel-canaries\n- mutant_id: M03", "owner: sentinel-canaries\n  colour: red\n- mutant_id: M03"), "unknown keys"),
])
def test_catalog_rejects_entries_outside_the_schema(edit, match):
    with pytest.raises(C.CatalogError, match=match):
        C.parse_catalog(CATALOG.replace(*edit, 1))


# -- generation (10.3 Determinism) -------------------------------------------------------------------

def test_generate_is_seed_stable_and_hash_checked():
    entries = C.parse_catalog(CATALOG).entries
    first = C.generate(entries, APPROVED, REGIONS)
    again = C.generate(entries, APPROVED, REGIONS, previous_manifest=first.manifest)
    assert not first.failed and not again.failed and first.manifest == again.manifest
    keys = {m.key for m in first.mutants}
    assert {f"M25@{c}" for c in (CONFIRM, POSITIONS, FLEXGRID)} <= keys                 # every bound checkpoint
    other = C.generate(entries, APPROVED, REGIONS, seed=2).manifest
    assert other[f"M09@{POSITIONS}"] != first.manifest[f"M09@{POSITIONS}"]               # catalog seed governs
    assert other[f"M12@{FLEXGRID}"] == first.manifest[f"M12@{FLEXGRID}"]                 # an entry's own seed wins
    m12 = next(m for m in first.mutants if m.mutant_id == "M12")
    assert m12.footprint == (176, 166, 160, 54) and m12.base_sha256 == C.image_sha256(BASE)
    m25 = [m for m in first.mutants if m.mutant_id == "M25"]
    assert all(m.mutant_sha256 == m.base_sha256 and m.footprint is None for m in m25)
    m26 = next(m for m in first.mutants if m.mutant_id == "M26")
    assert m26.image is None and not m26.failed                                          # behavioral: the flow runs it


def test_a_mutant_that_changes_bytes_on_the_same_base_fails_generation():
    entries = C.parse_catalog(CATALOG).entries
    manifest = C.generate(entries, APPROVED, REGIONS).manifest
    manifest[f"M02@{CONFIRM}"] = dict(manifest[f"M02@{CONFIRM}"], mutant_sha256="0" * 64)
    m02 = next(m for m in C.generate(entries, APPROVED, REGIONS, previous_manifest=manifest).mutants if m.mutant_id == "M02")
    assert m02.failed and "nondeterministic" in m02.error and m02.image is None
    new_base = BASE.copy()
    new_base[0, 0] = 0                                                                   # approved capture changed
    regen = C.generate(entries, {**APPROVED, CONFIRM: new_base}, REGIONS, previous_manifest=manifest)
    m02 = next(m for m in regen.mutants if m.mutant_id == "M02")
    assert not m02.failed and m02.base_sha256 == C.image_sha256(new_base)


def test_generation_failed_on_missing_or_degenerate_targets():
    entries = C.parse_catalog(CATALOG).entries
    regions = {
        CONFIRM: {"rgn.confirm.send-button": (140, 64, 0, 12)},                         # degenerate; qty not located
        FLEXGRID: {"rgn.ladder.order-strip": BOX["strip"]},                              # layer not located
    }
    gen = C.generate(entries, {CONFIRM: BASE, FLEXGRID: BASE}, regions)
    errors = {m.key: m.error for m in gen.failed}
    assert "degenerate" in errors[f"M02@{CONFIRM}"]
    assert "rgn.confirm.qty not located" in errors[f"M03@{CONFIRM}"]
    assert "no approved capture" in errors[f"M09@{POSITIONS}"]
    assert "rgn.ladder.viewport (layer) not located" in errors[f"M12@{FLEXGRID}"]
    assert "no approved capture" in errors[f"M25@{POSITIONS}"]                            # M25 on every bound checkpoint
    assert not any(m.key in (f"M25@{CONFIRM}", f"M25@{FLEXGRID}") or m.mutant_id == "M26" for m in gen.failed)
    assert all(m.failed for m in C.generate(entries, APPROVED, REGIONS, catalog_hash_ok=False).mutants)
    bad = C.parse_catalog(CATALOG.replace("{fill: background_sample}, seed", "{fill: background_sample, size: 3}, seed", 1))
    m02 = C.generate(bad.entries, APPROVED, REGIONS).mutants[0]
    assert m02.error.startswith("exception:TypeError")                                   # a generator exception


# -- scoring (10.3 Scoring) ----------------------------------------------------------------------------

def entry(mid, cls, money=False, cid=CONFIRM, outcomes=("invariant_violation",), region="rgn.x", mode="image", **kw):
    return C.MutantEntry(mutant_id=mid, checkpoint_id=cid, defect_class=cls, mode=mode, money_cell=money, op="erase",
                         params={}, expected=C.Expected(tuple(outcomes)), owner="sentinel-canaries",
                         region_ref=region, catalog_version="v1", **kw)


def mutant(*args, footprint=(100, 100, 20, 20), error=None, **kw):
    e = entry(*args, **kw)
    return C.Mutant(entry=e, checkpoint_id=e.checkpoint_id, seed=C.SEED, base_sha256="b" * 64,
                    mutant_sha256=None if error else "m" * 64, footprint=None if error else footprint, error=error)


def produced(m, outcome, reason=None, diff=None, synthetic=True):
    return CheckpointOutcome(run_id="run-2026-11-10-nightly-0001", checkpoint_id=m.checkpoint_id, outcome=outcome,
                             reason=reason, workspace_id="ws.tier-a.active-trader", persona="active_trader",
                             data_mode="frozen", diff=diff, synthetic=synthetic)


def result(m, outcome, reason=None, **kw):
    diff = kw.pop("diff", None)
    return C.MutantResult(mutant=m, produced=produced(m, outcome, reason, diff), **kw)


CTX = C.RowContext(run_id="run-2026-11-10-nightly-0001", workspace_id="ws.tier-a.active-trader",
                   persona="active_trader", data_mode="frozen")


def test_rolling_floor():
    assert C.rolling_floor([]) == pytest.approx(0.90)
    assert C.rolling_floor([1.0] * 14) == pytest.approx(0.90)
    assert C.rolling_floor([0.0] * 10 + [0.92] * 14) == pytest.approx(0.87)              # last 14 nights only
    assert C.rolling_floor([0.5] * 14, phase2_exited=True) == pytest.approx(0.95)


def test_all_detected_is_ok():
    rs = [result(mutant("M02", "erased_control", money=True), "invariant_violation", "INV-10"),
          result(mutant("M12", "overlapped_panels", cid=FLEXGRID), "collateral",
                 diff=DiffSummary(components=[Component(area=40, bbox=(110, 105, 4, 4))])),
          result(mutant("M25", "aa_control", outcomes=("identical",), region=None), "identical")]
    rep = C.score(rs, since="2026-11-10 03:12 ET")
    assert rep.state == rep.detector_state == "ok" and rep.banner is None and rep.page is None
    assert [r.state for r in rep.results] == ["detected"] * 3
    assert not rep.outcome_rows and not rep.rc_signoff_blocked and not rep.no_canary_ran


def test_missed_money_mutant_is_detector_blind_with_the_10_3_banner():
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",), description="Price 150.25 -> 150.52")
    absorbed = ("threshold: calibration for fingerprint 9e1f03 carries a non-zero floor on rgn.confirm.price\n"
                "  (fix-list item FL-31, owner jdoe)")
    rs = [result(m05, "identical", absorbed_by=absorbed)]
    rs += [result(mutant(f"M{40 + i}", "swapped_digit", money=True, outcomes=("assertion_failed",)),
                  "assertion_failed", "assertion:price") for i in range(23)]
    rep = C.score(rs, since="2026-11-09 03:12 ET")
    assert rep.state == "detector_blind" and rep.page == "P1" and rep.rc_signoff_blocked
    row = rep.results[0]
    assert row.state == "detector_blind" and row.produced_outcome == "identical" and row.absorbed_by == absorbed
    (h,) = rep.outcome_rows
    assert (h.outcome, h.reason, h.synthetic, h.checkpoint_id) == ("harness_error", "detector_blind", True, CONFIRM)
    assert rep.banner == (
        "DETECTOR BLIND since 2026-11-09 03:12 ET.\n"
        "Missed 1 of 24 money-cell mutants: M05 swapped_digit, Price 150.25 -> 150.52\n"
        "  (checkpoint flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07).\n"
        "Absorbed by: threshold: calibration for fingerprint 9e1f03 carries a non-zero floor on rgn.confirm.price\n"
        "  (fix-list item FL-31, owner jdoe).\n"
        "Reports for the affected checkpoints cannot be trusted until this clears.\n"
        "RC sign-off is blocked."
    )


def test_non_money_misses_below_the_floor_are_detector_degraded():
    shifted = [result(mutant(m, "shifted_panel", cid=POSITIONS, outcomes=("collateral",)), "identical")
               for m in ("M08", "M09")]
    clipped = [result(mutant(f"M{40 + i}", "clipped_label"), "invariant_violation", "INV-03") for i in range(19)]
    clipped.append(result(mutant("M59", "clipped_label"), "noise", "below_floor"))
    m12 = result(mutant("M12", "overlapped_panels", cid=FLEXGRID), "identical")           # F58: non-money
    rep = C.score(shifted + clipped + [m12], history={"clipped_label": [1.0] * 14}, since="2026-11-10 03:12 ET")
    assert rep.state == "detector_degraded" and rep.page == "P2" and rep.rc_signoff_blocked
    assert rep.degraded_classes == ["overlapped_panels", "shifted_panel"]
    states = {r.mutant_id: r.state for r in rep.results}
    assert states["M08"] == states["M09"] == states["M12"] == "detector_degraded"
    assert states["M59"] == "missed" and rep.class_rates["clipped_label"] == pytest.approx(0.95)
    assert {(o.reason, o.synthetic) for o in rep.outcome_rows} == {("detector_degraded", True)}
    assert len(rep.outcome_rows) == 3
    assert rep.banner.startswith("DETECTOR DEGRADED since 2026-11-10 03:12 ET.\n")
    assert "shifted_panel 0% (floor 90%)" in rep.banner and rep.banner.endswith("RC sign-off is blocked.")
    after = C.score(clipped, phase2_exited=True)                                          # 95 percent once Phase 2 exits
    assert after.state == "ok" and C.score(clipped[1:], phase2_exited=True).state == "detector_degraded"


def test_identical_control_m25_expects_identical():
    m25 = mutant("M25", "aa_control", outcomes=("identical",), region=None)
    assert C.score([result(m25, "identical")]).results[0].state == "detected"
    on_region = DiffSummary(components=[Component(area=40, bbox=(0, 0, 400, 260))])
    rep = C.score([result(m25, "collateral", diff=on_region)])
    assert rep.results[0].state == "detector_degraded" and rep.state == "detector_degraded"   # any finding is the miss
    m20 = mutant("M20", "sub_floor", outcomes=("noise",))
    assert C.score([result(m20, "needs_human", "reviewer_uncertain", on_mutated_region=True)]).results[0].state != "detected"


def test_collateral_counts_only_on_the_mutated_region():
    m09 = mutant("M09", "shifted_panel", cid=POSITIONS, outcomes=("collateral",), footprint=(176, 180, 172, 40))
    m31 = mutant("M31", "misaligned_column", cid=POSITIONS, region="rgn.positions.cell")
    near = DiffSummary(components=[Component(area=90, bbox=(115, 110, 10, 10))])         # meets the footprint
    far = DiffSummary(components=[Component(area=90, bbox=(5, 5, 10, 10))])
    named = DiffSummary(components=[Component(area=9, bbox=(5, 5, 3, 3), region_id="rgn.positions.cell")])
    assert C.score([result(m09, "collateral", diff=far)]).results[0].state == "detected"     # in the expected list
    assert C.score([result(m31, "collateral", diff=near)]).results[0].state == "detected"
    assert C.score([result(m31, "needs_human", "reviewer_no_match", diff=named)]).results[0].state == "detected"
    assert C.score([result(m31, "collateral", diff=far)]).results[0].state == "detector_degraded"


def test_min_confidence_gates_detection():
    e = dataclasses.replace(entry("M04", "swapped_digit", money=True, outcomes=("collateral",)),
                            expected=C.Expected(("collateral",), min_confidence=0.8))
    m04 = C.Mutant(entry=e, checkpoint_id=CONFIRM, seed=C.SEED, footprint=(100, 100, 20, 20))
    low = produced(m04, "collateral")
    low.classification = Classification(category="collateral", by="reviewer", confidence=0.6)
    assert C.score([C.MutantResult(mutant=m04, produced=low)]).state == "detector_blind"
    low.classification = Classification(category="collateral", by="reviewer", confidence=0.9)
    assert C.score([C.MutantResult(mutant=m04, produced=low)]).state == "ok"


def test_generation_failed_pages_and_banners_like_blind():
    failed = mutant("M02", "erased_control", money=True, error="region rgn.confirm.send-button not located on " + CONFIRM)
    rep = C.score([C.MutantResult(mutant=failed)], context=CTX, since="2026-11-10 03:12 ET")
    assert rep.state == "generation_failed" and rep.page == "P1" and rep.no_canary_ran
    assert rep.results[0].state == "generation_failed" and rep.results[0].produced_outcome is None
    (h,) = rep.outcome_rows
    assert (h.outcome, h.reason, h.synthetic) == ("harness_error", "canary_generation_failed", True)
    assert rep.banner.startswith("DETECTOR BLIND since 2026-11-10 03:12 ET.\ncanaries did not run\n  M02 erased_control")
    with pytest.raises(ValueError, match="RowContext"):
        C.score([C.MutantResult(mutant=failed)])
    ok = result(mutant("M12", "overlapped_panels", cid=FLEXGRID), "invariant_violation", "INV-01")
    assert not C.score([C.MutantResult(mutant=failed), ok], context=CTX).no_canary_ran


def test_redaction_and_gateway_detection():
    m19 = mutant("M19", "redaction", outcomes=("collateral",))
    assert C.score([result(m19, "identical", redaction_hit=True)]).state == "ok"
    rep = C.score([result(m19, "identical", redaction_hit=False)])
    assert rep.state == "detector_blind" and rep.results[0].state == "detector_degraded"
    assert rep.outcome_rows[0].reason == "safety_canary_blind:redaction" and "Redaction canary blind: M19" in rep.banner
    m28 = mutant("M28", "gateway", money=True, cid=TICKET, mode="behavioral", canary_flow="flow.canary.gateway",
                 outcomes=("assertion_failed",))
    assert C.score([result(m28, "not_run", "step_failed:s04", step_status="blocked_by_gateway")]).state == "ok"
    assert C.score([result(m28, "identical", step_status="passed")]).state == "detector_blind"


def test_score_refuses_non_synthetic_or_missing_records():
    m = mutant("M02", "erased_control", money=True)
    with pytest.raises(ValueError, match="synthetic"):
        C.score([C.MutantResult(mutant=m, produced=produced(m, "invariant_violation", "INV-10", synthetic=False))])
    with pytest.raises(ValueError, match="outcome the path produced"):
        C.score([C.MutantResult(mutant=m)])


def test_canary_result_rows_enforce_the_8_3_checks():
    common = dict(mutant_id="M05", catalog_version="v1", checkpoint_id=CONFIRM, defect_class="swapped_digit",
                  seed=C.SEED, expected=["assertion_failed"], produced_outcome="identical")
    with pytest.raises(TaxonomyError):
        C.CanaryResult(money_cell=True, state="missed", **common)
    with pytest.raises(TaxonomyError):
        C.CanaryResult(money_cell=False, state="detector_blind", **common)
    with pytest.raises(TaxonomyError):
        C.CanaryResult(money_cell=False, state="detector_degraded", **common)
    assert C.CanaryResult(money_cell=True, state="detector_blind", **common).to_dict()["state"] == "detector_blind"


def test_generated_mutants_score_end_to_end():
    gen = C.generate(C.parse_catalog(CATALOG).entries, APPROVED, REGIONS)
    rs = []
    for m in gen.mutants:
        outcome = {"aa_control": "identical", "shifted_panel": "collateral"}.get(m.defect_class, m.entry.expected.outcomes[0])
        reason = {"invariant_violation": "INV-01", "assertion_failed": "assertion:qty"}.get(outcome)
        rs.append(result(m, outcome, reason))
    rep = C.score(rs)
    assert rep.state == "ok" and all(r.state == "detected" for r in rep.results)
    assert {r.catalog_version for r in rep.results} == {C.parse_catalog(CATALOG).version}
