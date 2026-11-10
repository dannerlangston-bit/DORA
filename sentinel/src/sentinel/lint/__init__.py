"""`sentinel lint`: the input schemas of section 4 and the lint rules of 4.2 to 4.12 and 4.15.

`lint(root, phase, today)` returns a `LintReport` of `Violation`s; `schemas.schema(name)` returns a
generated JSON Schema; `cli.main(argv)` is the command.
"""
from .rules import (
    RULES, LintReport, Rule, Violation, credential_hits, epoch_id, fingerprint_id, lint, lint_fingerprint,
    lint_fingerprint_pair, lint_fingerprint_rig_spec, lint_repo,
)
from .repo import Repo

__all__ = [
    "RULES", "LintReport", "Repo", "Rule", "Violation", "credential_hits", "epoch_id", "fingerprint_id", "lint",
    "lint_fingerprint", "lint_fingerprint_pair", "lint_fingerprint_rig_spec", "lint_repo",
]
