"""The `sentinel` command routes: `stop` (6.6), `reenable` (6.6, 6.12) and `lint --code` (5.4.1, 5.4.9, 6.2)."""
from __future__ import annotations

from pathlib import Path

from sentinel.cli import main
from sentinel.orchestrator.killflag import FileFlagChannel, rig_flag_path
from sentinel.store import Store
from sentinel.store import safety as S


def test_the_help_names_every_route(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for usage in ("sentinel stop <rig> [--reason TEXT] [--by NAME] [--root DIR]", "sentinel reenable --db PATH",
                  "sentinel lint --code [ROOT]"):
        assert usage in out


def test_sentinel_stop_writes_the_simulated_rigs_kill_flag(tmp_path, capsys):
    assert main(["stop", "rig-01", "--root", str(tmp_path), "--reason", "smoke", "--by", "asmith"]) == 0
    req = FileFlagChannel(rig_flag_path(tmp_path, "rig-01")).poll()
    assert (req.rig_id, req.channel, req.requested_by, req.reason) == ("rig-01", "cli", "asmith", "smoke")
    assert "kill switch requested on rig-01" in capsys.readouterr().out
    assert main(["stop", "rig-02"]) == 2 and "--root" in capsys.readouterr().err   # no transport to a real rig here


def test_sentinel_reenable_closes_an_incident_and_refuses_a_missing_database(tmp_path, capsys):
    db = tmp_path / "central.db"
    with Store(db) as st:
        st.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
        iid = S.open_incident(st, run_id=None, rig_id="rig-01", login_alias="login-01", severity="S2",
                              trigger="kill_switch: file_flag")
    assert main(["reenable", "--db", str(db), "incident", str(iid), "--by", "jdoe", "--reason", "verified"]) == 0
    with Store(db) as st:
        assert S.incident(st, iid).closed_by == "jdoe"
    assert main(["reenable", "--db", str(tmp_path / "none.db"), "incident", "1", "--by", "jdoe",
                 "--reason", "x"]) == 2
    assert "no database" in capsys.readouterr().err


def _tree(root: Path, rel: str, source: str) -> Path:
    path = root / "src" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return root


def test_sentinel_lint_code_routes_to_the_gateway_lint(tmp_path, capsys):
    clean = _tree(tmp_path / "clean", "sentinel/flows.py", "import json\n")
    assert main(["lint", "--code", str(clean)]) == 0
    dirty = _tree(tmp_path / "dirty", "sentinel/flows.py", "import pyautogui\n")
    assert main(["lint", "--code", str(dirty)]) == 1
    assert "pyautogui" in capsys.readouterr().out
    assert main(["lint", "--code", str(tmp_path / "nothing")]) == 2       # no src/ folder: a usage error
    assert main(["lint", str(tmp_path / "nothing")]) == 2                 # without --code it is the config lint
    assert "is not a directory" in capsys.readouterr().err
