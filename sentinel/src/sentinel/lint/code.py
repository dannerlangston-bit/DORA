"""`sentinel lint --code`: the structural layer of the gateway's single entry point (5.4.1, 5.4.9, 6.2).

CI fails the build when a harness module outside the gateway's emitter imports an input library or references
`SendInput`, `keybd_event` or `mouse_event`, or when a module the emitter does not serve imports the emitter
(5.4.9: "handlers have no access to the emitter module"). The one exception is the fire drill injector under the
gateway's `drill/` folder, allowlisted by path and by hash (6.5). The runtime layer, the hook that drops every
untokened event, is `sentinel.gateway.segment`.

Usage: `check_tree(root)` returns the violations under `<root>/src`; `main(argv)` is the command
(`sentinel lint --code [ROOT] [--drill-allowlist FILE] [--json]`): one line per violation, 1 when any, 0 when
clean, 2 on a usage error.

Choices where the spec is silent or ambiguous (reported):

1. The spec's `harness/gateway/` is this repository's `src/sentinel/gateway/`. Inside it only the emitter
   (`src/sentinel/gateway/emitter*`, a module or a package) may inject, since 5.4.1 makes `gateway.emit` the only
   caller of `SendInput`; the hook host, the segment and the drainer need no input library. The Windows track's
   `SendInput` adapter therefore lives at `src/sentinel/gateway/emitter*`.
2. Input libraries are the five of 5.4.1 (`pywinauto.mouse`, `pywinauto.keyboard`, `pyautogui`, `pynput`,
   `pydirectinput`) plus the PyPI `keyboard` and `mouse` packages, input libraries in 6.2's sense. A module is
   caught whether it imports one, a submodule of one, a name from one (`from pywinauto import mouse`), reaches
   one as an attribute (`pywinauto.mouse.click`, aliases followed), or imports one by name through
   `importlib.import_module` or `__import__`.
3. "References ... through ctypes": every identifier, attribute, imported name, definition, keyword or exact
   string constant `SendInput`, `keybd_event` or `mouse_event` is a reference, with or without a ctypes import in
   the module, since a module can reach user32 through another module's handle or through win32api. Prose that
   mentions the names (docstrings, comments) is not code and passes.
4. The emitter's importers are the gateway package itself, the executor's emission stage
   (`src/sentinel/executor/emitter.py`, 5.4.3 stage 6) and the rig that composes the ports
   (`src/sentinel/orchestrator/rig.py`), `EMITTER_IMPORTERS`. Every other module fails on importing the emitter,
   the agent and Explorer handlers wherever they live included: an allowlist fails closed for a handler module
   added later.
5. The drill injector is allowlisted by path (`src/sentinel/gateway/drill/`) and by the SHA-256 of the file as
   recorded in a sha256sum-format allowlist (`<sha256>  <path from root>` per line; default
   `src/sentinel/gateway/drill/SHA256SUMS`, or `--drill-allowlist`). A drill file whose hash is not recorded is
   an ordinary module, with a `drill-hash` violation saying why. The code-signing hash of the built executable is
   checked on the rig (6.5, 11 item 4), not in CI.
6. The scan covers the harness source, `<root>/src`: tests and tools never run on the rig. A file that does not
   parse is a violation (`syntax`): the lint cannot vouch for it.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3]          # the sentinel project folder (it holds src/)
INPUT_MODULES = ("pywinauto.mouse", "pywinauto.keyboard", "pyautogui", "pynput", "pydirectinput", "keyboard",
                 "mouse")
INJECTION_NAMES = frozenset("SendInput keybd_event mouse_event".split())   # one string: the list is not a reference
EMITTER_MODULE = "sentinel.gateway.emitter"                 # sentinel.gateway.emitter* is the emitter
EMITTER_PATHS = ("src/sentinel/gateway/emitter*",)
EMITTER_IMPORTERS = ("src/sentinel/gateway/*", "src/sentinel/executor/emitter.py", "src/sentinel/orchestrator/rig.py")
DRILL_DIR = "src/sentinel/gateway/drill/"
DRILL_ALLOWLIST = "src/sentinel/gateway/drill/SHA256SUMS"
KINDS = ("input-import", "injection-reference", "emitter-import", "drill-hash", "syntax")


@dataclass(frozen=True)
class CodeViolation:
    """One finding: the file (relative to the root), the line, the kind (`KINDS`) and what was found."""

    file: str
    line: int
    kind: str
    message: str

    def text(self) -> str:
        return f"{self.file}:{self.line}: {self.kind}: {self.message}"


def module_name(rel: str) -> tuple[str, bool]:
    """`src/sentinel/a/b.py` -> (`sentinel.a.b`, False); a package's `__init__.py` -> (its name, True)."""
    parts = Path(rel).with_suffix("").parts
    parts = parts[1:] if parts[:1] == ("src",) else parts
    if parts[-1:] == ("__init__",):
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def _input_library(name: str) -> str | None:
    return next((m for m in INPUT_MODULES if name == m or name.startswith(m + ".")), None)


def _is_emitter(name: str) -> bool:
    return name.startswith(EMITTER_MODULE)


class _Scan(ast.NodeVisitor):
    """Collects the hits of one module (choices 2 to 4)."""

    def __init__(self, module: str, is_pkg: bool, *, inject_ok: bool, emitter_ok: bool):
        self.module, self.is_pkg = module, is_pkg
        self.inject_ok, self.emitter_ok = inject_ok, emitter_ok
        self.aliases: dict[str, str] = {}
        self.hits: dict[tuple[int, str], str] = {}            # one finding per line and kind

    def _hit(self, node: ast.AST, kind: str, message: str) -> None:
        if self.emitter_ok if kind == "emitter-import" else self.inject_ok:
            return
        self.hits.setdefault((getattr(node, "lineno", 0), kind), message)

    def _target(self, node: ast.AST, name: str, how: str) -> None:
        lib = _input_library(name)
        if lib:
            self._hit(node, "input-import", f"{how} {name!r}: input library {lib!r} outside the gateway emitter")
        if _is_emitter(name):
            self._hit(node, "emitter-import", f"{how} {name!r}: only the gateway, the executor's emission stage and "
                                              "the rig may import the emitter (5.4.9)")

    def _resolve(self, module: str | None, level: int) -> str:
        if level == 0:
            return module or ""
        base = self.module.split(".")
        base = base if self.is_pkg else base[:-1]
        base = base[:len(base) - (level - 1)] if level > 1 else base
        return ".".join(base + ([module] if module else []))

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._target(node, a.name, "import")
            if a.asname:
                self.aliases[a.asname] = a.name
            else:
                self.aliases[a.name.split(".")[0]] = a.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._resolve(node.module, node.level)
        for a in node.names:
            full = f"{base}.{a.name}" if base else a.name
            self._target(node, full if a.name != "*" else base, "from-import")
            if a.name in INJECTION_NAMES:
                self._hit(node, "injection-reference", f"imports {a.name} from {base!r}")
            self.aliases[a.asname or a.name] = full

    def _dotted(self, node: ast.AST) -> str | None:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        return ".".join([self.aliases.get(node.id, node.id)] + parts[::-1])

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in INJECTION_NAMES:
            self._hit(node, "injection-reference", f"attribute .{node.attr}")
        dotted = self._dotted(node)
        if dotted and (dotted in INPUT_MODULES or dotted.rsplit(".", 1)[-1].startswith("emitter")
                       and dotted.rsplit(".", 1)[0] == "sentinel.gateway"):
            self._target(node, dotted, "uses")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in INJECTION_NAMES:
            self._hit(node, "injection-reference", f"name {node.id}")

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in INJECTION_NAMES:
            self._hit(node, "injection-reference", f"string {node.value!r}")

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            self._target(node, node.args[0].value, f"{name}()")
        self.generic_visit(node)

    def _named(self, node: ast.AST, name: str | None, what: str) -> None:
        if name in INJECTION_NAMES:
            self._hit(node, "injection-reference", f"{what} {name}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._named(node, node.name, "defines")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._named(node, node.name, "defines")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        self._named(node, node.arg, "keyword")
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._named(node, node.arg, "argument")


def check_source(source: str, rel: str) -> list[CodeViolation]:
    """The violations of one module, `rel` its path from the root (`src/sentinel/...`); no drill exemption."""
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as e:
        return [CodeViolation(rel, e.lineno or 0, "syntax", f"does not parse: {e.msg}")]
    module, is_pkg = module_name(rel)
    scan = _Scan(module, is_pkg, inject_ok=any(fnmatch(rel, p) for p in EMITTER_PATHS),
                 emitter_ok=any(fnmatch(rel, p) for p in EMITTER_IMPORTERS))
    scan.visit(tree)
    return [CodeViolation(rel, line, kind, msg) for (line, kind), msg in sorted(scan.hits.items())]


def load_drill_allowlist(path: str | Path) -> dict[str, str]:
    """`{path from root: sha256}` from a sha256sum-format file; an absent file allowlists nothing."""
    p = Path(path)
    if not p.is_file():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition(" ")
        out[name.strip().lstrip("*")] = digest.lower()
    return out


def check_tree(root: str | Path = DEFAULT_ROOT, *,
               drill_hashes: Mapping[str, str] | None = None) -> list[CodeViolation]:
    """Every violation under `<root>/src` (choice 6). `drill_hashes` maps drill files to their recorded SHA-256;
    by default they are read from `<root>/src/sentinel/gateway/drill/SHA256SUMS`."""
    root = Path(root)
    hashes = load_drill_allowlist(root / DRILL_ALLOWLIST) if drill_hashes is None else dict(drill_hashes)
    out: list[CodeViolation] = []
    for path in sorted((root / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        found = check_source(data.decode("utf-8", errors="replace"), rel)
        if found and rel.startswith(DRILL_DIR):
            actual = hashlib.sha256(data).hexdigest()
            recorded = hashes.get(rel)
            if recorded == actual:
                continue
            out.append(CodeViolation(rel, 0, "drill-hash", f"under the drill path but sha256 {actual} is "
                                     + (f"not the recorded {recorded}" if recorded else "not allowlisted")))
        out.extend(found)
    return out


def main(argv: list[str] | None = None) -> int:
    """`sentinel lint --code [ROOT] [--drill-allowlist FILE] [--json]`: 1 on any violation, 0 clean, 2 usage."""
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in[:1] == ["lint"]:
        args_in = args_in[1:]
    args_in = [a for a in args_in if a != "--code"]
    p = argparse.ArgumentParser(prog="sentinel lint --code", description="The gateway single-entry-point lint "
                                "(5.4.1, 5.4.9, 6.2).")
    p.add_argument("root", nargs="?", default=str(DEFAULT_ROOT), help="the sentinel project folder (holds src/)")
    p.add_argument("--drill-allowlist", help="sha256sum-format allowlist of drill injector files")
    p.add_argument("--json", action="store_true", help="one JSON object per violation")
    try:
        args = p.parse_args(args_in)
    except SystemExit as exc:
        return int(exc.code or 0)
    root = Path(args.root)
    if not (root / "src").is_dir():
        print(f"sentinel lint --code: {root} has no src/ folder", file=sys.stderr)
        return 2
    hashes = load_drill_allowlist(args.drill_allowlist) if args.drill_allowlist else None
    found = check_tree(root, drill_hashes=hashes)
    for v in found:
        print(json.dumps(asdict(v)) if args.json else v.text())
    print(f"{len(found)} code lint violations", file=sys.stderr)
    return 1 if found else 0
