"""The Nightly store state check and the restore drill record (8.6, 8.8, 8.9, 9.10 Store health; gap 08-store-health)."""
from __future__ import annotations

import json
from collections import namedtuple

import pytest

from sentinel.orchestrator.alerts import P2
from sentinel.store import Store, StoreRefused
from sentinel.store.health import (INTEGRITY_PREFIX, main, record_restore_drill, store_health,
                                   store_state_check)

GB = 1024 ** 3
NOW = "2026-11-11T11:00:00+00:00"
Disk = namedtuple("Disk", "total used free")
CALM = Disk(1000 * GB, 500 * GB, 500 * GB)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    yield s
    s.close()


def fresh(s, backup="2026-11-11T07:00:00+00:00", drill="2026-10-01T15:00:00+00:00"):
    s.set_system_state("last_backup_at", backup, at=backup)
    s.set_system_state("last_restore_drill_at", drill, at=drill)


def test_everything_fresh_gives_no_row_and_no_alert(store):
    fresh(store)
    check = store_state_check(store, NOW, CALM)
    assert check.rows == () and check.alert is None and check.summary_rows() == []


def test_a_backup_37_hours_old_is_a_red_row_and_the_p2_store_health_alert(store):
    fresh(store, backup="2026-11-09T22:00:00+00:00")
    check = store_state_check(store, NOW, CALM)
    assert [r.label for r in check.rows] == ["Backup age"]
    assert "37 h ago (limit 36 h)" in check.rows[0].text
    assert check.alert == store_health() and check.alert.priority == P2 and check.alert.rule == "store_health"
    assert check.alert.notified == ("on_call_channel", "system_owner") and check.alert.paged == ()
    assert check.summary_rows()[0][1].startswith("red: last backup at 2026-11-09T22:00:00+00:00")
    fresh(store, backup="2026-11-09T23:00:00+00:00")                    # exactly 36 h: not older than 36 h
    assert store_state_check(store, NOW, CALM).rows == ()


def test_no_backup_on_record_is_red(store):
    store.set_system_state("last_restore_drill_at", "2026-10-01T15:00:00+00:00")
    check = store_state_check(store, NOW, CALM)
    assert [(r.label, r.text) for r in check.rows] == [("Backup age", "no backup recorded")]
    assert check.alert is not None


def test_a_current_backup_error_is_red_and_an_error_before_the_last_success_is_not(store):
    fresh(store)
    store.set_system_state("last_backup_error", "OSError: share unreachable", at="2026-11-11T07:30:00+00:00")
    check = store_state_check(store, NOW, CALM)
    assert [(r.label, r.text) for r in check.rows] == [("Backup", "backup job failed: OSError: share unreachable")]
    assert check.alert == store_health()
    store.set_system_state("last_backup_at", "2026-11-11T08:00:00+00:00", at="2026-11-11T08:00:00+00:00")
    assert store_state_check(store, NOW, CALM).rows == ()


def test_an_integrity_failure_has_its_own_row(store):
    fresh(store)
    store.set_system_state("last_backup_error", INTEGRITY_PREFIX + "row 4 missing from index ix_runs_pair",
                           at="2026-11-11T07:30:00+00:00")
    check = store_state_check(store, NOW, CALM)
    assert [r.label for r in check.rows] == ["Integrity check"]
    assert "row 4 missing from index ix_runs_pair; stop the service and restore (8.9)" in check.rows[0].text
    assert check.alert is not None


def test_a_restore_drill_101_days_old_is_red_and_100_days_is_not(store):
    fresh(store, drill="2026-08-02T11:00:00+00:00")                      # 101 days before NOW
    check = store_state_check(store, NOW, CALM)
    assert [r.label for r in check.rows] == ["Restore drill"]
    assert "101 days ago (limit 100 days)" in check.rows[0].text and check.alert is not None
    fresh(store, drill="2026-08-03T11:00:00+00:00")
    assert store_state_check(store, NOW, CALM).rows == ()


def test_no_restore_drill_on_record_is_red(store):
    store.set_system_state("last_backup_at", "2026-11-11T07:00:00+00:00", at="2026-11-11T07:00:00+00:00")
    assert [(r.label, r.text) for r in store_state_check(store, NOW, CALM).rows] == [
        ("Restore drill", "no restore drill recorded")]


def test_the_volume_at_71_percent_raises_the_disk_alarm(store):
    fresh(store)
    check = store_state_check(store, NOW, Disk(1000 * GB, 710 * GB, 290 * GB))
    assert [(r.label, r.text) for r in check.rows] == [("Disk", "store volume 71% used (alarm at 70%)")]
    assert check.alert == store_health()
    assert store_state_check(store, NOW, Disk(1000 * GB, 690 * GB, 310 * GB)).rows == ()


def test_free_space_below_the_preflight_threshold_is_red(store):
    fresh(store)
    check = store_state_check(store, NOW, Disk(2000 * GB, 1250 * GB, 90 * GB))
    assert [(r.label, r.text) for r in check.rows] == [
        ("Disk free", "90 GB free, below the preflight threshold 100 GB")]
    # twice the projected run size raises the threshold (8.6)
    check = store_state_check(store, NOW, Disk(2000 * GB, 1000 * GB, 150 * GB), projected_run_bytes=80 * GB)
    assert [(r.label, r.text) for r in check.rows] == [
        ("Disk free", "150 GB free, below the preflight threshold 160 GB")]


def test_a_passing_drill_is_recorded_with_who_ran_it(store, tmp_path):
    when = record_restore_drill(store, "olivia.owner", compared=10, matched=10, at="2026-11-10T16:00:00-05:00")
    assert when == "2026-11-10T21:00:00+00:00"
    assert store.get_system_state("last_restore_drill_at") == when
    last = json.loads((tmp_path / "results.jsonl").read_text().splitlines()[-1])
    assert last["batch"] == "restore_drill"
    assert {"op": "restore_drill", "by": "olivia.owner", "compared": 10, "matched": 10, "at": when} in last["rows"]


@pytest.mark.parametrize("by, compared, matched, why", [
    ("system", 10, 10, "a person"), ("olivia.owner", 9, 9, "at least 10"), ("olivia.owner", 10, 9, "1 of 10")])
def test_a_drill_by_a_service_with_too_few_checkpoints_or_a_mismatch_is_refused(store, by, compared, matched, why):
    with pytest.raises(StoreRefused, match=why):
        record_restore_drill(store, by, compared=compared, matched=matched)
    assert store.get_system_state("last_restore_drill_at") is None


def test_the_restore_drill_command(tmp_path, capsys):
    db = tmp_path / "central.db"
    Store(db).close()
    assert main(["--db", str(db), "--by", "olivia.owner", "--compared", "10", "--matched", "10",
                 "--at", "2026-11-10T21:00:00+00:00"]) == 0
    assert "restore drill recorded at 2026-11-10T21:00:00+00:00 by olivia.owner" in capsys.readouterr().out
    assert main(["--db", str(db), "--by", "olivia.owner", "--compared", "10", "--matched", "8"]) == 1
    assert "refused" in capsys.readouterr().err
    assert main(["--db", str(tmp_path / "none.db"), "--by", "x", "--compared", "10", "--matched", "10"]) == 2
    with Store(db) as s:
        assert s.get_system_state("last_restore_drill_at") == "2026-11-10T21:00:00+00:00"


def test_the_restore_drill_command_while_the_service_holds_the_writer(tmp_path, capsys):
    """w4-store-maint:4: with the Reporter service's writer holding writer.lock (8.2) the command writes nothing
    and exits 4, no traceback; the service records the drill through its writer."""
    db = tmp_path / "central.db"
    with Store(db) as service:
        assert main(["--db", str(db), "--by", "olivia.owner", "--compared", "10", "--matched", "10"]) == 4
        assert "nothing was written" in capsys.readouterr().err
        record_restore_drill(service, "olivia.owner", compared=10, matched=10, at="2026-11-10T21:00:00+00:00")
        assert service.get_system_state("last_restore_drill_at") == "2026-11-10T21:00:00+00:00"
