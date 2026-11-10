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

It takes about two minutes. Then open `out/index.html` in a browser. It links every scenario's report.

To run one scenario: `sentinel demo flexgrid-ladder --out out`.

## What to look at in each scenario

Each scenario folder holds:
- `summary.txt`: what happened, in plain English. Read this first.
- `pr-comment.md`: the comment a developer would see on their pull request.
- `report.html`: the full report with before and after images at full size.
- `commit-status.json`: whether the pull request check passed.
- `alerts.json`: who would be paged or notified.

The scenarios are listed in `out/index.html` with one line each on what to check. Use the questions below for every one.

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
