# Sentinel core

The platform-independent core of the Sentinel thinkorswim UI regression system. The spec lives in `../sentinel-spec/SPEC.md`; section numbers in the code (for example "3.11") point there.

This folder holds everything that does not need Windows or thinkorswim. The Windows parts, the input gateway, capture and the executor, come after the Phase 0 rig exists.

## Setup

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest
```

## Layout

| Path | What it is |
|---|---|
| `config/taxonomy.yaml` | Every closed vocabulary, generated from the spec by `tools/gen_taxonomy.py`. Never edit by hand. |
| `src/sentinel/taxonomy.py` | Loads the taxonomy and refuses any outcome, reason or id outside it. |
| `tools/` | Generators that read the spec's tables. |
| `tests/` | Unit tests. `test_taxonomy.py` fails if the taxonomy and the spec disagree. |
