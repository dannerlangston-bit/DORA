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
| `wrong-number` | The Order Confirmation shows a cost that is not quantity times price, while every pixel matches. | A failed number check (`cost_math`) with both builds' values, even though the screenshots are identical. |
| `nightly-canary` | The nightly self-test plants known defects. Night 2 has a wrongly placed mask over a money field. | Night 1 all caught. Night 2 says the detector is blind, names the mask, and the next PR comment carries a DETECTOR BLIND banner. |
| `decision-inheritance` | A tester confirms some findings as bugs and marks others intended, then the next build runs. | Those findings move to "Suppressed by decision" and the check passes. Nothing gets lost. |
| `paper-mode-mismatch` | The login lands on a live-looking account. | The run stops before capturing anything, every checkpoint says why, and an S1 page goes out. |
| `attempted-send` | A test step tries to press Enter on the Order Confirmation dialog. | The input guard blocks it, the run stops, and the proof reads "PROVEN: no orders sent". |
| `app-crash` | The app freezes in the middle of a size sweep. | The crash recorded on that step, the rest of the sweep marked not run, and the run continuing after a relaunch. |
| `config-invalid` | The test configuration has an expired entry. | The run refuses to start and says exactly which rule failed. |

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
