"""The Recorder (5.16, 11.5 Recorder, 12.1 Recorder and Anchor patch, 11.9 Phase 3).

A flow owner authors an anchored 4.7 flow by performing it once on an authoring rig, never a run rig. The global
hooks, the screenshots and the authoring UI are the Windows track; this package is the platform-independent core:

- `core`: `RecordingSession` (the pause on the login dialog, a password field or the hotkey, and its keystroke-log
  assertion; the refusal on a rig host), `recorded_events_to_flow` (an event log to a flow with 40x20 image anchors,
  OCR text anchors and JAB contexts on new Surface Registry controls) and `suggest_anchors`.
- `save`: `check_and_emit`, the save-time checks (template uniqueness, the secret scan of the keystroke log and of
  every screenshot's OCR text, the `order_stage` prompts, the lint) before anything reaches sentinel-config.
- `anchor_patch`: `anchor_patch.anchor_patch`, the anchor patch of a `locator_failure` finding whose agent rescue
  succeeded (its final target and crop), and the re-anchor view of broken anchors beside those targets.
"""
from .anchor_patch import AnchorPatch, ReanchorItem, reanchor_items
from .core import (FlowHeader, RecordedEvent, RecordedFlow, Recording, RecordingSession, RecorderRefused,
                   recorded_events_to_flow, refuse_rig_host, suggest_anchors)
from .save import SaveResult, check_and_emit

__all__ = ["AnchorPatch", "FlowHeader", "ReanchorItem", "RecordedEvent", "RecordedFlow", "Recording",
           "RecordingSession", "RecorderRefused", "SaveResult", "check_and_emit", "reanchor_items",
           "recorded_events_to_flow", "refuse_rig_host", "suggest_anchors"]
