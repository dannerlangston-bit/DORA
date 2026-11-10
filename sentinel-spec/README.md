# Sentinel: thinkorswim UI regression system specification

Sentinel drives the thinkorswim desktop app the way a user does and compares every screen against the build before a change. For every change it reports two things. The intended change comes with before and after pictures, so developers stop hand-screenshotting their work. Anything else that moved or broke is ranked, with the reason it was tested.

## What is here

| Path | What it is |
|---|---|
| `SPEC.md` | The full specification as one document. Start with "Read this first". |
| `sections/` | The source: one file per section (`00-preface.md`, `01-...` to `12-...`). Edit these, not `SPEC.md`. |
| `tools/assemble.py` | Rebuilds `SPEC.md` from `sections/` and lints headings, numbering and dashes. |
| `tools/xcheck.py` | Cross-section consistency check: retired vocabulary and dangling section references. |
| `review/original-spec-gap-review.md` | The 76-gap review of the first draft that this spec answers. Section 12.10 traces every gap. |
| `review/design-decisions-log.md` | Every cross-section decision made while writing the spec (F1 to F76). |

## Working on the spec

```
python3 tools/xcheck.py      # must report TOTAL issues: 0
python3 tools/assemble.py    # rebuilds SPEC.md; must report no heading or dash problems
```

Change a section, run both, and commit the section and the rebuilt `SPEC.md` together.

## Status

Ready for Phase 0. Seven unknowns about the app are settled by the Phase 0 spike, and each already has both outcomes specified (section 11.8).
