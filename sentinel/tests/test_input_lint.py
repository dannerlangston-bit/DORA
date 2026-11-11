"""The structural single-entry-point lint of the Input Gateway (5.4.1, 5.4.9, 6.2): `sentinel lint --code`."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sentinel.lint.code import (DEFAULT_ROOT, CodeViolation, check_source, check_tree, load_drill_allowlist, main,
                                module_name)

AGENT = "src/sentinel/agent/tools.py"


def kinds(source: str, rel: str = AGENT) -> list[str]:
    return [v.kind for v in check_source(source, rel)]


def tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_the_real_source_tree_passes():
    assert check_tree(DEFAULT_ROOT) == []


@pytest.mark.parametrize("source", [
    "import pyautogui",
    "import pynput.keyboard as kb",
    "from pynput.mouse import Controller",
    "import pydirectinput",
    "import pywinauto.mouse",
    "from pywinauto import keyboard",
    "from pywinauto.keyboard import send_keys",
    "import pywinauto as pw\npw.mouse.click(coords=(1, 2))",
    "import keyboard",
    "import mouse",
    "import importlib\nimportlib.import_module('pyautogui')",
    "__import__('pynput')",
])
def test_input_libraries_outside_the_emitter_fail(source):
    assert "input-import" in kinds(source)


@pytest.mark.parametrize("source", [
    "import ctypes\nctypes.windll.user32.SendInput(1, None, 28)",
    "from ctypes import windll\nu = windll.user32\nu.keybd_event(13, 0, 0, 0)",
    "import ctypes\nf = getattr(ctypes.WinDLL('user32'), 'mouse_event')",
    "from win32api import keybd_event",
    "import ctypes\nfn = ctypes.WinDLL('user32')['SendInput']",
    "def SendInput(n, events, size):\n    return 0",
])
def test_injection_references_fail(source):
    assert "injection-reference" in kinds(source)


@pytest.mark.parametrize("source", [
    '"""Calls SendInput through the emitter only; keybd_event and mouse_event are never used."""',
    "# SendInput is the gateway's\nx = 1",
    "from . import mouse\nfrom .keyboard import layout",            # this package's own modules
    "import pywinauto\npywinauto.Application",
    "import ctypes\nctypes.windll.user32.SetWindowsHookExW",
    "from sentinel.gateway import segment\nfrom sentinel.executor import emitter",
])
def test_prose_own_modules_and_other_win32_calls_pass(source):
    assert kinds(source) == []


@pytest.mark.parametrize("rel", ["src/sentinel/gateway/emitter.py", "src/sentinel/gateway/emitter_win32.py",
                                 "src/sentinel/gateway/emitter/input.py"])
def test_the_gateway_emitter_may_inject(rel):
    assert kinds("import ctypes\nimport pywinauto.keyboard\nctypes.windll.user32.SendInput(1, None, 28)", rel) == []


def test_the_rest_of_the_gateway_may_not():
    source = "import pyautogui\nimport ctypes\nctypes.windll.user32.SendInput"
    assert set(kinds(source, "src/sentinel/gateway/drainer.py")) == {"input-import", "injection-reference"}


@pytest.mark.parametrize("source", [
    "from sentinel.gateway.emitter import emit",
    "from sentinel.gateway import emitter",
    "import sentinel.gateway.emitter_win32",
    "from ..gateway import emitter",
    "from ..gateway.emitter import emit as e",
    "from .. import gateway\ngateway.emitter.emit()",
    "import sentinel.gateway as g\ng.emitter_win32.Input()",
    "import importlib\nimportlib.import_module('sentinel.gateway.emitter')",
])
def test_handlers_have_no_access_to_the_emitter(source):
    assert "emitter-import" in kinds(source)


@pytest.mark.parametrize("rel", ["src/sentinel/gateway/hookhost_win32.py", "src/sentinel/executor/emitter.py",
                                 "src/sentinel/orchestrator/rig.py"])
def test_the_emitters_own_importers_pass(rel):
    assert kinds("from sentinel.gateway.emitter import emit\nfrom ..gateway import emitter", rel) == []


def test_relative_imports_resolve_from_the_module():
    assert module_name("src/sentinel/agent/tools.py") == ("sentinel.agent.tools", False)
    assert module_name("src/sentinel/agent/__init__.py") == ("sentinel.agent", True)
    assert kinds("from ..gateway import emitter", "src/sentinel/agent/__init__.py") == ["emitter-import"]
    assert kinds("from .gateway import emitter", "src/sentinel/__init__.py") == ["emitter-import"]
    assert kinds("from ...gateway import emitter", "src/sentinel/agent/sub/x.py") == ["emitter-import"]


def test_a_file_that_does_not_parse_fails():
    assert kinds("def broken(:\n") == ["syntax"]


INJECTOR = "import ctypes\nctypes.windll.user32.SendInput(1, None, 28)\n"
DRILL = "src/sentinel/gateway/drill/injector.py"


def test_drill_injector_passes_with_its_recorded_hash_only(tmp_path):
    root = tree(tmp_path, {DRILL: INJECTOR, "src/sentinel/__init__.py": ""})
    digest = hashlib.sha256(INJECTOR.encode()).hexdigest()
    assert check_tree(root, drill_hashes={DRILL: digest}) == []
    wrong = check_tree(root, drill_hashes={DRILL: "0" * 64})
    assert [v.kind for v in wrong] == ["drill-hash", "injection-reference"] and "not the recorded" in wrong[0].message
    unlisted = check_tree(root, drill_hashes={})
    assert [v.kind for v in unlisted] == ["drill-hash", "injection-reference"]
    assert "not allowlisted" in unlisted[0].message
    (root / "src/sentinel/gateway/drill/SHA256SUMS").write_text(f"# recorded at signing\n{digest}  {DRILL}\n")
    assert check_tree(root) == []                                                   # the default allowlist file
    assert load_drill_allowlist(root / "missing") == {}


def test_the_drill_exemption_is_by_path_too(tmp_path):
    rel = "src/sentinel/agent/injector.py"
    root = tree(tmp_path, {rel: INJECTOR})
    digest = hashlib.sha256(INJECTOR.encode()).hexdigest()
    assert [v.kind for v in check_tree(root, drill_hashes={rel: digest})] == ["injection-reference"]


def test_a_planted_input_import_fails_the_command(tmp_path, capsys):
    root = tree(tmp_path, {"src/sentinel/__init__.py": "", "src/sentinel/report/util.py": "import pynput\n",
                           "tests/test_x.py": "import pyautogui\n"})                # tests are not scanned
    assert main(["lint", "--code", str(root)]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["src/sentinel/report/util.py:1: input-import: import 'pynput': input library 'pynput' outside "
                   "the gateway emitter"]
    assert main(["--code", str(root), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"file": "src/sentinel/report/util.py", "line": 1,
                                                   "kind": "input-import", "message": out[0].split(": ", 2)[2]}
    (root / "src/sentinel/report/util.py").write_text("import json\n")
    assert main([str(root)]) == 0
    assert main([str(tmp_path / "nowhere")]) == 2


def test_the_command_takes_a_drill_allowlist_file(tmp_path):
    root = tree(tmp_path, {DRILL: INJECTOR})
    allow = tmp_path / "drill.sha256"
    allow.write_text(f"{hashlib.sha256(INJECTOR.encode()).hexdigest()} *{DRILL}\n")
    assert main([str(root), "--drill-allowlist", str(allow)]) == 0
    assert main([str(root)]) == 1


def test_violation_text():
    assert CodeViolation("a.py", 3, "syntax", "x").text() == "a.py:3: syntax: x"
