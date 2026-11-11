"""The run's effective configuration (4.5, 4.7, 4.8, 2.7, 5.9, SD-08, L-SRF-11, L-MSK-02, L-RGN-05)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sentinel import effective as eff
from sentinel import invariants as inv
from sentinel.invariants import GridCell, Rect, ScreenModel, UIComponent, Window
from sentinel.lint.repo import Doc, Repo
from sentinel.lint.rules import ExpiredRecord
from sentinel.lint.runstart import lint_at_run_start
from sentinel.scope import PlannedCheckpoint

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
START = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
SID = "monitor.activity.position-statement"
PLAIN = "monitor.activity.account-info"
SREL = f"registry/surfaces/monitor/{SID}.yaml"
FLOW_CP = f"flow/{SID}/flow.monitor.ps/cp1"
CENSUS_CP = f"census/{SID}/ws.tier-a.active-trader"
FRAME = UIComponent("frame", Rect(0, 0, 480, 360), "panel")
BLOCK = {"min_font_px": 11, "overlap_allowlist": [["crosshair", "*"]], "signed_columns": ["pl_open"],
         "allowed_scrollbars": {"positions-grid": {"w": 2000, "h": 2000}}, "focus_order": ["qty", "price"]}
PALETTE = {"schema_version": 1, "theme": "dark", "tolerance": 50,
           "colors": {"positive": "#00c800", "negative": "#dc0000", "neutral": "#e6e6e6"}}
ERRORS = ("(?i)^(java|exception|error)", "(?i)has stopped working")


def surface(sid: str = SID, **kw) -> Doc:
    data = {"id": sid, "tier": 2, "account_state": True, "labels": ["P/L Open"],
            "controls": [{"id": "grid", "template": "t.png"},
                         {"id": "close-button", "template": "c.png", "required": True, "ocr_text": "Close",
                          "jab": {"name": "Close", "role": "push button"}}], **kw}
    return Doc(f"registry/surfaces/monitor/{sid}.yaml", "surface", data)


FLOW = Doc("flows/tier-2/flow.monitor.ps.yaml", "flow", {"flow_id": "flow.monitor.ps", "steps": [
    {"id": "s1", "type": "click_named", "target": f"{SID}.grid"},
    {"id": "cp1", "type": "checkpoint", "checkpoint_id": FLOW_CP, "surface_id": SID, "tier": 1,
     "account_state": False, "required_labels": ["Mark", "P/L Open"]}]})


def config(*docs: Doc) -> SimpleNamespace:
    return SimpleNamespace(repo=Repo(Path("."), {d.rel: d for d in docs}))


def checkpoint(cid: str = FLOW_CP, sid: str = SID, mode: str = "frozen") -> PlannedCheckpoint:
    flow = cid.startswith("flow/")
    return PlannedCheckpoint(cid, sid, "flow" if flow else "census", 2, "ws.tier-a.active-trader", "active_trader",
                             mode, "flow.monitor.ps" if flow else None, "cp1" if flow else None)


def exception(inv_id: str, expires: str, owner: str = "tos-monitor-ui") -> dict:
    return {"invariant_id": inv_id, "owner": owner, "reason": "wrong for this surface", "expires_at": expires}


SHOWN = UIComponent("pl", Rect(10, 300, 60, 20), "text", parent="frame", label="P/L Open")
CLOSE = UIComponent("close", Rect(200, 300, 60, 20), "control", parent="frame", role="push button", label="Close")


def model(*components, **kw) -> ScreenModel:
    """A Branch A frame that shows the surface's required label and control, plus `components`."""
    return ScreenModel("A", (480, 360), (FRAME, SHOWN, CLOSE, *components), **kw)


def fired(m: ScreenModel, rules: inv.SurfaceRules, config_: inv.CheckerConfig = inv.DEFAULT_CONFIG) -> list[str]:
    return [v.invariant_id for v in inv.check(m, rules, config=config_).violations]


OVERLAP = (UIComponent("a", Rect(0, 0, 100, 100), "panel", parent="frame"),
           UIComponent("b", Rect(50, 50, 100, 100), "panel", parent="frame"))
SCROLLED = (UIComponent("p", Rect(10, 10, 100, 100), "panel", parent="frame", control_id="positions-grid"),
            UIComponent("sb", Rect(100, 10, 10, 100), "scrollbar", parent="p"))


# -- exceptions, masks and floor exceptions at run start ---------------------------------------------------------

def test_an_exception_in_force_suppresses_its_invariant_and_an_expired_one_is_dropped_and_listed():
    doc = surface(invariant_exceptions=[exception("INV-06", "2026-12-01T00:00:00Z"),
                                        exception("INV-01", "2026-11-01T00:00:00Z", owner="tos-ui")])
    reg = eff.effective_registry(config(doc, FLOW), START)
    rules = reg.surface_rules(checkpoint(CENSUS_CP))
    assert [e.invariant_id for e in rules.exceptions] == ["INV-06"]
    fc = inv.check(model(*SCROLLED), rules)
    assert fc.violations == () and fc.excepted == ("INV-06",)
    assert fired(model(*OVERLAP), rules) == ["INV-01"]                                      # its exception expired
    assert reg.dropped == (ExpiredRecord("invariant_exception_expired", "INV-01", SID, SREL,
                                         "invariant_exceptions[1]", "2026-11-01T00:00:00Z", "L-SRF-11", "tos-ui"),)
    assert reg.run_health_lines() == ("invariant exception expired: INV-01",)


def test_expiry_is_decided_at_the_run_start_instant():
    doc = surface(invariant_exceptions=[exception("INV-06", "2026-11-10T13:00:00-05:00")])     # 18:00 UTC
    before = eff.effective_registry(config(doc), datetime(2026, 11, 10, 17, 59, tzinfo=timezone.utc))
    at = eff.effective_registry(config(doc), datetime(2026, 11, 10, 18, 0))                    # naive is UTC
    assert before.exceptions[SID][0].invariant_id == "INV-06" and before.dropped == ()
    assert SID not in at.exceptions and [r.kind for r in at.dropped] == ["invariant_exception_expired"]


def test_expired_masks_and_floor_exceptions_are_dropped_with_their_finding_kinds():
    def mask(mid: str, expires: str) -> Doc:
        return Doc(f"masks/{CENSUS_CP}/{mid}.yaml", "mask", {"mask_id": mid, "checkpoint_id": CENSUS_CP,
                                                             "owner": "tos-monitor-ui", "expires_at": expires})
    fe = {"approved_by": "lead", "reason": "aa noise", "ticket": "SENT-1", "owner": "tos-monitor-ui"}
    regions = Doc(f"regions/{CENSUS_CP}.yaml", "regions", {"checkpoint_id": CENSUS_CP, "regions": [
        {"id": "rgn.a", "type": "diff"},
        {"id": "rgn.b", "type": "diff", "floor_exception": dict(fe, expires_at="2026-11-09T00:00:00Z")},
        {"id": "rgn.c", "type": "diff", "floor_exception": dict(fe, expires_at="2026-11-30T00:00:00Z")}]})
    reg = eff.effective_registry(config(mask("msk.old", "2026-11-10T08:00:00-05:00"),
                                        mask("msk.new", "2026-12-05T15:20:00-05:00"), regions), START)
    assert reg.masks_in_force == {"msk.new"}
    assert [(r.kind, r.record_id, r.scope, r.path) for r in reg.dropped] == [
        ("floor_exception_expired", "rgn.b", CENSUS_CP, "regions[1].floor_exception"),
        ("mask_expired", "msk.old", CENSUS_CP, "expires_at")]
    assert reg.run_health_lines() == ("floor exception expired: rgn.b", "mask expired: msk.old")


def test_the_dropped_records_equal_the_run_start_lints():
    day = date(2027, 1, 10)
    _, expired = lint_at_run_start(EXAMPLE, today=day)
    reg = eff.effective_registry(SimpleNamespace(repo=Repo.load(EXAMPLE)), datetime(2027, 1, 10, tzinfo=timezone.utc))
    assert expired and list(reg.dropped) == expired


# -- checkpoint overrides (4.7) ----------------------------------------------------------------------------------

def test_checkpoint_tier_account_state_and_required_labels_override_the_surface():
    reg = eff.effective_registry(config(surface(), FLOW), START)
    flow, census = checkpoint(), checkpoint(CENSUS_CP)
    assert reg.overrides(flow) == eff.CheckpointOverrides(1, False, ("Mark", "P/L Open"))
    assert reg.overrides(census) == eff.CheckpointOverrides()
    assert (reg.tier(flow), reg.account_state(flow)) == (1, False)
    assert (reg.tier(census), reg.account_state(census)) == (2, True)
    rules = reg.surface_rules(flow)
    assert rules.tier == 1 and [lb.text for lb in rules.labels] == ["P/L Open", "Mark"]
    assert inv.effective_severity("INV-04", tier=rules.tier, pre_existing=False) == "S1"
    assert fired(model(), rules) == ["INV-05"]                                              # Mark is required here
    assert fired(model(), reg.surface_rules(census)) == []


def test_required_controls_carry_their_jab_name_role_and_ocr_anchor():
    rules = eff.effective_registry(config(surface()), START).surface_rules(checkpoint(CENSUS_CP))
    assert rules.controls == (inv.RequiredControl("close-button", role="push button", name="Close", ocr_text="Close"),)


# -- the invariants block (SD-08) and the checker config ----------------------------------------------------------

def test_the_invariants_block_feeds_the_checker_and_its_absence_keeps_the_defaults():
    reg = eff.effective_registry(config(surface(invariants=BLOCK), surface(PLAIN)), START)
    rules, plain = reg.surface_rules(checkpoint(CENSUS_CP)), reg.surface_rules(checkpoint(CENSUS_CP, PLAIN))
    assert (rules.min_font_px, rules.min_cap_height_px) == (11.0, pytest.approx(7.0 * 11 / 9))
    assert (plain.min_font_px, plain.signed_columns, plain.focus_order, plain.allowlist) == (9.0, frozenset(), None, ())
    small = UIComponent("t", Rect(10, 10, 60, 20), "text", parent="frame", font_px=10)
    assert fired(model(small), rules) == ["INV-07"] and fired(model(small), plain) == []
    crosshair = (OVERLAP[0], UIComponent("b", Rect(50, 50, 100, 100), "panel", parent="frame", control_id="crosshair"))
    assert fired(model(*crosshair), rules) == [] and fired(model(*crosshair), plain) == ["INV-01"]
    assert fired(model(*SCROLLED), rules) == [] and fired(model(*SCROLLED), plain) == ["INV-06"]
    assert fired(model(focus_trace=("price", "qty")), rules) == ["INV-13"]
    cfg = eff.checker_config(config(Doc("palettes/dark.yaml", "palette", PALETTE)), "dark", ERRORS)
    red_gain = model(cells=(GridCell("pl_open", Rect(10, 10, 60, 14), text="+12.50", color=(220, 0, 0)),))
    assert fired(red_gain, rules, cfg) == ["INV-15"] and fired(red_gain, plain, cfg) == []


def test_checker_config_reads_the_palette_and_the_error_titles():
    cfg = eff.checker_config(config(Doc("palettes/dark.yaml", "palette", PALETTE)), "dark", ERRORS)
    assert cfg.palette == {"positive": (0, 200, 0), "negative": (220, 0, 0), "neutral": (230, 230, 230)}
    assert cfg.palette_tolerance == 50 and cfg.error_title_patterns == ERRORS
    untold = dict(PALETTE)
    del untold["tolerance"]
    assert eff.checker_config(config(Doc("palettes/dark.yaml", "palette", untold)), "dark", ERRORS).palette_tolerance == 60
    screen = Rect(0, 0, 1920, 1080)
    crash = model(windows=(Window("main", Rect(0, 0, 1920, 1040), "main"),
                           Window("dlg", Rect(800, 400, 300, 200), "owned", title="java.lang.NullPointerException",
                                  owner_screen=screen, error_template=True)), virtual_screen=screen)
    assert fired(crash, inv.SurfaceRules(SID, tier=2), cfg) == ["INV-14"]
    with pytest.raises(ValueError):                                                         # the default is loud
        fired(crash, inv.SurfaceRules(SID, tier=2))


@pytest.mark.parametrize("docs,theme,patterns,rule", [
    ((), "dark", ERRORS, "L-XREF-30"),
    ((Doc("palettes/dark.yaml", "palette", dict(PALETTE, theme="light")),), "dark", ERRORS, "L-XREF-30"),
    ((Doc("palettes/dark.yaml", "palette", dict(PALETTE, colors={"positive": "green"})),), "dark", ERRORS,
     "L-XREF-30"),
    ((Doc("palettes/dark.yaml", "palette", PALETTE),), "dark", (), "L-XREF-12"),
    ((Doc("palettes/dark.yaml", "palette", PALETTE),), "dark", ("(java",), "L-POP-01"),
])
def test_missing_checker_inputs_fail_loudly(docs, theme, patterns, rule):
    with pytest.raises(eff.EffectiveConfigError) as exc:
        eff.checker_config(config(*docs), theme, patterns)
    assert exc.value.rule == rule


def test_the_workspace_theme_picks_the_palette():
    ws = Doc("corpus/workspaces/ws.a/entry.yaml", "workspace", {"id": "ws.a", "display_config": {"theme": "dark"}})
    bare = Doc("corpus/workspaces/ws.b/entry.yaml", "workspace", {"id": "ws.b"})
    assert eff.workspace_theme(config(ws, bare), "ws.a") == "dark"
    for wid in ("ws.b", "ws.missing"):
        with pytest.raises(eff.EffectiveConfigError) as exc:
            eff.workspace_theme(config(ws, bare), wid)
        assert exc.value.rule == "L-WSP-00"
    assert eff.workspace_theme(SimpleNamespace(repo=Repo.load(EXAMPLE)), "ws.tier-a.active-trader") == "dark"


@pytest.mark.parametrize("docs,cp,rule", [
    ((surface(),), checkpoint(CENSUS_CP, "monitor.unknown"), "L-XREF-11"),
    ((surface(invariant_exceptions=[{"invariant_id": "INV-06", "expires_at": "2026-12-01T00:00:00Z"}]),),
     None, "L-SRF-11"),
    ((surface(invariant_exceptions=[exception("INV-99", "2026-12-01T00:00:00Z")]),), None, "L-SRF-11"),
    ((surface(invariant_exceptions=[exception("INV-06", "soon")]),), None, "L-SRF-11"),
    ((Doc(f"masks/{CENSUS_CP}/m.yaml", "mask", {"mask_id": "m", "expires_at": 5}),), None, "L-MSK-02"),
    ((surface(invariants={"overlap_allowlist": [["crosshair"]]}),), checkpoint(CENSUS_CP), "L-XREF-30"),
])
def test_unusable_registry_inputs_fail_loudly(docs, cp, rule):
    with pytest.raises(eff.EffectiveConfigError) as exc:
        reg = eff.effective_registry(config(*docs), START)
        reg.surface_rules(cp)
    assert exc.value.rule == rule
