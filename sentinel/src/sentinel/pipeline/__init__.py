"""A simulated end-to-end Sentinel run over synthetic screens (1.6 to 1.11, 5.17, 10.3).

The thinkorswim app is replaced by `sentinel.synth`; everything else is the real core: config
lint, scope, the settle rule, the Diff Engine with A/A calibration, the Invariant Checker,
classification, decisions, the store, coverage, canaries and the Reporter.

- `sim`: the simulated builds. It renders the screen of any planned checkpoint for a build that
  carries a set of declared visual changes, returns the settle loop's frames, the bounds-source
  view of the screen (`invariants.ScreenModel`, what Branch B OCR and templates would read) and
  the Region Registry entry cut for the synthetic geometry.
- `reviewer`: an offline, deterministic stand-in for the AI Reviewer (3.12, 3.13), plus the
  adapter interface a real model client would implement. No network call is ever made.
- `run`: `run_pipeline`, one PR run from webhook to PR comment, and `run_nightly`, the Nightly
  canary pass, both writing through `store.Store` and `store.ArtifactStore`.
"""
