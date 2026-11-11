"""Config-file schema migrations (4.1 Common conventions): "A schema change that is not backward compatible bumps
the version and ships a migration under `schemas/migrations/` in the `sentinel` repository; lint refuses a file
whose version the current harness does not know."

- `known_version(schema)`: the `schema_version` the harness knows, read from the generated JSON Schema (its `const`).
- `MIGRATIONS`: the registry, one `Migration` per (schema, from version) step; `None` is a file written before it
  carried a `schema_version` (the bare-list catalogs of SD-47).
- `migration_path(schema, version)`: the steps from `version` to the known version, or None when none ships.
- `migrate_data(schema, data)`: the data after every step, with the steps applied.
- `hint(schema, version)`: the sentence the lint appends when it refuses a version (`sentinel.lint.rules`).
- `migrate_dir(root, write=True)`: every file of the 4.1 layout under `root` that a shipped migration brings to the
  known version, rewritten in place unless `write=False`.
- `main(argv)`: `sentinel schemas --migrate DIR [--dry-run]` (wired by `sentinel.cli`); also
  `python -m sentinel.config_migrations DIR`.

Choices (reported):

1. The migrations live in this module, the harness code the run pins (8.5), not as files under the generated
   `schemas/` directory, which `sentinel schemas --check` regenerates from the notation (L-XREF-09).
2. Shipped steps: the popup and mutant catalogs from the bare list to `{schema_version: 1, entries}` (SD-47, 4.10,
   4.11). The flow header is at version 2 in 4.7, but the spec writes no version-1 flow, so no flow step ships: a
   flow at version 1 is refused with "no migration ships" and must be rewritten by hand.
3. A rewritten file is dumped as plain YAML in key order: its comments are not kept, so `--dry-run` lists the files
   first and the pull request shows the rewrite.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from .lint import schemas
from .lint.repo import Repo

MIGRATE_COMMAND = "sentinel schemas --migrate"


@dataclass(frozen=True)
class Migration:
    """One step: `schema` files at `from_version` become `to_version` by `apply` (a pure function of the data)."""
    schema: str
    from_version: int | None
    to_version: int
    summary: str
    apply: Callable[[Any], Any]


def _wrap_entries(data: Any) -> Any:
    """SD-47: a bare catalog list becomes `{schema_version: 1, entries: [...]}`."""
    return {"schema_version": 1, "entries": list(data)}


MIGRATIONS: dict[tuple[str, int | None], Migration] = {
    (m.schema, m.from_version): m for m in (
        Migration("popup", None, 1, "wrap the bare popup list as {schema_version: 1, entries} (SD-47, 4.10)",
                  _wrap_entries),
        Migration("mutant", None, 1, "wrap the bare mutant list as {schema_version: 1, entries} (SD-47, 4.11)",
                  _wrap_entries),
    )
}
# 4.1 layout kinds -> the schema of the whole file (catalog files are validated per entry by the lint)
_FILE_SCHEMA = {"popups": "popup", "mutants": "mutant"}


def schema_of_kind(kind: str) -> str | None:
    """The schema name of a 4.1 layout kind (`flow` -> `flow`, `popups` -> `popup`), or None for a kind with none."""
    from .lint.rules import _SCHEMA_OF            # the lint's own kind table, imported late (rules imports this module)
    return _FILE_SCHEMA.get(kind) or (_SCHEMA_OF[kind][0] if kind in _SCHEMA_OF else None)


def known_version(schema: str) -> int | None:
    """The `schema_version` this harness knows for `schema` (the generated schema's `const`)."""
    prop = schemas.schema(schema).get("properties", {}).get("schema_version", {})
    return prop.get("const") if isinstance(prop.get("const"), int) else None


def version_of(data: Any) -> int | None:
    """A file's `schema_version`, or None for a file without one (a bare list, or a map lacking it)."""
    v = data.get("schema_version") if isinstance(data, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def migration_path(schema: str, version: int | None) -> list[Migration] | None:
    """The steps from `version` to the known version ([] when already there), or None when no migration ships."""
    want, steps, seen = known_version(schema), [], set()
    while version != want:
        step = MIGRATIONS.get((schema, version))
        if step is None or version in seen:
            return None
        seen.add(version)
        steps.append(step)
        version = step.to_version
    return steps


def migrate_data(schema: str, data: Any) -> tuple[Any, list[Migration]]:
    """`data` brought to the known version and the steps applied; `ValueError` when no migration ships."""
    steps = migration_path(schema, version_of(data))
    if steps is None:
        raise ValueError(f"no migration ships for {schema} schema_version {version_of(data)!r} "
                         f"(this harness knows {known_version(schema)})")
    for step in steps:
        data = step.apply(data)
    return data, steps


def hint(schema: str, version: int | None) -> str:
    """What the lint says about a refused version: the migration command, or that none ships."""
    shown = "no schema_version" if version is None else f"schema_version {version}"
    steps = migration_path(schema, version)
    if steps:
        return f"run `{MIGRATE_COMMAND} <config dir>` to migrate it ({'; '.join(s.summary for s in steps)})"
    return f"no migration from {shown} ships with this harness; rewrite the file at version {known_version(schema)}"


@dataclass(frozen=True)
class MigrationResult:
    """One file the migration touched (or would touch, on a dry run)."""
    rel: str
    schema: str
    from_version: int | None
    to_version: int
    steps: tuple[str, ...]


def migrate_dir(root: str | Path, write: bool = True) -> tuple[list[MigrationResult], list[str]]:
    """Migrate every file of the 4.1 layout under `root` whose version a shipped migration brings to the known one;
    returns (migrated files, files at an unknown version with no migration)."""
    repo = Repo.load(root)
    done, stuck = [], []
    for rel, doc in sorted(repo.docs.items()):
        schema = schema_of_kind(doc.kind)
        if schema is None or doc.error is not None or doc.data is None:
            continue
        version = version_of(doc.data)
        if version == known_version(schema):
            continue
        steps = migration_path(schema, version)
        if steps is None:
            stuck.append(f"{rel}: {hint(schema, version)}")
            continue
        data, applied = migrate_data(schema, doc.data)
        if write:
            Path(root, rel).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        done.append(MigrationResult(rel, schema, version, applied[-1].to_version, tuple(s.summary for s in applied)))
    return done, stuck


def main(argv: Sequence[str] | None = None) -> int:
    """`sentinel schemas --migrate DIR [--dry-run]`: 0 when every file is at a known version afterwards, 1 when a
    file is at a version no migration ships for, 2 on usage errors."""
    p = argparse.ArgumentParser(prog=MIGRATE_COMMAND)
    p.add_argument("dir", help="the sentinel-config checkout to migrate")
    p.add_argument("--dry-run", action="store_true", help="list the files that would be rewritten; write nothing")
    args = p.parse_args(list(sys.argv[1:] if argv is None else argv))
    root = Path(args.dir)
    if not root.is_dir():
        print(f"{args.dir}: not a directory", file=sys.stderr)
        return 2
    done, stuck = migrate_dir(root, write=not args.dry_run)
    verb = "would migrate" if args.dry_run else "migrated"
    for r in done:
        print(f"{verb} {r.rel}: {r.schema} {r.from_version} -> {r.to_version} ({'; '.join(r.steps)})")
    for line in stuck:
        print(f"cannot migrate {line}", file=sys.stderr)
    print(f"{len(done)} file(s) {verb}, {len(stuck)} at an unknown version", file=sys.stderr)
    return 1 if stuck else 0


if __name__ == "__main__":
    raise SystemExit(main())
