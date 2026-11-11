"""The credential rotation calendar and the run hold (sentinel.rotation; 7.7, 6.9, 4.16, SD-74)."""
from __future__ import annotations

import copy
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from sentinel.rotation import (RotationCalendar, RotationCalendarError, calendar_problems, in_rotation_window,
                               pending_validation)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
NY = ZoneInfo("America/New_York")
LOGIN = "vault://sentinel/rigs/rig-01/tos-login"
OTHER = "vault://sentinel/rigs/rig-02/tos-login"


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=NY)


def example() -> dict:
    return yaml.safe_load((EXAMPLE / "ops" / "credential_rotation.yaml").read_text())


def two_logins() -> dict:
    doc = example()
    doc["logins"].append({"login_ref": OTHER, "owner": "bkhan", "backup_owner": "asmith", "sms_device": "team-phone-02"})
    doc["rotations"].append({"login_ref": LOGIN, "kind": "password", "owner": "asmith",
                             "window": {"start": "2026-12-01T09:00:00-05:00", "end": "2026-12-01T11:00:00-05:00"}})
    return doc


def test_a_run_inside_its_logins_rotation_window_is_held_and_another_logins_run_starts():
    cal = RotationCalendar.from_doc(two_logins())
    hold = in_rotation_window(at(2026, 12, 1, 10), cal, LOGIN)
    assert hold is not None and hold.until == at(2026, 12, 1, 11)
    assert hold.health_line() == (f"run held at Tue 2026-12-01 10:00 EST: password rotation of {LOGIN} by asmith in "
                                  "window Tue 2026-12-01 09:00 EST to Tue 2026-12-01 11:00 EST; not started until the "
                                  "window ends (7.7)")
    assert in_rotation_window(at(2026, 12, 1, 10), cal, OTHER) is None


def test_the_window_is_half_open():
    cal = RotationCalendar.from_doc(two_logins())
    assert in_rotation_window(at(2026, 12, 1, 9), cal, LOGIN) is not None
    assert in_rotation_window(at(2026, 12, 1, 11), cal, LOGIN) is None
    with pytest.raises(ValueError, match="naive"):
        in_rotation_window(datetime(2026, 12, 1, 10), cal, LOGIN)


def test_a_rotation_that_ended_without_a_validating_run_needs_the_login_only_canary():
    cal = RotationCalendar.from_doc(example())
    assert pending_validation(cal, at(2030, 1, 19, 22, 30)) == ()
    (r,) = pending_validation(cal, at(2030, 1, 19, 23))
    assert (r.kind, r.validating_run_id) == ("vault_token", None)


def test_the_example_is_clean_for_its_rig():
    assert calendar_problems(example(), rig_logins=[LOGIN], branch_b=True, today=date(2026, 11, 10)) == []


def problems(edit, **kw) -> list[tuple[str, str]]:
    doc = copy.deepcopy(example())
    edit(doc)
    return calendar_problems(doc, **{"rig_logins": [LOGIN], "branch_b": True, "today": date(2026, 11, 10), **kw})


@pytest.mark.parametrize("edit,kw,path,text", [
    (lambda d: None, {"rig_logins": [LOGIN, OTHER]}, "logins", f"{OTHER} is used by a rig"),
    (lambda d: d["logins"][0].update(backup_owner="asmith"), {}, "logins[0].backup_owner", "the backup owner is the owner"),
    (lambda d: d["logins"][0].update(owner=""), {}, "logins[0]", "a named owner and a backup owner"),
    (lambda d: d["logins"][0].pop("sms_device"), {}, "logins[0].sms_device", "SMS device under DR-03 branch B"),
    (lambda d: d["logins"].append(dict(d["logins"][0])), {}, "logins[1].login_ref", "already has an entry"),
    (lambda d: d["rotations"][0].update(login_ref=OTHER), {}, "rotations[0].login_ref", "is not a listed login"),
    (lambda d: d["rotations"][0].update(kind="mfa_seed"), {}, "rotations[0].kind", "not one of password, vault_token"),
    (lambda d: d["rotations"][1]["window"].update(end="2030-01-19T21:00:00-05:00"), {}, "rotations[1].window",
     "ends before it starts"),
    (lambda d: d["rotations"][0].pop("validating_run_id"), {}, "rotations[0].validating_run_id",
     "name the login-only canary run"),
    (lambda d: d["rotations"][0]["window"].update(start="2026-10-17T22:00:00"), {}, "rotations[0].window",
     "UTC offset"),
    (lambda d: d["rotations"][0].pop("window"), {}, "rotations[0].window", "is required"),
    (lambda d: d["rotations"][0].pop("owner"), {}, "rotations[0].owner", "names its owner"),
    (lambda d: d["logins"][0].pop("login_ref"), {}, "logins[0].login_ref", "names its login_ref"),
])
def test_calendar_problems(edit, kw, path, text):
    found = problems(edit, **kw)
    assert any(p == path and text in m for p, m in found), found


def test_branch_a_needs_no_sms_device_and_an_open_window_needs_no_run_yet():
    assert problems(lambda d: d["logins"][0].pop("sms_device"), branch_b=False) == []
    assert problems(lambda d: d["rotations"][1].update(validating_run_id=None), today=date(2030, 1, 19)) == []
    assert problems(lambda d: None, today=date(2030, 1, 20)) == [
        ("rotations[1].validating_run_id", "the window ended Sat 2030-01-19 23:00 EST; name the login-only canary run "
                                           "that validated the rotation (6.9)")]


def test_from_doc_refuses_a_broken_calendar():
    doc = example()
    doc["rotations"][0]["login_ref"] = OTHER
    with pytest.raises(RotationCalendarError) as err:
        RotationCalendar.from_doc(doc)
    assert err.value.problems == (("rotations[0].login_ref", f"{OTHER} is not a listed login"),)
    with pytest.raises(RotationCalendarError):
        RotationCalendar.from_doc(["not", "a", "mapping"])
