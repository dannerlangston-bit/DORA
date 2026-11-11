# Sentinel user testing guide

This guide is for the people who will judge whether Sentinel does its job. You don't need to read the spec or the code.

## What you are testing

Sentinel runs thinkorswim the way a user does, compares every screen with the build before a change, and posts what it found on the developer's pull request. It should do three things well:

1. **Catch side effects.** A change to one feature breaks another one nobody tested. The standard example: a Flexible Grid change makes the Active Trader ladder overlap its order buttons.
2. **Prove the intended change.** The developer stops pasting screenshots, because Sentinel shows the before and after of what they meant to change.
3. **Never cry wolf, never go quiet.** Every screen ends with exactly one result and a reason, and the report says clearly what was not checked and why.

This demo runs the real Sentinel logic. Two things are simulated: the thinkorswim screens are drawn by a test generator, and the AI Reviewer is an offline stand-in that gives fixed answers. Everything else is the system that will run on the Windows test machines.

## How to run it

You need Python 3.11 or newer.

```
cd sentinel
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e '.[test]'
sentinel demo all --out out
```

It takes about a minute or two. Then open `out/index.html` in a browser. It links every scenario's report.

To run one scenario: `sentinel demo flexgrid-ladder --out out`.

## What to look at in each scenario

Each scenario folder holds:
- `summary.txt`: what happened, in plain English. Read this first.
- `pr-comment.md`: the comment a developer would see on their pull request.
- `report.html`: the full report with before and after images at full size.
- `commit-status.json`: whether the pull request check passed.
- `alerts.json`: who would be paged or notified.
- `orchestrator.json`: the run's safety gates and what the input guard blocked.

The scenarios, and what each one should show:

| Scenario | What happens | What you should see |
|---|---|---|
| `flexgrid-ladder` | A Flexible Grid change makes the Active Trader ladder overlap its order buttons at one cell height. This is the case that started the project. | The overlap reported first, on the four 360 px tall steps, with the base build showing no overlap. The other moved cells listed as side effects of the Flexible Grid change. The chart cell the developer declared listed as intended. The check fails. |
| `clean-pr` | A declared change that touches only what it declared. | Nothing unexpected, the declared chart cell under Intended, the check passes. |
| `backend-only` | A backend pricing change with no manifest moves the Buying Power Effect on the Order Confirmation. | The number check (rule R02) fails on that cell and the comment shows both builds' values. |
| `ui-no-manifest` | A UI change the developer did not declare. | A "manifest missing" finding and the moved cell listed as unexpected. |
| `wrong-number` | The Order Confirmation shows a cost that is not quantity times price. Two digits are swapped; nothing else on the dialog changes. | A failed number check (`cost_math`) showing both builds' values. A screen comparison alone would only say the cost cell changed; the number check says the value is wrong. |
| `nightly-canary` | The nightly self-test plants all 33 known defects and takes each main-build screen twice. Night 2 has a wrongly placed mask over a money field, and the number check on that field was removed. | Night 1 all caught. Night 2 says the detector is blind, names the mask, and pages the on-call (`alerts.json`). The next PR comment carries a DETECTOR BLIND banner. The screens taken twice match each night, and the morning report asks for a Calibration run. |
| `decision-inheritance` | A tester confirms some findings as bugs and marks others intended, then the next build runs. | Those findings move to "Suppressed by decision" and the check passes. Nothing gets lost. |
| `paper-mode-mismatch` | The login lands on a live-looking account. | The run stops before capturing anything, every checkpoint says why, and an S1 page goes out. |
| `attempted-send` | A test step tries to press Enter on the Order Confirmation dialog. | The input guard blocks it, the run stops, and the proof reads "PROVEN: no orders sent". |
| `app-crash` | The app freezes in the middle of a size sweep. | The crash recorded on that step, the rest of the sweep marked not run, and the run continuing after a relaunch. |
| `config-invalid` | The test configuration points to a mask record that does not exist. | The run refuses to start and says exactly which rule failed. |
| `expired-mask` | The run happens the day after a mask expires. | The run still goes ahead. The report lists the expired mask, and the area it used to hide is compared again. |
| `operator-stop` | Someone runs `sentinel stop` on a rig in the middle of a run. | The run stops on that rig, the safety sequence runs, and the proof reads "PROVEN: no orders sent". An incident is opened. |
| `redaction-miss` | The new build shows an account number in a place the hiding rules do not cover. | The number is hidden before anything is saved, a finding says where it was, and the check still passes. |
| `popups` | Two windows pop up while the order ticket is filled in: the Message Center, which Sentinel knows, and a window it has never seen. | The Message Center is closed with Escape and the run goes on. The unknown window is closed too, a finding names it and proposes a catalog entry, and its test flow is put on hold. |
| `fingerprint-mismatch` | The old build was drawn by a different graphics setup, so its screens cannot be compared fairly. | Every screen says "not comparable" and names what differed. Run health says the drawing changed inside one Windows build. Nothing is reported as a change. |
| `clamped-gadget` | The Active Trader cell will not get shorter than 400 px, on both builds, and the size test asks for smaller sizes. | The small sizes are marked as clamped, not as failures. The sizes match on both builds. Run health shows the smallest size the cell reached. |
| `fixed-reopens` | A bug is confirmed, the ticket is marked fixed in the next build, and the next build still has the bug. | After the second run the "fixed" decisions are reopened, the ticket is sent back to open, and Run health lists the reopened fixes. |
| `occluded-capture` | Another program's window covers the screen while two screens are captured. | Those two screens say they were covered and name the program. One alert and one finding name the program. The rest of the run is compared. |
| `base-distance` | The PR branched from a main commit that was never built. The closest main build is two commits older. Another PR was merged in between. | Run health says the base is 2 commits before the merge base. The other PR's chart change is listed as intended, not as a side effect. |
| `base-missing` | No main build exists at the PR's merge base or in the 20 commits before it. | The run stops before it touches a rig: base_build_missing. The abort block says the merge base will be built and the run tried once more. Nothing is captured. |
| `base-reuse` | A merge run captures the new main build. Then a PR run uses that build as its base. | The PR run does not capture the base again for its frozen screens: each one names the merge run as its source, and Run health counts them. The results match `clean-pr`. |
| `calibrated-floors` | A weekend calibration run measures the base build's noise and saves it. Then a PR run uses that base. | The PR run reads the saved floors instead of measuring its own. Run health asks for no new calibration. The results match `clean-pr`. |
| `lockstep-slip` | The Position Statement runs on two rigs at once, one per build. The base rig falls more than a minute behind, twice. | It is tried once more at the end, then marked not run (budget). Run health counts one lockstep slip. No finding is written for it. |
| `unregistered-tab` | The new build shows a new tab at the top, "Crypto". The list of known screens does not have it. | The PR comment lists a tier 1 "unregistered surface" for the tab. Its finding names the file a new registry entry would add. The check fails. |

`out/index.html` repeats this list with links. Use the questions below for every scenario.

## Questions to answer for each scenario

1. **Did it find the right thing?** Compare the report with what the scenario summary says was changed.
2. **Is anything wrong or missing?** A result that seems wrong, or a change that should have been reported and wasn't.
3. **Would a developer understand the PR comment in under a minute?** What would they do next?
4. **Is anything reported that should not be?** Noise is the main reason people stop trusting a tool like this.
5. **Is it clear what was not checked?** Look for the coverage section and any "not run" items.

## How to report what you find

For each problem, write down:
- the scenario name,
- the item number or checkpoint name in the report,
- what you expected,
- what you saw.

## Known limits of this demo

- Screens are simulated, so sizes and counts differ from the real app. The summaries say where.
- The AI Reviewer is a stand-in. A live Claude Reviewer exists in the code but is off unless an API key is set on purpose.
- Nothing here touches thinkorswim or any account. The Windows parts (driving the app, capturing the screen, the input guard that blocks order sends) need the Phase 0 test machine.
