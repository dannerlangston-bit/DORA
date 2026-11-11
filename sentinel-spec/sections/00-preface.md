## Read this first: how Sentinel works in one page

**What it is for.** A change to one part of thinkorswim breaks another part, and nobody looks there. The standard example: a Flexible Grid change makes the Active Trader ladder overlap its order buttons at one cell size. User testing of Flexible Grid passes, and the ladder bug ships. Sentinel drives the whole desktop product the way a user does, compares every screen against the build before the change, and reports two things for every change. First, the intended change, with before and after pictures, so the developer no longer hand-screenshots their work. Second, anything else that moved or broke, ranked, with the reason it was tested.

**How it decides what changed and why.** Attribution comes from a build pair, not from AI. A PR run compares the branch build with its merge-base main build (or the nearest earlier main build, at most 20 commits back), a merge run compares new main with previous main, and an RC run compares the release candidate with the last released build. Both builds run on identically fingerprinted rigs against the same data. If a screen differs, the difference was caused by the code between those two builds.

**How it tests.** Five kinds of test feed one result model:

1. **Surface census.** Every tab, gadget, dialog, ticket variant and settings page in the Surface Registry is visited in each corpus workspace, captured, and diffed. This finds side effects nobody predicted.
2. **Size sweeps.** Every resizable gadget is placed in a Flexible Grid cell and stepped through a ladder of sizes and splitter positions. This is where the ladder overlap is found.
3. **Scripted flows.** Interactive states such as building an order, opening the Order Confirmation dialog or adding a study, with assertions on the values shown.
4. **Layout invariants.** Checks that need no baseline: no overlapping controls, nothing outside its parent, no clipped text, required labels present. They fire on the first run of any new layout.
5. **Oracle checks.** Numbers are read by OCR, export and API, then checked against expectations and arithmetic rules such as quantity times price equals cost.

A bounded Explorer agent runs last in RC runs to find edge cases. Every finding is replayed once before anyone sees it.

**How it decides scope.** The developer's change manifest names what was meant to change. The Dependency Map, plus a static impact step over the diff, lists every surface that consumes what changed. A PR run tests those surfaces plus a smoke set within 45 minutes. Merge runs test everything within 3 hours, and RC runs test everything deeper within 8 hours.

**How it classifies a difference.** A difference on a declared surface that the reviewer confirms matches the manifest is `intended`. A difference on an undeclared surface is `collateral`, the top-priority finding. A difference below the measured noise floor is `noise`. Invariant violations and failed assertions stand on their own and no AI can overrule them. The AI reviewer explains diffs and checks them against the manifest. It never approves baselines, never creates masks, and never changes a rule-based outcome.

**What "no gaps" means here.** No corner of the app is silent. Every checkpoint ends in exactly one outcome from a closed set of 25, with a reason and a named place a human sees it (section 1.10, reason table in 12.2). Every surface is validated against the running app on every run. Nightly synthetic faults prove the detector can still see a missing button or a changed digit. Every unknown about the app has both branches written down before the Phase 0 spike runs.

**Safety.** No order is ever sent by default. One input gateway is the only path to the keyboard and mouse. While a ticket or confirm dialog is open, it drops Enter, Space and order hotkeys. The settings that could bypass confirmation are attested at the start of every run. Paper mode is asserted from the account selector against an exact allowlist. A nightly fire drill proves each block works.

**Where to go next.**

| If you want | Read |
|---|---|
| The end-to-end story, timelines and the worked ladder example | 1.6 to 1.11 |
| Exactly what is tested and how scope is computed | 2 |
| How diffs are measured and classified | 3 |
| Every input file and schema | 4 |
| Components, the input gateway and the checkpoint sequence | 5 |
| Safety controls and their proofs | 6 |
| Rigs, state, logins and data modes | 7 |
| Database and artifacts | 8 |
| PR comment, triage, alerts and metrics | 9 |
| How the system proves it works, including the day-one acceptance test | 10 |
| Fleet, cost, Phase 0 decisions, phases and risks | 11 |
| Reference tables: glossary, outcomes, reasons, invariants, mutants, checklists, gap traceability | 12 |

**What Phase 0 must settle before code is written.** There are seven decisions, each with both branches already specified (11.8): Java Access Bridge exposure, a deterministic market-data fixture, login and MFA arrangement, pinned build install, the measured noise floor, the session rule, and whether the security ruling allows the model endpoint and agent mode.
