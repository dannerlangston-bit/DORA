"""Capture's window rules and frame mechanics (3.3, 3.4, 3.5, 5.7, 7.8, 7.9): window selection, the target window,
surface headers, the occlusion grid, the modal check, the DPI self-test, crops and the composite, the masked_live
median and the capture metadata. Synthetic windows and frames only."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from sentinel.capture import (CaptureError, ForeignWindowLog, Sweep, TargetWindow, capture_meta, choose_target,
                              composite, composite_jpeg, crop_windows, dpi_self_test, foreign_window_event,
                              header_levels, masked_live_median, masked_live_offsets, meta_from_json, meta_to_json,
                              modal_blocking, occluded, occlusion, occlusion_grid, pid_tree, put_capture,
                              redact_text, select_windows, surface_check)
from sentinel.diff import load_params
from sentinel.diff.settle import Frame as SettleFrame
from sentinel.diff.settle import SettleSpec, settle
from sentinel.orchestrator.ports import Frame, PointOwner, ProcessInfo, WindowInfo
from sentinel.store.artifacts import ArtifactStore, Redaction, RedactionRefused, sha256_hex

SCREEN = (-400, 0, 2320, 1080)            # a 400 px monitor left of the 1920 x 1080 primary
TREE = frozenset({100, 200})
MAIN_RX = r"^thinkorswim"
CID = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"


def win(hwnd, pid=100, cls="SunAwtFrame", title="thinkorswim [build 1987]", rect=(0, 0, 1920, 1040), z=0, **kw):
    return WindowInfo(hwnd=hwnd, pid=pid, cls=cls, title=title, rect=rect, z_order=z, **kw)


MAIN = win(1, z=2, monitor=1)
DIALOG = win(2, pid=200, cls="SunAwtDialog", title="Order Confirmation", rect=(700, 300, 520, 360), z=0,
             owner_hwnd=1)
DETACHED = win(3, cls="SunAwtFrame", title="Active Trader - /ES", rect=(-380, 100, 360, 480), z=1)


# -- window selection -----------------------------------------------------------------------------------------------

def test_pid_tree_takes_every_descendant_of_the_roots():
    procs = [ProcessInfo(100, 1, "thinkorswim.exe"), ProcessInfo(200, 100, "java"), ProcessInfo(300, 200, "x"),
             ProcessInfo(400, 1, "explorer.exe")]
    assert pid_tree([100], procs) == {100, 200, 300}
    assert pid_tree([100, 400], procs) == {100, 200, 300, 400}


def test_select_windows_keeps_the_pid_tree_sunawt_windows_and_logs_the_rest():
    foreign = win(10, pid=999, title="Windows Update", z=0)
    ime = win(11, cls="MSCTFIME UI", title="")
    hidden = win(12, cls="SunAwtWindow", visible=False)
    canvas = win(13, cls="SunAwtCanvas", title="", rect=(10, 10, 50, 50), z=3)
    off = win(14, cls="SunAwtWindow", title="tooltip", rect=(5000, 5000, 100, 40), z=4)
    sel = select_windows([MAIN, foreign, ime, hidden, DIALOG, canvas, off, DETACHED], TREE, SCREEN)
    assert [w.hwnd for w in sel.kept] == [2, 3, 1, 13, 14]           # z-order, topmost first
    assert {w.hwnd for w in sel.unknown_class} == {11, 13}           # logged unknown_window_class (5.7)
    assert [w.hwnd for w in sel.offscreen] == [14]                   # INV-09's input (3.4 rule 1)


# -- target window ----------------------------------------------------------------------------------------------------

KEPT = (DIALOG, DETACHED, MAIN)


def test_choose_target_main_and_dialog_by_title_regex():
    main = choose_target(KEPT, TargetWindow("main"), virtual_screen=SCREEN, main_title_regex=MAIN_RX)
    assert main.window is MAIN and main.terminal is None
    assert {w.hwnd for w in main.others} == {2, 3}                   # listed in metadata (3.4 rule 3)
    dlg = choose_target(KEPT, TargetWindow.from_dict({"kind": "dialog", "title_regex": "^Order Confirmation"}),
                        virtual_screen=SCREEN)
    assert dlg.window is DIALOG


def test_choose_target_main_is_a_frame_only():
    imposter = win(5, cls="SunAwtDialog", title="thinkorswim error", z=0)
    got = choose_target((imposter, MAIN), TargetWindow("main"), virtual_screen=SCREEN, main_title_regex=MAIN_RX)
    assert got.window is MAIN


def test_missing_target_is_window_mismatch_with_the_side():
    got = choose_target(KEPT, TargetWindow("dialog", "^Order Rejected"), virtual_screen=SCREEN, side="candidate")
    assert got.window is None
    t = got.terminal
    assert (t.outcome, t.reason, t.side) == ("not_comparable", "window_mismatch", "candidate")
    assert "Order Rejected" in t.detail


@pytest.mark.parametrize("rect", [(3000, 0, 400, 300), None])
def test_target_outside_the_virtual_screen_or_without_bounds_is_window_mismatch(rect):
    lost = win(6, cls="SunAwtDialog", title="Order Confirmation", rect=rect)
    got = choose_target((lost, MAIN), TargetWindow("dialog", "^Order Confirmation"), virtual_screen=SCREEN)
    assert got.window is None and got.terminal.reason == "window_mismatch"
    assert "virtual screen" in got.terminal.detail


def test_choose_target_prefers_the_topmost_match_and_honours_the_template():
    a = win(7, cls="SunAwtDialog", title="Order Confirmation", rect=(0, 0, 100, 80), z=0)
    b = win(8, cls="SunAwtDialog", title="Order Confirmation", rect=(200, 0, 100, 80), z=1)
    assert choose_target((b, a), TargetWindow("dialog", "^Order"), virtual_screen=SCREEN).window is a
    img = np.zeros((200, 400, 3), np.uint8)
    tpl = np.random.default_rng(1).integers(0, 256, (20, 30, 3), dtype=np.uint8)
    img[30:50, 240:270] = tpl                                       # only inside window b
    frame = Frame(img, (0, 0), 0.0)
    got = choose_target((a, b), TargetWindow("dialog", "^Order"), virtual_screen=SCREEN, template=tpl, frame=frame)
    assert got.window is b
    none = choose_target((a,), TargetWindow("dialog", "^Order"), virtual_screen=SCREEN, template=tpl, frame=frame)
    assert none.terminal.reason == "window_mismatch" and "template" in none.terminal.detail


# -- surface headers --------------------------------------------------------------------------------------------------

LEVELS = header_levels([{"title": "Trade", "aliases": ["Trade Tab"]}, {"title": "Active Trader"}])


def test_surface_check_passes_matching_headers_case_and_space_blind():
    assert surface_check(LEVELS, base=["Monitor", "trade", "Active  Trader"], candidate=["Trade Tab",
                                                                                          "ACTIVE TRADER"]) is None


def test_wrong_active_tab_is_surface_mismatch_naming_the_side():
    t = surface_check(LEVELS, base=["Trade", "Active Trader"], candidate=["Charts", "Active Trader"])
    assert (t.outcome, t.reason, t.side) == ("not_comparable", "surface_mismatch", "candidate")
    assert "Charts" in t.detail
    both = surface_check(LEVELS, base=["Trade"], candidate=["Monitor"])
    assert both.side == "both"
    assert surface_check(LEVELS, base=None, candidate=["Trade", "Active Trader"]) is None


# -- occlusion --------------------------------------------------------------------------------------------------------

def test_occlusion_grid_is_64_cell_centres_inside_the_rect():
    pts = occlusion_grid((100, 50, 800, 400))
    assert len(pts) == 64 and len(set(pts)) == 64
    assert pts[0] == (150, 75) and pts[-1] == (850, 425)
    assert all(100 <= x < 900 and 50 <= y < 450 for x, y in pts)


def _samples(foreign_at=None, pid=4242, title="Windows Update"):
    out = []
    for i, (x, y) in enumerate(occlusion_grid(DIALOG.rect)):
        if i == foreign_at:
            out.append(PointOwner(x, y, 77, pid, title))
        else:
            out.append(PointOwner(x, y, 2, 200, "Order Confirmation"))
    return out


def test_one_foreign_sample_of_64_is_an_occluder_with_pid_and_title():
    assert occlusion(_samples(), TREE) is None
    unknown = [PointOwner(1, 1, None, None)] + _samples()             # an unread owner is not foreign
    assert occlusion(unknown, TREE) is None
    occ = occlusion(_samples(foreign_at=37), TREE)
    assert (occ.pid, occ.title, occ.hwnd) == (4242, "Windows Update", 77)
    assert occ.point == occlusion_grid(DIALOG.rect)[37]


def test_occluded_is_env_error_running_with_one_alert_and_finding_per_image_per_run():
    log = ForeignWindowLog()
    occ = occlusion(_samples(foreign_at=0), TREE)
    first = occluded(occ, image_name="MusNotifyIcon.exe", log=log)
    o = first.checkpoint
    assert (o.outcome, o.reason, o.gate) == ("env_error", "occluded", "running")
    assert "pid 4242" in o.detail and "Windows Update" in o.detail
    assert first.outcomes[0].selector == "in_progress"
    assert first.alert is not None and [f.kind for f in first.findings] == ["hardening"]
    again = occluded(occ, image_name="musnotifyicon.EXE", log=log)
    assert (again.checkpoint.outcome, again.checkpoint.reason) == ("env_error", "occluded")
    assert again.alert is None and again.findings == ()
    other = occluded(occ, image_name="Teams.exe", log=log, gate="workspace_loaded")
    assert other.alert is not None and other.checkpoint.gate == "workspace_loaded"


def test_foreign_window_event_is_a_step_event_row():
    occ = occlusion(_samples(foreign_at=5), TREE)
    ev = foreign_window_event(occ, run_id="run-1", shard_id="s1", side="candidate", seq=9,
                              wall_at="2026-11-10T12:00:00+00:00", mono_ms=1234, checkpoint_id=CID)
    assert ev.action == "foreign_window" and ev.popup_title == "Windows Update"
    assert (ev.screen_x, ev.screen_y) == occ.point and "4242" in ev.reason


# -- modal ------------------------------------------------------------------------------------------------------------

def test_modal_blocking_names_an_unexpected_modal_only():
    disabled = win(1, enabled=False, z=2)
    error = win(9, cls="SunAwtDialog", title="Error", owner_hwnd=1, z=1)
    stray = win(10, cls="SunAwtWindow", title="Tip", z=0)
    assert modal_blocking([MAIN, DIALOG], main_hwnd=1) is None                       # main enabled
    assert modal_blocking([disabled, stray, error], main_hwnd=1) == "Error"           # owned beats topmost
    assert modal_blocking([disabled, DIALOG], main_hwnd=1, target_hwnd=2) is None     # the expected dialog
    assert modal_blocking([disabled], main_hwnd=1) == "untitled"


def test_modal_title_feeds_the_settle_loop_to_unexpected_window():
    disabled = win(1, enabled=False)
    error = win(9, cls="SunAwtDialog", title="Error", owner_hwnd=1)
    title = modal_blocking([disabled, error], main_hwnd=1)
    img = np.zeros((40, 40, 3), np.uint8)
    frames = [SettleFrame(t, img, modal_title=title) for t in range(0, 16000, 250)]
    r = settle(frames, SettleSpec())
    assert (r.outcome, r.reason) == ("not_settled", "unexpected_window:Error")


# -- DPI self-test ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["PER_MONITOR_AWARE_V2", "per-monitor-aware-v2"])
def test_per_monitor_v2_passes(value):
    assert dpi_self_test(value) is None


@pytest.mark.parametrize("value", ["PER_MONITOR_AWARE", "SYSTEM_AWARE", None])
def test_any_other_awareness_aborts_the_run(value):
    r = dpi_self_test(value)
    assert (r.run.status, r.run.reason) == ("aborted_harness", "dpi_awareness")
    assert (r.unreached.outcome, r.unreached.reason) == ("harness_error", "dpi_awareness")
    assert r.gate == "provisioned" and r.has("teardown")


# -- crops and composite ----------------------------------------------------------------------------------------------

def _screen():
    """A virtual screen grab with origin (-400, 0): two windows painted with distinct patterns."""
    rng = np.random.default_rng(3)
    img = np.full((1080, 2320, 3), 30, np.uint8)
    for w in (MAIN, DETACHED):
        x, y, ww, hh = w.rect
        img[y:y + hh, x + 400:x + 400 + ww] = rng.integers(0, 256, (hh, ww, 3), dtype=np.uint8)
    return Frame(img, (-400, 0), 1.0)


def test_crops_equal_the_window_pixels_at_their_bounds():
    frame = _screen()
    half_off = win(20, rect=(-500, 0, 200, 100))                      # 100 px off the left edge
    gone = win(21, rect=(5000, 0, 10, 10))
    blind = win(22, rect=None)
    crops = crop_windows(frame, [MAIN, DETACHED, half_off, gone, blind])
    assert set(crops) == {1, 3, 20}
    assert np.array_equal(crops[1], frame.image[0:1040, 400:2320])
    assert np.array_equal(crops[3], frame.image[100:580, 20:380])
    assert crops[20].shape[:2] == (100, 100)
    crops[1][:] = 0
    assert frame.image[5, 405].any()                                  # crops are copies


def test_composite_pastes_redacted_crops_fills_the_rest_and_encodes_jpeg():
    frame = _screen()
    red = crop_windows(frame, [MAIN])
    red[1][10:30, 10:60] = (7, 7, 7)                                  # a redacted box
    img = composite(frame, [MAIN, DETACHED], red, fill=(1, 2, 3))
    assert np.array_equal(img[20, 430], (7, 7, 7))                    # the redacted pixels, not the grab's
    assert np.array_equal(img[300, 200], (1, 2, 3))                   # no redacted crop: filled
    assert np.array_equal(img[0, 400], (0, 255, 255))                 # the rect outline
    assert np.array_equal(img[1070, 10], frame.image[1070, 10])       # outside every window: the grab
    leaky = win(1, title="thinkorswim - Jane Doe")
    shown = composite(frame, [win(1, title="thinkorswim - <redacted:display_name>")], red, fill=(1, 2, 3))
    assert np.array_equal(composite(frame, [leaky], red, fill=(1, 2, 3), sweep=Sweep(display_name="Jane Doe")),
                          shown)                                            # the name is never drawn
    jpg = composite_jpeg(img)
    assert jpg[:2] == b"\xff\xd8" and cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR).shape == img.shape
    with pytest.raises(CaptureError):
        composite(frame, [MAIN], {1: red[1][:10]}, fill=(0, 0, 0))


def test_composite_paints_bottom_up_so_the_topmost_window_shows_where_windows_overlap():
    # capture-eval:1, 5.7 Grab: the composite shows the screen's instant, so the target dialog (z 0) over the main
    # frame (z 2) shows its own redacted crop, whatever order the windows come in (select_windows: topmost first).
    frame = _screen()
    kept = select_windows([MAIN, DIALOG], TREE, SCREEN).kept
    assert [w.hwnd for w in kept] == [2, 1]
    dlg = np.full((DIALOG.rect[3], DIALOG.rect[2], 3), 255, np.uint8)        # only the target has a redacted crop
    cx, cy = DIALOG.rect[0] + 400 + 260, DIALOG.rect[1] + 180                  # the dialog's centre on the grab
    for order in (kept, tuple(reversed(kept))):
        img = composite(frame, order, {2: dlg}, fill=(7, 7, 7))
        assert np.array_equal(img[cy, cx], (255, 255, 255))                    # the dialog, not main's fill
        assert np.array_equal(img[1000, 100 + 400], (7, 7, 7))                 # main outside the dialog: filled
        assert np.array_equal(img[DIALOG.rect[1], cx], (0, 255, 255))         # the dialog's outline is drawn


# -- masked_live (7.9, 3.5) -------------------------------------------------------------------------------------------

def test_masked_live_offsets_from_the_config():
    assert masked_live_offsets(load_params()) == (0, 250, 500, 750, 1000)


@pytest.mark.parametrize("frames,interval", [(3, 250), (6, 250), (5, 500), (2, 1000)])
def test_masked_live_parameters_outside_7_9_or_without_a_settled_majority_are_refused(frames, interval):
    p = load_params()
    bad = {**p, "settle": {**p["settle"], "masked_live": {"frames": frames, "interval_ms": interval}}}
    with pytest.raises(CaptureError):
        masked_live_offsets(bad)


def _live_frames():
    """Five frames: the settled triple identical outside the quote mask (a flash inside it in frame 2), then two
    frames where the quote keeps moving and, in the last, a pixel outside the mask too."""
    rng = np.random.default_rng(5)
    base = rng.integers(0, 256, (60, 80, 3), dtype=np.uint8)
    mask = np.zeros((60, 80), bool)
    mask[10:20, 10:40] = True
    frames = [base.copy() for _ in range(5)]
    frames[1][mask] = (0, 255, 0)                                     # the quote flash, recorded under its mask
    frames[3][mask] = (255, 0, 0)
    frames[4][mask] = (0, 0, 255)
    frames[4][50, 70] = (1, 1, 1)
    return frames, mask


def test_masked_live_median_equals_the_settled_frame_outside_masks():
    frames, mask = _live_frames()
    ml = masked_live_median(frames, (0, 250, 500, 750, 1000), ignore=mask)
    assert np.array_equal(ml.median[~mask], frames[0][~mask])
    assert ml.span_ms == 1000 and len(ml.frames) == 5
    assert (ml.frames[1][mask] == (0, 255, 0)).all()                 # the flash is kept, never diffed


def test_masked_live_median_refuses_bad_frame_sets():
    frames, mask = _live_frames()
    with pytest.raises(CaptureError):
        masked_live_median(frames[:2], (0, 1000))                      # fewer than 3
    with pytest.raises(CaptureError):
        masked_live_median(frames[:3], (0, 250, 500))                  # span under 1 s
    with pytest.raises(CaptureError):
        masked_live_median(frames[:3], (0, 1000, 1000))                # times must increase
    with pytest.raises(CaptureError):
        masked_live_median(frames[:2] + [frames[2][:10]], (0, 500, 1000))
    noisy = [frames[0], frames[0], *[np.full_like(frames[0], v) for v in (10, 20, 30)]]
    with pytest.raises(CaptureError):
        masked_live_median(noisy, (0, 250, 500, 750, 1000), ignore=mask)   # not starting with a settled triple


# -- capture metadata (3.3, 5.7, 8.4) ---------------------------------------------------------------------------------

def _meta(png: bytes, version: str = "redaction-1-abc"):
    window = win(2, cls="SunAwtDialog", title="Order Confirmation D-12345678 jane.doe@example.com",
                 rect=(700, 300, 520, 360), z=0, monitor=1)
    return capture_meta(sha256=sha256_hex(png), run_id="run-1", checkpoint_id=CID, side="candidate", window=window,
                        captured_at="2026-11-10T12:00:01+00:00", sweep=Sweep(display_name="Jane Doe"), dpi=96,
                        scale_percent=100, dpi_awareness="PER_MONITOR_AWARE_V2", fingerprint_id="fp-1",
                        app_uptime_s=412, app_restart_count=1, mode="scripted", action_path="s05>s06>s07",
                        settle_ms=740, redaction_version=version, redaction_boxes=3)


def _png() -> bytes:
    ok, buf = cv2.imencode(".png", np.full((36, 52, 3), 90, np.uint8))
    assert ok
    return buf.tobytes()


def test_capture_meta_carries_every_field_with_the_title_redacted():
    meta = _meta(_png())
    assert all(v is not None for v in meta.to_dict().values()) and len(meta.to_dict()) == 21
    assert (meta.window_class, meta.rect, meta.z_order, meta.monitor) == ("SunAwtDialog", (700, 300, 520, 360), 0, 1)
    assert "D-12345678" not in meta.title and "example.com" not in meta.title
    assert meta.title == "Order Confirmation <redacted:paper_id> <redacted:email>"
    assert meta_from_json(meta_to_json(meta)) == meta
    with pytest.raises(CaptureError):
        capture_meta(sha256="x", run_id="r", checkpoint_id=CID, side="base", window=win(1, rect=None),
                     captured_at="2026-11-10T12:00:01+00:00")


def test_redact_text_merges_overlapping_hits_and_keeps_the_rest():
    sweep = Sweep(literals=("D-48201937",), display_name="Jane Q Doe")
    assert redact_text("thinkorswim [build 1987.12] - Jane Q Doe", sweep) == \
        "thinkorswim [build 1987.12] - <redacted:display_name>"
    assert redact_text("acct D-48201937 / doe", sweep) == "acct <redacted:literal_id> / <redacted:display_name>"
    assert redact_text("Order Confirmation", sweep) == "Order Confirmation"


def test_capture_meta_round_trips_through_the_artifact_store(tmp_path):
    png = _png()
    meta = _meta(png)
    arts = ArtifactStore(tmp_path, "runs/run-1")
    header = Redaction("redaction-1-abc")
    image, sidecar = put_capture(arts, png, header, name=f"{CID}/candidate/capture.png", meta=meta)
    assert image.sha256 == meta.sha256 and image.kind == "capture" and sidecar.kind == "bounds_json"
    assert sidecar.name.endswith("capture.png.meta.json")
    assert arts.get(image.sha256) == png
    assert meta_from_json(arts.get(sidecar.sha256)) == meta
    with pytest.raises(CaptureError):
        put_capture(arts, png + b"x", header, name="other.png", meta=meta)
    with pytest.raises(CaptureError):
        put_capture(arts, png, Redaction("redaction-1-zzz"), name="other.png", meta=meta)
    with pytest.raises(RedactionRefused):
        put_capture(arts, png, Redaction("redaction-1-abc", passed=False), name="other.png", meta=meta)
