"""Load a `sentinel-config` checkout into memory (4.1 repository layout).

`Repo.load(root)` reads every file the lint knows by its 4.1 path into a `Doc`; nothing is
validated here. YAML is read with `ConfigLoader`, which keeps timestamps and dates as strings (so
the schemas of 4.1 can check their RFC 3339 form) and reads only `true` and `false` as booleans
(YAML 1.2), so the manifest's `state: on` stays the string `on` that its enum names.

Choices where 4.1 names a directory but not a file name (reported):

- a workspace corpus entry is any `corpus/workspaces/<id>/*.yaml` next to its workspace file;
- `fixtures/margin_rules.yaml` is not a fixture (4.1 lists it separately); it is read as `margin_rules`;
- `flows/RENAMES.yaml` is the rename map, every other `flows/**/*.yaml` is a flow file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

_BOOL = "tag:yaml.org,2002:bool"
_TIMESTAMP = "tag:yaml.org,2002:timestamp"


class ConfigLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans and no timestamp conversion."""


ConfigLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in (_BOOL, _TIMESTAMP)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ConfigLoader.add_implicit_resolver(_BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def load_yaml_text(text: str) -> Any:
    """Parse one YAML document with `ConfigLoader`."""
    return yaml.load(text, Loader=ConfigLoader)  # noqa: S506 - ConfigLoader is a SafeLoader


@dataclass
class Doc:
    """One file of the repository: its repository-relative path, parsed data or parse error."""
    rel: str
    kind: str
    data: Any = None
    error: str | None = None


# kind -> glob patterns relative to the root (4.1 table); the first match of a path wins
LAYOUT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("renames", ("flows/RENAMES.yaml",)),
    ("flow", ("flows/**/*.yaml",)),
    ("build", ("builds/*.yaml",)),
    ("manifest", ("manifests/*.yaml",)),
    ("dependency_map", ("registry/dependency-map.yaml",)),
    ("ladders", ("registry/ladders.yaml",)),
    ("surface", ("registry/surfaces/**/*.yaml",)),
    ("workspace", ("corpus/workspaces/*/*.yaml",)),
    ("regions", ("regions/**/*.yaml",)),
    ("mask", ("masks/**/*.yaml",)),
    ("margin_rules", ("fixtures/margin_rules.yaml",)),
    ("fixture", ("fixtures/*.yaml",)),
    ("profile", ("profiles/*.yaml",)),
    ("allowlist", ("gateway/allowlist.yaml",)),
    ("popups", ("popups/catalog.yaml",)),
    ("mutants", ("canaries/mutants.yaml",)),
    ("configs_allowed", ("configs/allowed.yaml",)),
    ("send_policy", ("policy/send_policy.yaml",)),
    ("explorer_budget", ("policy/explorer_budget.yaml",)),
    ("branches", ("phase0/branches.yaml",)),
    ("quarantine", ("quarantine.yaml",)),
    ("redaction", ("redaction/anchors.yaml",)),
    ("taxonomy", ("taxonomy.yaml",)),
)
NOT_FIXTURES = frozenset({"fixtures/margin_rules.yaml"})
TEXT_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".md", ".txt", ".csv", ".jsonl"})


@dataclass
class Repo:
    """A sentinel-config tree held in memory; `docs` is keyed by repository-relative path."""
    root: Path
    docs: dict[str, Doc] = field(default_factory=dict)

    @classmethod
    def load(cls, root: str | Path) -> "Repo":
        """Read every file of the 4.1 layout under `root`."""
        repo = cls(Path(root))
        seen: set[str] = set()
        for kind, patterns in LAYOUT:
            for pattern in patterns:
                for path in sorted(repo.root.glob(pattern)):
                    rel = path.relative_to(repo.root).as_posix()
                    if rel in seen or not path.is_file() or (kind == "fixture" and rel in NOT_FIXTURES):
                        continue
                    seen.add(rel)
                    repo.docs[rel] = _read(path, rel, kind)
        return repo

    def of(self, kind: str) -> list[Doc]:
        """Every document of one kind, in path order."""
        return [d for d in self.docs.values() if d.kind == kind]

    def one(self, kind: str) -> Doc | None:
        """The single document of a kind that 4.1 places at one path, or None."""
        docs = self.of(kind)
        return docs[0] if docs else None

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        """True when a repository-relative path names an existing file."""
        return bool(rel) and not rel.startswith("/") and ".." not in Path(rel).parts and self.path(rel).is_file()

    def text_files(self) -> Iterator[tuple[str, str]]:
        """Every text file under the root (for the credential scan of L-XREF-07)."""
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or any(p.startswith(".") for p in path.relative_to(self.root).parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or path.parent.parent.name == "workspaces":
                try:
                    yield path.relative_to(self.root).as_posix(), path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue


def _read(path: Path, rel: str, kind: str) -> Doc:
    try:
        return Doc(rel, kind, load_yaml_text(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return Doc(rel, kind, None, f"cannot read: {exc}".splitlines()[0])
