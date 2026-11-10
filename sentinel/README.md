# Sentinel core

The platform-independent core of the Sentinel thinkorswim UI regression system. The spec lives in `../sentinel-spec/SPEC.md`; section numbers in the code (for example "3.11") point there.

This folder holds everything that does not need Windows or thinkorswim. The Windows parts, the input gateway, capture and the executor, come after the Phase 0 rig exists.

## Setup

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest                                        # about 650 tests, under a minute
sentinel lint config/example --today 2026-11-10
sentinel taxonomy --check
```

## Modules

| Module | Spec | What it does |
|---|---|---|
| `taxonomy.py`, `config/taxonomy.yaml` | 1.10, 4.1, 4.13 | Every closed vocabulary, generated from the spec; refuses anything outside it |
| `records.py` | 4.13, 4.14 | Outcome, step, run, finding and decision records, validated on construction |
| `lint/` | 4 | JSON Schemas generated from the spec's notation and the `L-*` lint rules; `sentinel lint` |
| `scope.py`, `coverage.py` | 2.10 to 2.12 | Static impact, PR/merge/RC scope, the coverage matrix |
| `diff/` | 3.2 to 3.10, 4.8 | Three-granularity diff, A/A calibration, settle rule, mask budgets, pHash |
| `invariants.py` | 2.7 | The fifteen layout invariants, baseline-free |
| `classify.py` | 3.11, 1.10 | The classification decision table as data, with precedence |
| `canary.py` | 10.3, 12.5 | Mutant generation and detector scoring |
| `store/` | 8 | Single-writer SQLite store and content-addressed artifacts |
| `report/` | 9.1 to 9.7 | The PR comment, commit status and a static HTML run report |
| `synth.py` | | Synthetic thinkorswim-like screens for tests, including the 1.11 ladder overlap |
| `config/example/` | 4.1 | A complete example `sentinel-config` that lints clean |

## Rules for changing it

- Never edit `config/taxonomy.yaml` by hand. Change the spec, then run `python tools/gen_taxonomy.py`.
- Every new outcome, reason or finding kind starts as a spec change (4.13).
