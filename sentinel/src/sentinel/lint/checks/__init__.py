"""Lint rule modules added by the packages that own them (4.1 The lint, 4.15).

Every module of this package is discovered by `sentinel.lint.rules.check_modules()` and run by `lint()`
after the rules of rules.py. A module exposes:

    RULE_IDS: tuple[str, ...]            # the RULES rows it reports, for example ("L-XREF-17", "L-XREF-18")
    def check(repo, add, phase, today):  # repo: sentinel.lint.repo.Repo (shapes coerced as for rules.py)
        add(rule_id, file, message, path="", severity=None)

Every id in RULE_IDS must be a row of `rules.RULES` (a module never invents a rule id), and `add` refuses an id
the module does not name. A row whose skip reason is pending stops being reported as skipped once a module names
it. Modules whose name starts with an underscore are not run.
"""
