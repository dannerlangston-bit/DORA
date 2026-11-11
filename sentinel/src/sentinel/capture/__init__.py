"""Capture's platform-independent stages (5.7). The grab, the window reads and `WindowFromPoint` run on the rig
through `orchestrator.ports.DesktopPort`; the window rules of 3.4, the crops, the composite, the masked_live median
and the capture metadata are `capture`, the size-sweep measurement of 2.5.1 is `sweep`, and the redaction stage of
3.6 and 6.10 is `redaction`.

`redaction` is imported first: `orchestrator.simrig` imports `h6_self_test` from here, and `capture` imports the
orchestrator package, so the name must exist before `capture` starts that import."""
from .redaction import (OcrWord, RedactionContext, RedactionError, RedactionList, RedactionRegion, RedactionResult,
                        SelfTest, Sweep, SweepHit, SyntheticOcr, WordOcr, h6_frame, h6_self_test, redact_pair,
                        redaction_version, sample_artifacts, verify)
from .capture import (CaptureError, ForeignWindowLog, MaskedLiveFrames, Occluder, TargetChoice, TargetWindow,
                      WindowSelection, capture_meta, choose_target, composite, composite_jpeg, crop_windows,
                      dpi_self_test, foreign_window_event, header_levels, masked_live_median, masked_live_offsets,
                      meta_from_json, meta_to_json, modal_blocking, occluded, occlusion, occlusion_grid, pid_tree,
                      put_capture, redact_text, select_windows, surface_check)
from .sweep import (MinSize, SizeLadder, SizeStepResult, SweepError, check_size_reached, classify_axes,
                    classify_size_step, compare_minimums, minimums_summary, size_step_event)

__all__ = [
    "CaptureError", "ForeignWindowLog", "MaskedLiveFrames", "Occluder", "TargetChoice", "TargetWindow",
    "WindowSelection", "capture_meta", "choose_target", "composite", "composite_jpeg", "crop_windows",
    "dpi_self_test", "foreign_window_event", "header_levels", "masked_live_median", "masked_live_offsets",
    "meta_from_json", "meta_to_json", "modal_blocking", "occluded", "occlusion", "occlusion_grid", "pid_tree",
    "put_capture", "redact_text", "select_windows", "surface_check",
    "OcrWord", "RedactionContext", "RedactionError", "RedactionList", "RedactionRegion", "RedactionResult",
    "SelfTest", "Sweep", "SweepHit", "SyntheticOcr", "WordOcr", "h6_frame", "h6_self_test", "redact_pair",
    "redaction_version", "sample_artifacts", "verify",
    "MinSize", "SizeLadder", "SizeStepResult", "SweepError", "check_size_reached", "classify_axes",
    "classify_size_step", "compare_minimums", "minimums_summary", "size_step_event",
]
