"""A simulated end-to-end Sentinel run over synthetic screens (1.6 to 1.11, 5.17, 10.3).

The thinkorswim app is replaced by `sentinel.synth` and the rig by the Orchestrator's `SimulatedRig`;
everything else is the real core: config lint, scope, the Orchestrator and its safety controls, the
settle rule, the Diff Engine with A/A calibration, the Invariant Checker, the Oracle, classification,
decisions, the store, coverage, canaries and the Reporter.

- `sim`: the simulated builds. It renders the screen of any planned checkpoint for a build that
  carries a set of declared visual changes, returns the settle loop's frames, the bounds-source
  view of the screen (`invariants.ScreenModel`, what Branch B OCR and templates would read) and
  the Region Registry entry cut for the synthetic geometry.
- `reviewer`: an offline, deterministic stand-in for the AI Reviewer (3.12, 3.13), plus the
  adapter interface a real model client would implement. No network call is ever made.
- `run`: `run_pipeline`, one PR run from webhook to PR comment, and `run_nightly`, the Nightly
  canary pass, both writing through `store.Store` and `store.ArtifactStore`.
- `assertions`: the Oracle on the run: every flow checkpoint's assertions on both builds, read from
  what synth drew (`oracle.GroundTruthReader`).
- `orchestration`: the Orchestrator's simulated rig, shard context, outcome sink, alert routes and the
  actions it asks others to persist; every shard of every run goes through the 5.2 gate machine.
- `live`: the real Reviewer adapter (`reviewer.ModelReviewer`) behind `--reviewer live`, with the 3.12
  items cut from the artifact store; network only with `ANTHROPIC_API_KEY` set.
"""
