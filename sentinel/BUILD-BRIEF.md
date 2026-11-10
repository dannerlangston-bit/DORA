# Build brief for Sentinel core builders

You are one of several builders working in parallel on the Sentinel core in `/home/user/DORA/sentinel/`. The spec is `/home/user/DORA/sentinel-spec/sections/NN-*.md` (section numbers like "3.11" refer to it). Search it with grep and read the subsections you need; do not read whole files.

## Rules

- **Python:** use `/tmp/claude-0/-home-user-DORA/b8b11c0f-d12c-5ac7-ab8e-6badd10f84fe/scratchpad/venv/bin/python` (numpy, opencv-python-headless, scikit-image, pyyaml, jsonschema, pillow, pytest installed; the package is installed editable). Run tests with `... -m pytest -q tests/<your tests>`.
- **Own only your files.** Each builder owns the files named in its task. Never edit another builder's files, `pyproject.toml`, `src/sentinel/cli.py`, `src/sentinel/taxonomy.py`, `src/sentinel/records.py`, `config/taxonomy.yaml` or `tools/`. If you need a change there, say so in your final report.
- **Shared foundation (read-only):** `sentinel.taxonomy` (closed vocabularies, `check_checkpoint`, `is_checkpoint_id`, `checkpoint_parts`) and `sentinel.records` (`CheckpointOutcome`, `StepOutcome`, `RunOutcome`, `Finding`, `Decision`, `Signature`, `DiffSummary`, `RegionDiff`, `Component`, `Classification`). Produce and consume these types; never invent an outcome, reason, gate or finding kind. If the spec needs one the taxonomy lacks, report it.
- **CLI:** expose a `main(argv: list[str] | None = None) -> int` in your module if the task names a command; the coordinator wires `cli.py`.
- **Tests:** every behavior the task lists has a unit test. Tests must be deterministic and run in under 30 seconds total for your module. Use synthetic data; there is no thinkorswim here.
- **Spec fidelity:** implement what the spec says, with its parameters. Where the spec is ambiguous, choose, write the choice in a module docstring with the section number, and report it.
- **Style:** type hints, docstrings that cite spec sections, no dead code, no network access, no new dependencies (if one is essential, report it instead of installing).
- **Do not commit.** The coordinator commits and merges.

## Final report (your final text)

Files created; public API (functions and classes with one line each); tests and their pass count; choices you made where the spec was ambiguous; anything you need from another module or the foundation.
