"""The CLI commands of the control host (8.1, 8.6, 8.9, 9.10, 4.1): `sentinel store rebuild|backup|audit|
restore-drill`, `sentinel schemas --migrate`, `sentinel serve` and `sentinel digest`, each with its help output and
one real invocation on a temporary store; and `sentinel serve`'s composition over WSGI in process."""
from __future__ import annotations

import io
import shutil
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest

from sentinel import cli
from sentinel.records import CheckpointOutcome, Finding, RunOutcome
from sentinel.service.serve import compose
from sentinel.service.shipper import WsgiTransport
from sentinel.store.store import RunPlan, ScopeEntry, Store

EXAMPLE = cli.ROOT / "config" / "example"
WS = "ws.tier-a.active-trader"
CENSUS = f"census/trade.active-trader.ladder/{WS}"


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def seeded(path, run_id="run-1", started="2026-11-09T15:00:00+00:00"):
    """A store with one finished merge run and one open tier-2 finding."""
    with Store(path) as s:
        s.begin_run(RunPlan(run_id=run_id, run_type="merge", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=7200, scope=(ScopeEntry(CENSUS, WS, "active_trader", 2, "frozen"),),
                            started_at=started))
        s.write_checkpoint(CheckpointOutcome(run_id=run_id, checkpoint_id=CENSUS, outcome="collateral", workspace_id=WS,
                                             persona="active_trader", data_mode="frozen", tier=2),
                           findings=[Finding("F-1", run_id, "collateral", "ladder moved", run_id, checkpoint_id=CENSUS,
                                             tier=2, region_id="rgn.x", phash="0123456789abcdef",
                                             category="collateral", created_at=started)])
        s.end_run(RunOutcome(run_id, "merge", "complete", "reported", started_at=started,
                             ended_at="2026-11-09T16:00:00+00:00"))
    return path


@pytest.mark.parametrize("argv", [["--help"], ["store", "--help"], ["store", "backup", "--help"],
                                  ["store", "audit", "--help"], ["store", "restore-drill", "--help"],
                                  ["serve", "--help"], ["digest", "--help"], ["schemas", "--migrate", "--help"]])
def test_every_new_command_prints_its_help(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as e:                                     # argparse's --help
        code = e.code
    assert code == 0 and capsys.readouterr().out.strip()


def test_the_usage_lists_every_command():
    _, text = run_cli("--help")
    for cmd in ("lint", "schemas", "taxonomy", "demo", "run", "review", "stop", "reenable", "calibrate", "depmap",
                "schemas --migrate", "store rebuild", "store backup", "store audit", "store restore-drill", "serve",
                "digest"):
        assert f"sentinel {cmd}" in text, cmd


def test_store_rebuild_replays_the_ledger_into_a_new_database(tmp_path):
    seeded(tmp_path / "a" / "sentinel.db")
    code, out = run_cli("store", "rebuild", str(tmp_path / "b" / "sentinel.db"), "--ledger",
                        str(tmp_path / "a" / "results.jsonl"))
    assert code == 0 and "applied" in out
    with Store(tmp_path / "b" / "sentinel.db") as s:
        assert s.get_finding("F-1") is not None


def test_store_backup_audit_and_restore_drill_run_and_pass_their_exit_codes(tmp_path, monkeypatch):
    db = str(seeded(tmp_path / "db" / "sentinel.db"))
    (tmp_path / "artifacts").mkdir()
    base = ["--db", db, "--artifact-root", str(tmp_path / "artifacts"), "--backup-root", str(tmp_path / "backup")]
    assert run_cli("store", "backup", *base, "--now", "2026-11-10T07:00:00+00:00")[0] == 0
    assert (tmp_path / "backup" / "snapshots").is_dir()
    assert run_cli("store", "audit", *base, "--now", "2026-11-15T08:00:00+00:00")[0] == 0
    assert run_cli("store", "restore-drill", "--db", db, "--by", "asmith", "--compared", "12", "--matched", "12")[0] == 0
    assert run_cli("store", "restore-drill", "--db", db, "--by", "asmith", "--compared", "3", "--matched", "3")[0] == 1
    from sentinel.store import backup
    monkeypatch.setattr(backup, "main", lambda argv: 4)         # the store would not open for writing (8.2)
    assert run_cli("store", "backup", *base)[0] == 4


def test_schemas_migrate_lists_nothing_to_do_on_the_example(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "cfg")
    assert run_cli("schemas", "--migrate", str(tmp_path / "cfg"), "--dry-run")[0] == 0
    assert run_cli("schemas", "--migrate", str(tmp_path / "missing"))[0] == 2


def test_digest_queues_the_daily_digest_on_a_business_day_and_nothing_on_a_saturday(tmp_path):
    db = str(seeded(tmp_path / "sentinel.db"))
    args = ["digest", "--db", db, "--config", str(EXAMPLE), "--artifact-root", str(tmp_path / "artifacts")]
    code, out = run_cli(*args, "--now", "2026-11-10T13:00:00+00:00")
    assert code == 0 and "needs_human daily digest" in out and "Tier 2: 1 open" in out
    assert "queued 2 outbox job(s): digest to #sentinel-triage, digest to email" in out
    code, out = run_cli(*args, "--now", "2026-11-14T13:00:00+00:00")
    assert code == 0 and out.startswith("no daily digest on 2026-11-14")


def test_serve_once_binds_both_apps_and_ticks(tmp_path):
    db = str(seeded(tmp_path / "sentinel.db"))
    code, out = run_cli("serve", "--db", db, "--artifact-root", str(tmp_path / "artifacts"), "--config", str(EXAMPLE),
                        "--port", "0", "--ui-port", "0", "--once", "--integrations", "simulated")
    assert code == 0
    assert "Reporter service on http://127.0.0.1:" in out and "tick: outbox" in out


def test_the_composed_service_and_ui_answer_in_process(tmp_path):
    db = seeded(tmp_path / "sentinel.db")
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    c = compose(db, tmp_path / "artifacts", EXAMPLE, integrations="simulated", clock=lambda: now, background=False)
    try:
        health = WsgiTransport(c.service).request("GET", "http://x/v1/health")
        assert health.status == 200 and health.json()["ok"] is True
        page = WsgiTransport(c.ui).request("GET", "http://x/runs", headers={"X-Forwarded-User": "asmith"})
        assert page.status == 200 and b"run-1" in page.body
        rep = c.tick(now)
        assert "outbox" in rep.ran
    finally:
        c.close()
