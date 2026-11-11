"""Canary Service: recipes, deterministic generation and scoring (10.3, 12.5, 4.11; F7, F31, F58)."""
from __future__ import annotations

import dataclasses

import cv2
import numpy as np
import pytest

from sentinel import canary as C
from sentinel.diff.engine import RegionComponent
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
YELLOW = (220, 220, 0)                                # RGB, the channel order of every frame
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
    text("-3.10", (180, 48), 0.5, (200, 0, 0))
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


@pytest.mark.parametrize("cls,params", [("color_swap", {}), ("study_removed", {"color": list(YELLOW)}),
                                        ("gateway", {}), ("error_dialog", {})])
def test_recipes_that_need_no_background_take_a_whole_frame_box(cls, params):
    """No border exists around a whole-frame box; a recipe that never uses the background needs none."""
    out = C.RECIPES[cls](BASE, FRAME, rng(), **params)
    assert out.shape == BASE.shape and not np.array_equal(out, BASE)
    with pytest.raises(C.GenerationError, match="no border"):
        C.erase(BASE, FRAME, rng())                       # the border fill still needs its border (10.3)


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
    assert np.array_equal(red[..., 1:], BASE[..., 1:])                       # M23: red is channel 0 (RGB)
    assert np.array_equal(red[..., 0].astype(int), np.minimum(BASE[..., 0].astype(int) + 6, 255))
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
- mutant_id: M09
  checkpoint_id: {POSITIONS}
  class: shifted_panel
  mode: image
  money_cell: false
  generator: {{op: translate, params: {{}}}}
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
  name: mut.aa-control
  checkpoint_id: "*"
  class: aa_control
  mode: image
  money_cell: false
  generator: {{op: overlay, params: {{}}}}
  expected: {{outcomes: [identical]}}
  owner: sentinel-canaries
- mutant_id: M26
  name: mut.ticket.qty-ten
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
    assert cat.bare_list and cat.entries[1].label == "Quantity 100 -> 108 by glyph transplant"   # 12.5's words


def _wrapped(text: str, version: int = 1) -> str:
    body = "".join(f"  {line}\n" if line else "\n" for line in text.strip("\n").split("\n"))
    return f"schema_version: {version}\nentries:\n{body}"


def test_catalog_reads_the_wrapper_form_and_the_bare_list():
    """SD-47: `{schema_version, entries}` is the 4.11 file; the old bare list still parses (the lint grades it)."""
    wrapped = C.parse_catalog(_wrapped(CATALOG))
    assert not wrapped.bare_list and [e.mutant_id for e in wrapped.entries] == \
        [e.mutant_id for e in C.parse_catalog(CATALOG).entries]
    with pytest.raises(C.CatalogError, match="schema_version 2"):
        C.parse_catalog(_wrapped(CATALOG, 2))
    with pytest.raises(C.CatalogError, match="SD-47"):
        C.parse_catalog("mutants: []\n")


@pytest.mark.parametrize("edit,match", [
    (("class: erased_control", "class: erased_button"), r"class: 'erased_button' is not one of"),
    (("mutant_id: M03", "mutant_id: M02"), "duplicate"),
    (("  canary_flow: flow.canary.ticket-stock\n", ""), "L-MUT-04"),
    (("- mutant_id: M02\n", "- mutant_id: M02\n  :\n"), "not valid YAML"),
    (("mode: behavioral", "mode: image"), "behavioral"),
    (("invariant_id: INV-10", "invariant_id: INV-99"), "L-MUT-06"),
    (("outcomes: [identical]", "outcomes: [detected]"), r"expected.outcomes\[0\]"),
    (("op: erase,", "op: smudge,"), "generator.op"),
    (("owner: sentinel-canaries\n- mutant_id: M03", "owner: sentinel-canaries\n  colour: red\n- mutant_id: M03"),
     r"'colour' was unexpected.*L-MUT-00"),
    (("owner: sentinel-canaries\n- mutant_id: M03",
      "owner: sentinel-canaries\n  description: Send erased\n- mutant_id: M03"), "'description' was unexpected"),
    (("  generator: {op: translate, params: {}}\n  region_ref: rgn.positions.grid",
      "  generator: {op: translate}\n  region_ref: rgn.positions.grid"), "'params' is a required property"),
    (("checkpoint_id: \"*\"\n  class: aa_control", "checkpoint_id: \"*\"\n  class: sub_floor"), "SD-43"),
    (("  region_ref: rgn.confirm.qty\n", ""), "region_ref is required"),
    (("outcomes: [identical]}", "outcomes: [identical], reason: below_floor}"), "SD-44"),
    (("outcomes: [assertion_failed]}", "outcomes: [assertion_failed], step_status: blocked_by_gateway}"), "SD-44"),
])
def test_catalog_rejects_entries_outside_the_schema(edit, match):
    """The catalog validates through the lint's generated schema (L-MUT-00), so the parser and the lint agree."""
    assert edit[0] in CATALOG
    with pytest.raises(C.CatalogError, match=match):
        C.parse_catalog(CATALOG.replace(*edit, 1))


def test_parser_and_lint_schema_treat_entries_alike():
    """04b-canary-schema-drift: the only schema errors parsing tolerates are '*' for aa_control (SD-43) and a
    missing name (10.3's M12 example); every other schema error refuses the entry in both."""
    from sentinel.lint import schemas
    import yaml
    raw = yaml.safe_load(CATALOG)
    m09 = raw[2]
    assert "name" not in m09 and schemas.validate("mutant#mutant", m09) == [("", "'name' is a required property")]
    assert C.parse_entry(m09).name is None
    with_desc = dict(raw[0], description="Send erased")
    assert schemas.validate("mutant#mutant", with_desc)
    with pytest.raises(C.CatalogError, match="description"):
        C.parse_entry(with_desc)
    gateway = dict(raw[5], mutant_id="M28", **{"class": "gateway"},
                   expected={"outcomes": ["not_run"], "reason": "step_failed:s04", "step_status": "blocked_by_gateway"})
    assert C.parse_entry(gateway).expected == C.Expected(("not_run",), reason="step_failed:s04",
                                                         step_status="blocked_by_gateway")


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
    version = C.parse_catalog(CATALOG).version
    assert all(m.failed and "G3" in m.error for m in C.generate(entries, APPROVED, REGIONS, g3_hash="0" * 64).mutants)
    assert not C.generate(entries, APPROVED, REGIONS, g3_hash=version).failed
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
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))  # labelled by 12.5
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


def test_footprint_test_uses_window_coordinates_not_region_relative_boxes():
    """The mutant footprint is measured in the frame, so it meets `bbox`, never `bbox_region` (engine choice 16)."""
    m31 = mutant("M31", "misaligned_column", cid=POSITIONS, region="rgn.positions.cell", footprint=(100, 100, 20, 20))
    in_window = DiffSummary(components=[RegionComponent(90, (105, 105, 10, 10), "rgn.positions.panel", (5, 5, 10, 10))])
    in_region_only = DiffSummary(components=[RegionComponent(90, (5, 5, 10, 10), "rgn.positions.panel",
                                                             (105, 105, 10, 10))])
    assert C.score([result(m31, "collateral", diff=in_window)]).results[0].state == "detected"
    assert C.score([result(m31, "collateral", diff=in_region_only)]).results[0].state == "detector_degraded"


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
    rep = C.score([C.MutantResult(mutant=failed)], context=CTX, since="2026-11-10 03:12 ET", watchdog_event="wd-7")
    assert rep.state == "generation_failed" and rep.page == "P1" and rep.no_canary_ran
    assert rep.generation_reason == "the stage generated no mutants"
    assert rep.results[0].state == "generation_failed" and rep.results[0].produced_outcome is None
    (h,) = rep.outcome_rows
    assert (h.outcome, h.reason, h.synthetic, h.mutant_id) == ("harness_error", "canary_generation_failed", True, "M02")
    assert h.detail.startswith("watchdog_event: wd-7; mutant M02")                   # 4.13 harness_error detail
    assert rep.banner.startswith("DETECTOR BLIND since 2026-11-10 03:12 ET.\ncanaries did not run\n"
                                 "  the stage generated no mutants.\n  M02 erased_control")
    with pytest.raises(ValueError, match="RowContext"):
        C.score([C.MutantResult(mutant=failed)])


def test_one_failed_mutant_is_its_row_state_only():
    """SD-88: one mutant's generation failure is its row state; the run stays at the worst of the other states."""
    failed = mutant("M02", "erased_control", money=True, error="region rgn.confirm.send-button not located on " + CONFIRM)
    ok = result(mutant("M12", "overlapped_panels", cid=FLEXGRID), "invariant_violation", "INV-01")
    rep = C.score([C.MutantResult(mutant=failed), ok], context=CTX)
    assert rep.state == "ok" and not rep.no_canary_ran and rep.banner is None and not rep.rc_signoff_blocked
    assert [r.state for r in rep.results] == ["generation_failed", "detected"]
    assert rep.generation_errors == {f"M02@{CONFIRM}": failed.error}
    blind = result(mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",)), "identical")
    rep = C.score([C.MutantResult(mutant=failed), blind], context=CTX, since="2026-11-10 03:12 ET")
    assert rep.state == "detector_blind" and "canaries did not run" not in rep.banner


def test_a_critical_checkpoint_without_a_generated_mutant_is_generation_failed():
    """SD-88: the run state is generation_failed when a critical checkpoint has zero generated mutants."""
    failed = mutant("M02", "erased_control", money=True, error="no approved capture for " + CONFIRM)
    ok = result(mutant("M12", "overlapped_panels", cid=FLEXGRID), "invariant_violation", "INV-01")
    rep = C.score([C.MutantResult(mutant=failed), ok], context=CTX, critical_checkpoints=[CONFIRM, FLEXGRID],
                  since="2026-11-10 03:12 ET")
    assert rep.state == "generation_failed" and rep.page == "P1" and not rep.no_canary_ran
    assert rep.generation_reason == f"no generated mutant for critical checkpoint {CONFIRM}"
    assert f"  no generated mutant for critical checkpoint {CONFIRM}." in rep.banner.split("\n")
    assert C.score([ok], critical_checkpoints=[FLEXGRID]).state == "ok"


def test_no_canary_ran_needs_no_mutant_and_no_behavioral_flow():
    """10.3 Determinism: the Nightly aborts only when the stage produced no mutants and no canary flow started."""
    failed = mutant("M02", "erased_control", money=True, error="no approved capture for " + CONFIRM)
    assert C.score([C.MutantResult(mutant=failed)], context=CTX).no_canary_ran
    started = C.score([C.MutantResult(mutant=failed)], context=CTX, behavioral_flows_started=True)
    assert started.state == "generation_failed" and not started.no_canary_ran
    assert C.score([], context=CTX).no_canary_ran
    m26 = mutant("M26", "wrong_quantity", money=True, cid=TICKET, mode="behavioral", canary_flow="flow.canary.ticket-stock",
                 outcomes=("assertion_failed",))
    flow = result(m26, "assertion_failed", "assertion:qty")
    assert not C.score([C.MutantResult(mutant=failed), flow], context=CTX).no_canary_ran


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
        inv = m.entry.expected.invariant_id or "INV-01"
        reason = {"invariant_violation": inv, "assertion_failed": "assertion:qty"}.get(outcome)
        rs.append(result(m, outcome, reason))
    rep = C.score(rs)
    assert rep.state == "ok" and all(r.state == "detected" for r in rep.results)
    assert {r.catalog_version for r in rep.results} == {C.parse_catalog(CATALOG).version}


# -- expected signals, behavioral results, batches (10.3 Scoring and Injection, SD-44, SD-89) ----------------

LADDER = "sweep/trade.active-trader.ladder/flexgrid/480x360"


def with_expected(m, **kw):
    e = dataclasses.replace(m.entry, expected=dataclasses.replace(m.entry.expected, **kw))
    return dataclasses.replace(m, entry=e)


def test_the_expected_invariant_must_fire():
    """12.3: every invariant has a mutant proving it fires. M29 producing INV-01 instead of INV-02 is detected under
    10.3's list rule but not proven (a proof-gap line), and a miss under strict signals."""
    m29 = with_expected(mutant("M29", "outside_parent", cid=LADDER), invariant_id="INV-02")
    loose = C.score([result(m29, "invariant_violation", "INV-01")])
    assert loose.state == "ok" and loose.results[0].state == "detected"
    assert loose.unproven == {f"M29@{LADDER}": "signal: expected INV-02 did not fire (fired INV-01)"}
    assert loose.coverage_gaps == [f"proof gap: M29 outside_parent on {LADDER} was detected but its expected INV-02 "
                                   "did not fire (fired INV-01) (12.3)"]
    wrong = C.score([result(m29, "invariant_violation", "INV-01")], since="2026-11-10 03:12 ET", strict_signals=True)
    row = wrong.results[0]
    assert row.state == "detector_degraded" and wrong.state == "detector_degraded"
    assert row.absorbed_by == "signal: expected INV-02 did not fire (fired INV-01)"
    both = result(m29, "invariant_violation", "INV-01")
    both.produced.invariant_ids = ["INV-01", "INV-02"]                         # INV-02 fired too: first by catalog order
    rep = C.score([both], strict_signals=True)
    assert rep.results[0].state == "detected" and rep.fired[f"M29@{LADDER}"] == ("INV-01", "INV-02") and not rep.unproven


def test_signals_below_precedence_and_from_the_caller_count():
    m03 = with_expected(mutant("M03", "swapped_digit", money=True, outcomes=("assertion_failed",)), assertion_id="qty")
    strict = dict(strict_signals=True)
    assert C.score([result(m03, "assertion_failed", "assertion:qty")], **strict).state == "ok"
    price = result(m03, "assertion_failed", "assertion:price")
    assert C.score([price], **strict).state == "detector_blind"
    price.produced.assertion_ids = ["price", "qty"]
    assert C.score([price], **strict).state == "ok"
    m01 = with_expected(mutant("M01", "erased_control", money=True), invariant_id="INV-10")
    lower = result(m01, "assertion_failed", "assertion:R01")
    lower.produced.lower_results = [{"outcome": "invariant_violation", "reason": "INV-10"}]
    m01 = with_expected(m01, outcomes=("invariant_violation", "assertion_failed"))
    lower.mutant = m01
    assert C.fired_ids(lower.produced) == frozenset({"R01", "INV-10"})
    assert C.score([lower], **strict).state == "ok"
    extra = result(m01, "invariant_violation", "INV-05", fired_ids=("INV-10",))
    assert C.score([extra], **strict).state == "ok"
    assert C.score([result(m01, "invariant_violation", "INV-05")], **strict).state == "detector_blind"


def test_collateral_on_the_region_still_counts_without_the_invariant():
    """10.3: collateral or needs_human on the mutated region counts for every class but the controls."""
    m12 = with_expected(mutant("M12", "overlapped_panels", cid=FLEXGRID, outcomes=("invariant_violation", "collateral")),
                        invariant_id="INV-01")
    on = DiffSummary(components=[Component(area=40, bbox=(110, 105, 4, 4))])
    assert C.score([result(m12, "collateral", diff=on)]).state == "ok"


def test_expected_reason_is_matched():
    m20 = with_expected(mutant("M20", "sub_floor", outcomes=("identical", "noise")), reason="below_floor")
    assert C.score([result(m20, "noise", "below_floor")]).results[0].state == "detected"
    assert C.score([result(m20, "identical")]).results[0].state == "detected"
    m22 = with_expected(mutant("M22", "landmark_loss", outcomes=("landmark_missing",)), reason="landmark:lm.title")
    assert C.score([result(m22, "landmark_missing", "landmark:lm.title")]).state == "ok"
    assert C.score([result(m22, "landmark_missing", "landmark:lm.other")]).unproven
    rep = C.score([result(m22, "landmark_missing", "landmark:lm.other")], strict_signals=True)
    assert rep.results[0].state == "detector_degraded"
    assert rep.results[0].absorbed_by.startswith("signal: expected reason landmark:lm.title, produced landmark:lm.other")


def test_gateway_is_scored_on_the_step_status_without_an_image():
    """12.5 M28: step blocked_by_gateway; a blocked step may leave no record, a step that clicked must keep one."""
    m28 = mutant("M28", "gateway", money=True, cid=TICKET, mode="behavioral", canary_flow="flow.canary.gateway",
                 outcomes=("not_run",), footprint=None)
    m28.mutant_sha256 = m28.base_sha256 = None
    blocked = C.score([C.MutantResult(mutant=m28, step_status="blocked_by_gateway")])
    assert blocked.state == "ok" and blocked.results[0].state == "detected" and not blocked.synthetic
    clicked = C.score([result(m28, "identical", step_status="passed")], since="2026-11-10 03:12 ET")
    assert clicked.state == "detector_blind"
    assert clicked.results[0].absorbed_by.startswith("gateway: step passed, expected blocked_by_gateway")
    with pytest.raises(ValueError, match="gateway mutant may omit it only when its step was blocked"):
        C.score([C.MutantResult(mutant=m28, step_status="passed")])
    custom = with_expected(m28, step_status="failed")
    assert C.score([C.MutantResult(mutant=custom, step_status="failed")]).state == "ok"


def test_a_behavioral_mutant_whose_flow_did_not_run_is_generation_failed():
    """Choice 5: generate leaves a behavioral mutant without an approved capture unrendered; scoring it without a
    flow result is that mutant's generation failure, not a crash."""
    gen = C.generate(C.parse_catalog(CATALOG).entries, {CONFIRM: BASE}, REGIONS)
    m26 = next(m for m in gen.mutants if m.mutant_id == "M26")
    assert m26.image is None and not m26.failed
    rep = C.score([C.MutantResult(mutant=m26)], context=CTX)
    assert rep.results[0].state == "generation_failed" and rep.no_canary_ran
    assert "canary flow flow.canary.ticket-stock did not run" in rep.outcome_rows[0].detail
    ran = C.score([result(m26, "assertion_failed", "assertion:qty")])
    assert ran.results[0].state == "detected" and not ran.no_canary_ran


def test_global_shift_expands_over_the_batch_and_is_judged_once():
    """SD-89: M21 is applied to every bound checkpoint of the night's batch and detected only when the pre-pass
    put every member in one needs_human (global_shift) cluster."""
    m21 = f"""
- mutant_id: M21
  name: mut.charts.shift
  checkpoint_id: {POSITIONS}
  class: global_shift
  mode: image
  money_cell: false
  generator: {{op: translate, params: {{dx: 1}}, seed: 20260101}}
  region_ref: rgn.positions.grid
  expected: {{outcomes: [needs_human], reason: global_shift}}
  owner: sentinel-canaries
"""
    entries = C.parse_catalog(CATALOG + m21).entries
    gen = C.generate(entries, APPROVED, REGIONS)
    batch = [m for m in gen.mutants if m.mutant_id == "M21"]
    assert sorted(m.checkpoint_id for m in batch) == sorted(C.bound_checkpoints(entries)) == sorted(APPROVED)
    assert all(not m.failed and m.footprint is not None for m in batch)
    clustered = [result(m, "needs_human", "global_shift") for m in batch]
    rep = C.score(clustered)
    assert rep.state == "ok" and [r.state for r in rep.results] == ["detected"] * 3
    split = clustered[:2] + [result(batch[2], "collateral")]
    rep = C.score(split, since="2026-11-10 03:12 ET")
    assert [r.state for r in rep.results] == ["detector_degraded"] * 3 and rep.class_rates["global_shift"] == 0.0
    assert rep.results[0].absorbed_by.startswith(
        f"global shift pre-pass: 1 of 3 batch checkpoints outside the global_shift cluster (first {batch[2].checkpoint_id}")


# -- what absorbed a miss (10.3 Scoring) ---------------------------------------------------------------------

def test_absorbed_by_names_only_the_mask_over_the_mutant():
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",), region="rgn.confirm.price",
                 footprint=(100, 100, 20, 10))
    p = produced(m05, "identical")
    masks = [C.MaskArea("msk-clock", "rgn.confirm.clock", (300, 0, 40, 10)),
             C.MaskArea("msk-price", "rgn.confirm.cover", (95, 98, 40, 20))]
    assert C.absorbed_by(m05, p, masks=masks) == "mask msk-price (region rgn.confirm.cover)"
    floors = [C.CalibratedFloor("rgn.confirm.price", "9e1f03", 0.004, fix_item="FL-31", owner="jdoe"),
              C.CalibratedFloor("rgn.confirm.symbol", "9e1f03", 0.0, rect=(100, 100, 20, 10))]
    assert C.absorbed_by(m05, p, floors=floors) == (
        "threshold: calibration for fingerprint 9e1f03 carries a non-zero floor on rgn.confirm.price "
        "(fix-list item FL-31, owner jdoe)")
    rep = C.score([C.MutantResult(mutant=m05, produced=p, absorbed_by=C.absorbed_by(m05, p, floors=floors))],
                  since="2026-11-09 03:12 ET")
    assert "Absorbed by: threshold: calibration for fingerprint 9e1f03" in rep.banner


def test_absorbed_by_from_the_record_alone():
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    noise = produced(m05, "noise", "decision:dec-000517")
    noise.decision_ref = "dec-000517"
    assert C.absorbed_by(m05, noise) == "decision: dec-000517"
    intended = produced(m05, "intended")
    intended.manifest_entry_ref = "me-0042"
    assert C.absorbed_by(m05, intended) == "intended: manifest entry me-0042"
    lost = produced(m05, "landmark_missing", "landmark:lm.confirm-title")
    assert C.absorbed_by(m05, lost) == "landmark: lm.confirm-title not located"
    m25 = mutant("M25", "aa_control", outcomes=("identical",), region=None)
    assert C.absorbed_by(m25, produced(m25, "invariant_violation", "INV-03")) == "finding: invariant_violation (INV-03)"
    assert C.absorbed_by(m05, produced(m05, "identical")) is None
    rep = C.score([C.MutantResult(mutant=m05, produced=noise)])
    assert rep.results[0].absorbed_by == "decision: dec-000517"


# -- rows, synthetic outcomes, coverage (8.3, SD-91, SD-42) --------------------------------------------------

def test_every_mutant_gets_its_synthetic_outcome_row():
    """SD-91: synthetic outcomes go to synthetic_outcomes, one per mutant, the harness_error row replacing a miss."""
    m02 = with_expected(mutant("M02", "erased_control", money=True), invariant_id="INV-10")
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    failed = mutant("M03", "swapped_digit", money=True, error="no glyph inside the target box")
    rep = C.score([result(m02, "invariant_violation", "INV-10"), result(m05, "identical"), C.MutantResult(failed)],
                  context=CTX, watchdog_event="wd-12")
    got = [(s.mutant_id, s.checkpoint_id, s.outcome, s.reason) for s in rep.synthetic]
    assert got == [("M02", CONFIRM, "invariant_violation", "INV-10"), ("M05", CONFIRM, "harness_error", "detector_blind"),
                   ("M03", CONFIRM, "harness_error", "canary_generation_failed")]
    assert all(s.run_id == CTX.run_id for s in rep.synthetic) and rep.synthetic[0].mutant_sha256 == "m" * 64
    assert rep.synthetic[1].detail.startswith("watchdog_event: wd-12; mutant M05 swapped_digit; produced identical")


def test_rows_carry_the_reviewer_and_version_columns():
    m09 = mutant("M09", "shifted_panel", cid=POSITIONS, outcomes=("collateral",))
    p = produced(m09, "needs_human", "reviewer_uncertain")
    p.classification = Classification(category="needs_human", by="reviewer", confidence=0.55, model_id="claude-x",
                                      prompt_hash="ph-2")
    v = C.RowVersions(mask_version="mv-3", threshold_version="cal-sha", model_id="claude-pinned", prompt_hash="ph-1")
    row = C.score([C.MutantResult(mutant=m09, produced=p)], versions=v).results[0]
    assert (row.reviewer_verdict, row.reviewer_confidence, row.model_id, row.prompt_hash) == \
        ("needs_human", 0.55, "claude-x", "ph-2")
    assert (row.mask_version, row.threshold_version) == ("mv-3", "cal-sha")
    rule = C.score([result(m09, "collateral")], versions=v).results[0]
    assert (rule.reviewer_verdict, rule.model_id, rule.prompt_hash) == (None, "claude-pinned", "ph-1")


def test_coverage_gaps_count_live_mutants_and_brief_classes():
    """10.3 Gate and governance and L-MUT-05 (SD-42): fewer than 20 live mutants, or a brief class missing."""
    brief = ("erased_control", "shifted_panel", "swapped_digit", "color_swap", "clipped_label", "overlapped_panels")
    rs = [result(mutant(f"M{40 + i}", brief[i % 6], cid=CONFIRM), "collateral",
                 diff=DiffSummary(components=[Component(area=9, bbox=(105, 105, 3, 3))])) for i in range(20)]
    failed = mutant("M90", "erased_control", cid=FLEXGRID, error="no approved capture for " + FLEXGRID)
    rep = C.score(rs + [C.MutantResult(failed)], context=CTX, critical_checkpoints=[CONFIRM],
                  tier1_checkpoints=[FLEXGRID])
    assert rep.coverage_gaps == [
        f"coverage gap: {FLEXGRID} has 0 live mutants, fewer than 20 (10.3)",
        f"coverage gap: {FLEXGRID} lacks the brief classes {', '.join(brief)} (L-MUT-05)",
    ]
    assert rep.state == "ok"                                     # FLEXGRID is not critical


def test_the_banner_can_be_restamped_with_an_earlier_start():
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    rep = C.score([result(m05, "identical")], since="2026-11-10 03:12 ET")
    assert rep.banner.startswith("DETECTOR BLIND since 2026-11-10 03:12 ET.\n")
    assert rep.banner_for("2026-11-09 03:12 ET") == rep.banner.replace("2026-11-10", "2026-11-09", 1)
    assert C.score([result(m05, "assertion_failed", "assertion:price")]).banner_for("x") is None


def test_canary_rows_take_a_kind_of_the_taxonomy():
    common = dict(catalog_version="v1", checkpoint_id=CONFIRM, seed=C.SEED, expected=["identical"], state="detected")
    C.CanaryResult("run-1", defect_class="path_equivalence", money_cell=True, kind="path_equivalence", **common)
    with pytest.raises(TaxonomyError, match="canary_kinds"):
        C.CanaryResult("run-1", defect_class="pair", money_cell=True, kind="pair", **common)


def test_health_row_and_findings_follow_9_2_and_4_13():
    shifted = [result(mutant(m, "shifted_panel", cid=POSITIONS, outcomes=("collateral",)), "identical")
               for m in ("M08", "M09")]
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    m25 = mutant("M25", "aa_control", outcomes=("identical",), region=None)
    over = result(m25, "invariant_violation", "INV-03")
    m31 = mutant("M31", "misaligned_column", cid=POSITIONS, region="rgn.positions.cell")
    missed = result(m31, "noise", "below_floor")
    failed = mutant("M03", "swapped_digit", money=True, error="no glyph inside the target box")
    rep = C.score(shifted + [result(m05, "assertion_failed", "assertion:price"), over, missed, C.MutantResult(failed)],
                  history={"misaligned_column": [0.0] * 14, "aa_control": [0.0] * 14}, context=CTX)
    row = C.health_row(rep, date="2026-11-10", since_text="2026-11-10 03:12 ET")
    assert row == {"money_detected": 1, "money_total": 1, "other_detected": 0, "other_total": 4,
                   "detector_state": "detector_degraded", "date": "2026-11-10", "since": "2026-11-10 03:12 ET",
                   "classes": ["shifted_panel"]}
    found = C.findings(rep, CTX.run_id, created_at="2026-11-10T08:42:00+00:00")
    kinds = {(f.kind, f.evidence["raw_values"]["mutant_id"]) for f in found}
    assert kinds == {("canary_miss", "M08"), ("canary_miss", "M09"), ("canary_miss", "M31"), ("harness_error", "M03")}
    assert [r.state for r in rep.results if r.mutant_id == "M31"] == ["missed"]          # a plain miss still files one
    gen = next(f for f in found if f.kind == "harness_error")
    assert gen.description.endswith("was not generated: no glyph inside the target box")
    assert len({f.finding_id for f in found}) == len(found)


# -- the Nightly's records in the store (8.3, 9.2, 10.3 Determinism and Result states) -------------------------

from sentinel.store import RunPlan, ScopeEntry, Store                       # noqa: E402

NIGHTS = {1: ("run-2026-11-09-nightly-0001", "2026-11-09T08:12:00+00:00", "2026-11-09 03:12 ET", "2026-11-09"),
          2: ("run-2026-11-10-nightly-0001", "2026-11-10T08:12:00+00:00", "2026-11-10 03:12 ET", "2026-11-10"),
          3: ("run-2026-11-11-nightly-0001", "2026-11-11T08:12:00+00:00", "2026-11-11 03:12 ET", "2026-11-11")}
SCOPE = (CONFIRM, POSITIONS, FLEXGRID, TICKET)


def begin(store: Store, night: int) -> str:
    run_id, at, _, _ = NIGHTS[night]
    store.begin_run(RunPlan(run_id=run_id, run_type="nightly", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=14400, started_at=at,
                            scope=tuple(ScopeEntry(c, "ws.tier-a.active-trader", "active_trader", 1, "frozen",
                                                   flow_class="standard" if c.startswith("flow/") else None)
                                        for c in SCOPE)))
    return run_id


def on_night(night: int, r: C.MutantResult) -> C.MutantResult:
    r.produced = dataclasses.replace(r.produced, run_id=NIGHTS[night][0])
    return r


def save(store: Store, night: int, rep: C.CanaryReport, **kw) -> C.DetectorState:
    run_id, at, text, day = NIGHTS[night]
    return C.persist(store, run_id, rep, since_at=at, since_text=text, date=day, at=at, **kw)


def blind_night(night: int) -> C.CanaryReport:
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    shifted = [on_night(night, result(mutant(m, "shifted_panel", cid=POSITIONS, outcomes=("collateral",)), out))
               for m, out in (("M08", "identical"), ("M09", "collateral"))]
    return C.score([on_night(night, result(m05, "identical"))] + shifted, since=NIGHTS[night][2],
                   history={"shifted_panel": [0.0] * 14})


def green_night(night: int) -> C.CanaryReport:
    m05 = mutant("M05", "swapped_digit", money=True, outcomes=("assertion_failed",))
    shifted = [on_night(night, result(mutant(m, "shifted_panel", cid=POSITIONS, outcomes=("collateral",)), "collateral"))
               for m in ("M08", "M09")]
    return C.score([on_night(night, result(m05, "assertion_failed", "assertion:price"))] + shifted)


def test_persist_writes_the_rows_and_carries_the_banner_until_a_green_night(tmp_path):
    """10.3 Result states, 9.2 Banners: detector_state is written with its start time, kept while the state
    holds, and a green Nightly clears it; PR runs read it from system_state alone."""
    with Store(tmp_path / "sentinel.db") as store:
        n1 = begin(store, 1)
        rep1 = blind_night(1)
        ds1 = save(store, 1, rep1)
        assert (ds1.state, ds1.since, ds1.since_text) == ("detector_blind", NIGHTS[1][1], "2026-11-09 03:12 ET")
        assert ds1.banner == rep1.banner and ds1.banner.startswith("DETECTOR BLIND since 2026-11-09 03:12 ET.\n")
        assert store.canary_results(n1) == rep1.results
        assert store.query("SELECT count(*) FROM canary_results WHERE run_id = ? AND synthetic_ref IS NOT NULL",
                           (n1,))[0][0] == 3
        so = store.query("SELECT mutant_id, outcome, reason FROM synthetic_outcomes WHERE run_id = ? ORDER BY mutant_id",
                         (n1,))
        assert [tuple(r) for r in so] == [("M05", "harness_error", "detector_blind"), ("M08", "identical", None),
                                          ("M09", "collateral", None)]
        assert store.query("SELECT count(*) FROM outcomes WHERE run_id = ?", (n1,))[0][0] == 0   # real rows only
        assert C.read_detector_state(store) == ds1
        assert ds1.health == C.health_row(rep1, date="2026-11-09", since_text="2026-11-09 03:12 ET",
                                          state="detector_blind")
        summary = store.run_summary(n1)["canary"]
        assert summary["detector_state"] == "detector_blind" and summary["since_text"] == "2026-11-09 03:12 ET"

        begin(store, 2)
        ds2 = save(store, 2, blind_night(2))                                 # still blind: the start time holds
        assert ds2.since_text == "2026-11-09 03:12 ET" and ds2.banner.startswith("DETECTOR BLIND since 2026-11-09")
        assert ds2.run_id == NIGHTS[2][0]

        begin(store, 3)
        ds3 = save(store, 3, green_night(3))
        assert (ds3.state, ds3.since, ds3.banner) == ("ok", None, None)
        assert C.read_detector_state(store).state == "ok"


def test_a_diverged_path_row_raises_the_state_and_names_the_pair(tmp_path):
    """10.3 Path equivalence: a pair that does not reproduce is the detector-blind banner, with the pair named."""
    pair = C.CanaryResult(mutant_id="run-2026-11-08-merge-0003", catalog_version="v1", checkpoint_id=POSITIONS,
                          defect_class="path_equivalence", money_cell=True, seed=C.SEED, expected=["collateral"],
                          state="detector_blind", produced_outcome="identical", kind="path_equivalence",
                          absorbed_by="outcome collateral != identical")
    with Store(tmp_path / "sentinel.db") as store:
        n1 = begin(store, 1)
        ds = save(store, 1, green_night(1), extra_rows=[pair])
        assert (ds.state, ds.canary_state, ds.divergence) == ("detector_blind", "ok", True)
        lines = ds.banner.split("\n")
        assert lines[0] == "DETECTOR BLIND since 2026-11-09 03:12 ET."
        assert lines[1].startswith(f"Path equivalence: the merge-run pair {POSITIONS} of run-2026-11-08-merge-0003 "
                                   "did not reproduce")
        assert lines[-1] == "RC sign-off is blocked."
        assert [r.kind for r in store.canary_results(n1)] == ["mutant"] * 3 + ["path_equivalence"]
        drill = dataclasses.replace(pair, kind="fire_drill", defect_class="fire_drill", mutant_id="drill-3")
        begin(store, 2)
        ds2 = save(store, 2, green_night(2), extra_rows=[drill])               # its own 6.5 banner gives the detail
        assert ds2.state == "detector_blind" and not ds2.divergence and "Path equivalence" not in ds2.banner
        begin(store, 3)
        ds3 = save(store, 3, blind_night(3), extra_rows=[dataclasses.replace(pair, mutant_id="run-2026-11-10-merge-0001")])
        lines = ds3.banner.split("\n")
        assert lines[1].startswith("Missed 1 of 1 money-cell mutants: M05") and lines[-3].startswith("Path equivalence")
        assert lines[-2:] == list(C.BLIND_TAIL)


def test_class_history_and_the_previous_manifest_come_from_earlier_nights(tmp_path):
    """10.3: the rolling floor reads each class's last 14 nightly rates; the sha256 check reads last night's
    manifest."""
    entries = C.parse_catalog(CATALOG).entries
    with Store(tmp_path / "sentinel.db") as store:
        begin(store, 1)
        gen1 = C.generate(entries, APPROVED, REGIONS)
        rep1 = blind_night(1)
        save(store, 1, rep1, generation=gen1)
        n2 = begin(store, 2)
        assert C.previous_manifest(store, exclude_run_id=n2) == gen1.manifest
        assert C.class_history(store, exclude_run_id=n2) == {"shifted_panel": [0.5]}
        save(store, 2, green_night(2))
        n3 = begin(store, 3)
        history = C.class_history(store, exclude_run_id=n3)
        assert history == {"shifted_panel": [0.5, 1.0]}
        assert C.rolling_floor(history["shifted_panel"]) == pytest.approx(0.70)
        assert C.class_history(store, exclude_run_id=n3, nights=1) == {"shifted_panel": [1.0]}
        assert C.previous_manifest(store, exclude_run_id=n3) == gen1.manifest   # night 2 stored none
        regen = C.generate(entries, APPROVED, REGIONS, previous_manifest=C.previous_manifest(store, exclude_run_id=n3))
        assert not regen.failed and regen.manifest == gen1.manifest


def test_check_determinism_flags_changed_bytes_and_a_failed_g3_hash():
    manifest = {"M02@x": {"base_sha256": "b1", "mutant_sha256": "m1", "seed": 1},
                "M03@x": {"base_sha256": "b2", "mutant_sha256": "m2", "seed": 1}}
    previous = {"M02@x": {"base_sha256": "b1", "mutant_sha256": "m0", "seed": 1},       # same base and seed: noise
                "M03@x": {"base_sha256": "b0", "mutant_sha256": "m9", "seed": 1}}       # new base: regenerated
    assert C.check_determinism(manifest, previous, None, "cat") == {
        "M02@x": "nondeterministic generator: same base and seed, different mutant sha256"}
    assert C.check_determinism(manifest, None, "cat", "cat") == {}
    assert set(C.check_determinism(manifest, None, "other", "cat")) == {"M02@x", "M03@x"}
