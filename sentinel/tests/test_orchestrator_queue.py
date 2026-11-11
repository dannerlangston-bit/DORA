"""The run queue and scheduler (11.2 Queue and saturation, 1.6, 1.7, 7.2, 7.10, 9.10, 10.2, 10.9, 11.6; SD-76).

Every test drives `RunQueue` with a `ManualClock` on America/New_York wall times in November 2026 (EST)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sentinel.orchestrator import alerts
from sentinel.orchestrator import start_deadline as sd
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.queue import (
    Alert, CalibrationRequest, Coalesced, DeadlineReached, MergeBuild, NightlySkipped, Placeholder, PoolReleased,
    PrBuild, RcBuild, Replaced, Requeue, ReservationEnded, RigState, RunFinished, RunQueue, RunQueued,
    RunReachedRunning, RunStart, Scheduled, StateWrite, Supersede, WebhookError, calibration_slot,
    fixed_operating_day, friday_all_quiet, open_time, webhook_to_request,
)
from sentinel.orchestrator.results import Outcome

NY = ZoneInfo("America/New_York")
SIGNOFF = datetime(2026, 12, 1, 17, 0, tzinfo=NY)        # far enough that no RC start deadline interferes


def ny(d: int, h: int, mi: int = 0, s: int = 0, mo: int = 11) -> datetime:
    return datetime(2026, mo, d, h, mi, s, tzinfo=NY).astimezone(timezone.utc)


def of(outs, kind, run_type: str | None = None):
    """The outputs of `kind`, of one run type when given (the Nightly runs every night beside the run a test is
    about)."""
    items = [o for o in outs if isinstance(o, kind)]
    if run_type is None:
        return items
    return [o for o in items if (getattr(o, "request", None) or o).run_type == run_type]


class Harness:
    def __init__(self, start: datetime, **kw):
        self.clock = ManualClock(start)
        self.q = RunQueue(self.clock, **kw)
        self.log: list = []

    def send(self, event):
        out = self.q.handle(event)
        self.log += out
        return out

    def until(self, when: datetime, step: float = 60.0):
        """Tick every `step` seconds up to and including `when`."""
        out = []
        while self.clock.now() < when:
            self.clock.advance(min(step, (when - self.clock.now()).total_seconds()))
            out += self.q.tick()
        self.log += out
        return out

    def started(self, outs) -> RunStart:
        (s,) = of(outs, RunStart)
        return s


def pr(n: int, build: str, at: datetime) -> PrBuild:
    return PrBuild(n, build, at, commit=f"c-{build}", merge_base="main@abc123")


def merge(build: str, prev: str | None, pr_no: int, at: datetime) -> MergeBuild:
    return MergeBuild(build, at, previous_main=prev, merged_pr=pr_no, manifest_ref=f"mf-{pr_no}")


def rc(build: str, at: datetime, train: str = "26.4", signoff: datetime = SIGNOFF) -> RcBuild:
    return RcBuild(build, train, at, signoff_at=signoff)


# -- PR runs: one in flight per PR, the newest push wins (11.2, 1.6, 1.7) ---------------------------------------
def test_three_pushes_during_a_pr_run_only_the_third_runs_and_the_second_is_superseded():
    h = Harness(ny(10, 10, 0))
    first = h.started(h.send(pr(7, "b1", ny(10, 10, 0))))
    assert first.request.candidate == "b1" and first.rigs == (1, 2, 3, 4)
    h.clock.advance(60)
    out2 = h.send(pr(7, "b2", h.clock.now()))
    assert not of(out2, RunStart) and of(out2, RunQueued)[0].position == 1     # a push while running queues
    second = of(out2, RunQueued)[0].run_id
    h.clock.advance(60)
    out3 = h.send(pr(7, "b3", h.clock.now()))
    (rep,) = of(out3, Replaced)
    assert rep.run_id == second and rep.run_type == "pr"                        # run history: superseded
    assert [r.candidate for r in h.q.queued()] == ["b3"]
    h.clock.advance(30 * 60)
    nxt = h.started(h.send(RunFinished(first.request.run_id, "complete")))
    assert nxt.request.candidate == "b3" and nxt.request.run_id == rep.by_run_id


def test_a_second_pr_waits_for_the_pool_but_the_same_pr_never_runs_twice_at_once():
    h = Harness(ny(10, 10, 0))
    a = h.started(h.send(pr(1, "a1", ny(10, 10, 0))))
    h.send(pr(1, "a2", ny(10, 10, 1)))
    h.send(pr(2, "b1", ny(10, 10, 2)))
    assert [r.candidate for r in h.q.queued()] == ["a2", "b1"]                   # age within PR runs
    h.clock.advance(120)
    s = h.started(h.send(RunFinished(a.request.run_id, "complete")))
    assert s.request.candidate == "a2"


# -- order: RC, merge, PR, then age (11.2) -------------------------------------------------------------------------
def test_queue_order_is_rc_then_merge_then_pr_then_age():
    h = Harness(ny(10, 10, 0))
    busy = h.started(h.send(pr(1, "x", ny(10, 10, 0))))
    h.send(pr(2, "p-old", ny(10, 10, 1)))
    h.send(pr(3, "p-new", ny(10, 10, 2)))
    h.send(merge("m1", "m0", 101, ny(10, 10, 3)))
    out = h.send(rc("rc-b1", ny(10, 10, 4)))
    assert [(r.run_type, r.candidate) for r in h.q.queued()] == \
        [("rc", "rc-b1"), ("merge", "m1"), ("pr", "p-old"), ("pr", "p-new")]
    rcq = next(o for o in of(out, RunQueued) if o.run_type == "rc")
    assert rcq.position == 1 and rcq.depth == 4
    h.clock.advance(60)
    s = h.started(h.send(RunFinished(busy.request.run_id, "complete")))
    assert s.request.run_type == "rc" and s.rigs == (1, 2, 3, 4, 5, 6)


def test_run_queued_is_emitted_every_60_seconds_with_the_position():
    h = Harness(ny(10, 10, 0))
    h.send(pr(1, "x", ny(10, 10, 0)))
    first = of(h.send(pr(2, "y", ny(10, 10, 0))), RunQueued)
    assert [(o.position, o.depth) for o in first] == [(1, 1)]
    assert first[0].deadline_at == ny(10, 10, 45)
    assert not of(h.until(ny(10, 10, 0, 59), step=59), RunQueued)
    again = of(h.until(ny(10, 10, 1), step=1), RunQueued)
    assert [(o.run_id, o.position) for o in again] == [(first[0].run_id, 1)]
    assert again[0].covers == ("y",)


# -- the PR start deadline: pool_saturated (11.2, 9.2, 1.7 T+1:00) ----------------------------------------------
def test_pr_run_still_queued_at_45_minutes_is_aborted_budget_pool_saturated():
    h = Harness(ny(10, 10, 0))
    m = h.started(h.send(merge("m1", "m0", 101, ny(10, 10, 0))))           # a 3 h merge run holds the pool
    out = h.send(pr(5, "b", ny(10, 10, 0)))
    pid = of(out, RunQueued)[0].run_id
    assert not of(h.until(ny(10, 10, 44)), DeadlineReached)
    out = h.until(ny(10, 10, 45))
    (dl,) = of(out, DeadlineReached)
    d = dl.decision
    assert dl.request.run_id == pid and d.deadline == ny(10, 10, 45)
    assert d.result.run_status == "aborted_budget" and d.result.run_reason == "pool_saturated"
    assert d.result.unreached == Outcome("not_run", "budget")
    assert d.comment == "Sentinel did not run: pool saturated, 1 run ahead; push again or re-run the build job"
    assert d.check == ("error", "Sentinel did not run") and d.abort == {"runs_ahead": 1, "queue_depth": 1}
    assert h.q.queued() == () and m.request.run_id not in [r.run_id for r in h.q.queued()]


def test_pool_saturated_p2_at_most_once_per_hour():
    h = Harness(ny(10, 10, 0))
    h.send(merge("m1", "m0", 101, ny(10, 10, 0)))
    h.send(pr(5, "b", ny(10, 10, 0)))
    h.until(ny(10, 10, 50))
    h.send(pr(6, "c", h.clock.now()))
    h.until(ny(10, 12, 0))
    sat = [o for o in of(h.log, Alert) if o.alert == "pool saturated"]
    # the 20-minute wait at 10:21; the abort at 10:45 within the hour posts nothing; the next PR waits over 20 min
    # and the hour has passed at 11:21
    assert [o.at for o in sat] == [ny(10, 10, 21), ny(10, 11, 21)]
    assert all(o.route == sd.pool_saturated_alert() and o.route.priority == "P2" for o in sat)
    assert "queue depth 1 (1 PR runs)" in sat[0].detail
    assert len(of(h.log, DeadlineReached)) == 2


def test_queue_depth_above_three_pr_runs_posts_pool_saturated_at_once():
    h = Harness(ny(10, 10, 0))
    h.send(merge("m1", "m0", 101, ny(10, 10, 0)))
    for n in (1, 2, 3):
        assert not [o for o in h.send(pr(n, f"b{n}", ny(10, 10, 0))) if isinstance(o, Alert)]
    (a,) = of(h.send(pr(4, "b4", ny(10, 10, 0))), Alert)
    assert a.alert == "pool saturated" and "4 PR runs" in a.detail


# -- merge runs coalesce (11.2, 1.6, 1.8, 9.4) -----------------------------------------------------------------
def test_three_merges_during_a_busy_pool_coalesce_with_newest_candidate_and_last_completed_base():
    h = Harness(ny(10, 10, 0), last_completed_main="m0")
    m1 = h.started(h.send(merge("m1", "m0", 101, ny(10, 10, 0))))
    assert m1.request.base == "m0" and m1.request.candidate == "m1" and not m1.request.coalesced
    h.send(merge("m2", "m1", 102, ny(10, 10, 10)))
    c3 = of(h.send(merge("m3", "m2", 103, ny(10, 10, 20))), Coalesced)
    c4 = of(h.send(merge("m4", "m3", 104, ny(10, 10, 30))), Coalesced)
    assert [c.build_id for c in c3 + c4] == ["m3", "m4"]
    assert len(h.q.queued()) == 1
    h.clock.advance(3600)
    s = h.started(h.send(RunFinished(m1.request.run_id, "complete")))
    r = s.request
    assert (r.candidate, r.base) == ("m4", "m1") and r.coalesced
    assert r.prs == (102, 103, 104) and r.manifest_refs == ("mf-102", "mf-103", "mf-104")
    assert r.range_label == "main m1..m4: 3 main builds, PRs #102, #103, #104"
    assert [type(t).__name__ for t in r.triggers] == ["MergeBuild"] * 3


def test_an_aborted_merge_run_leaves_the_base_so_its_prs_join_the_next_range():
    h = Harness(ny(10, 10, 0), last_completed_main="m0")
    m1 = h.started(h.send(merge("m1", "m0", 101, ny(10, 10, 0))))
    h.send(merge("m2", "m1", 102, ny(10, 10, 10)))
    s = h.started(h.send(RunFinished(m1.request.run_id, "aborted_harness")))
    assert s.request.base == "m0" and s.request.prs == (101, 102)
    h.send(RunFinished(s.request.run_id, "complete_with_env_errors"))
    assert h.q.last_completed_main == "m2"


def test_a_merge_run_past_its_3_hour_deadline_is_not_aborted_and_joins_the_next_merge():
    h = Harness(ny(10, 9, 0))
    h.started(h.send(rc("rc-b1", ny(10, 9, 0))))                           # an RC run holds every rig
    mq = of(h.send(merge("m1", "m0", 101, ny(10, 9, 0))), RunQueued)[0]
    (dl,) = of(h.until(ny(10, 12, 0)), DeadlineReached)
    assert dl.decision.disposition == "coalesce" and dl.request.run_id == mq.run_id
    assert dl.decision.result is None and not of(h.log, Alert)
    assert [r.run_id for r in h.q.queued()] == [mq.run_id]                   # still queued
    assert not of(h.until(ny(10, 13, 0)), DeadlineReached)                   # one coalesce, not one per tick
    assert of(h.send(merge("m2", "m1", 102, h.clock.now())), Coalesced)[0].run_id == mq.run_id


# -- the RC reservation, respin and start deadline (11.2, 1.6, 10.9) --------------------------------------------
def test_pr_runs_are_held_during_an_rc_reservation_with_their_deadline_suspended():
    h = Harness(ny(10, 10, 0))
    r = h.started(h.send(rc("rc-b1", ny(10, 10, 0))))
    assert h.q.reserved
    out = h.send(pr(9, "b", ny(10, 10, 0)))
    (ph,) = of(out, Placeholder)
    assert ph.text == "queued behind RC run, pool expected free at 14:00" and ph.pr == 9
    assert of(out, RunQueued)[0].deadline_at is None
    out = h.until(ny(10, 14, 1))
    assert not of(out, DeadlineReached) and not of(out, Alert)               # no pool_saturated, no P2
    assert [p.text for p in of(out, Placeholder)] == ["queued behind RC run, pool expected free at 16:00"]
    h.clock.advance(29 * 60)
    out = h.send(PoolReleased(r.request.run_id))
    s = h.started(out)
    assert s.request.candidate == "b" and s.rigs == (1, 2, 3, 4) and not h.q.reserved
    (end,) = of(out, ReservationEnded)                                       # the RC report's suspended window
    assert (end.run_id, end.start, end.end) == (r.request.run_id, ny(10, 10, 0), ny(10, 14, 30))
    assert end.held == (s.request.run_id,)


def test_rc_respin_supersedes_the_run_in_flight_and_takes_over_the_reservation():
    h = Harness(ny(10, 10, 0))
    old = h.started(h.send(rc("rc-b1", ny(10, 10, 0))))
    h.clock.advance(2 * 3600)
    out = h.send(rc("rc-b2", h.clock.now()))
    (sup,) = of(out, Supersede)
    assert sup.run_id == old.request.run_id and sup.by_build == "rc-b2" and sup.label == "superseded by rc-b2"
    (alert,) = of(out, Alert)
    assert alert.route == sd.rc_start_deadline_alert() and "release_manager" in alert.route.notified
    assert not of(out, RunStart) and h.q.reserved
    held = of(h.send(pr(4, "p", h.clock.now())), Placeholder)
    assert [p.text for p in held] == ["queued behind RC run, pool expected free at 16:00"]   # the respin + 4 h
    assert not of(h.until(ny(10, 12, 15)), Placeholder)                    # the old run stops at a flow boundary
    new = h.started(h.send(RunFinished(old.request.run_id, "aborted_budget")))
    assert new.request.candidate == "rc-b2" and new.request.supersedes == old.request.run_id
    assert new.rigs == (1, 2, 3, 4, 5, 6) and h.q.reserved
    assert [q.run_type for q in h.q.queued()] == ["pr"]                       # queued PR runs stay held
    assert not of(h.log, ReservationEnded)
    h.clock.advance(4 * 3600)
    (end,) = of(h.send(PoolReleased(new.request.run_id)), ReservationEnded)
    assert end.start == ny(10, 10, 0) and end.run_id == new.request.run_id   # one window across the respin


def test_rc_respin_successor_freed_after_22_00_waits_until_06_00_holding_the_reservation():
    # w4-calendar-queue:2: SD-76, 7.10, 11.2: an RC run outside the operating day waits until 06:00; a respin's
    # successor "starts at once" only inside it, and keeps the transferred reservation meanwhile
    h = Harness(ny(10, 15, 0))
    old = h.started(h.send(rc("rc-b1", ny(10, 15, 0))))
    h.until(ny(10, 21, 50))
    h.send(rc("rc-b2", ny(10, 21, 50)))
    out = h.until(ny(10, 22, 30))
    assert of(out, NightlySkipped)
    out = h.send(RunFinished(old.request.run_id, "aborted_budget"))
    catch_up = h.started(out)                                                 # rig 5 is free: canaries run tonight
    assert catch_up.request.run_type == "nightly" and catch_up.request.catch_up and catch_up.rigs == (5,)
    assert h.q.reserved and [r.run_type for r in h.q.queued()] == ["rc"]
    h.send(pr(3, "p", ny(10, 23, 0)))
    h.send(RunFinished(catch_up.request.run_id, "complete", canaries_passed=True))
    out = h.until(ny(11, 5, 59))
    assert not of(out, RunStart) and h.q.reserved
    new = h.started(h.until(ny(11, 6, 0)))
    assert new.request.candidate == "rc-b2" and new.request.supersedes == old.request.run_id
    assert new.at == ny(11, 6, 0) and new.rigs == (1, 2, 3, 4, 5, 6) and h.q.reserved
    assert [r.run_type for r in h.q.queued()] == ["pr"]                       # the PR run stays held


def test_rc_respin_successor_freed_inside_the_operating_day_starts_at_once():
    h = Harness(ny(10, 15, 0))
    old = h.started(h.send(rc("rc-b1", ny(10, 15, 0))))
    h.until(ny(10, 21, 0))
    h.send(rc("rc-b2", ny(10, 21, 0)))
    new = h.started(h.send(RunFinished(old.request.run_id, "aborted_budget")))
    assert new.request.candidate == "rc-b2" and new.at == ny(10, 21, 0)


def test_a_queued_rc_replaced_by_a_newer_build_of_its_train_never_ran():
    h = Harness(ny(10, 10, 0))
    h.send(merge("m1", "m0", 101, ny(10, 10, 0)))
    q1 = of(h.send(rc("rc-b1", ny(10, 10, 5))), RunQueued)[0]
    out = h.send(rc("rc-b2", ny(10, 10, 6)))
    assert [(r.run_id, r.run_type) for r in of(out, Replaced)] == [(q1.run_id, "rc")]
    assert not of(out, Supersede) and not of(out, Alert)


def test_rc_run_past_its_start_deadline_stays_queued_and_goes_to_the_release_manager():
    h = Harness(ny(10, 10, 0))
    h.send(rc("a-b1", ny(10, 10, 0), train="26.3"))                          # another train holds every rig
    signoff = ny(10, 20, 0)                                                 # start deadline 12:00
    rq = of(h.send(rc("b-b1", ny(10, 10, 5), train="26.4", signoff=signoff)), RunQueued)[0]
    assert rq.deadline_at == ny(10, 12, 0)
    out = h.until(ny(10, 12, 0))
    (dl,) = of(out, DeadlineReached)
    assert dl.decision.disposition == "release_manager" and dl.request.run_id == rq.run_id
    (a,) = of(out, Alert)
    assert a.alert == "RC start deadline" and a.route.priority == "P2" and "release_manager" in a.route.notified
    assert [r.run_id for r in h.q.queued()] == [rq.run_id]
    assert not of(h.until(ny(10, 13, 0)), DeadlineReached)


# -- the Nightly run: pre-emption, start deadline, catch-up (11.2, 1.6, 9.10, 10.9) ----------------------------
def test_an_rc_run_holding_the_nightly_rigs_at_22_00_skips_the_nightly_and_stamps_the_banner():
    h = Harness(ny(10, 21, 0))
    r = h.started(h.send(rc("rc-b1", ny(10, 21, 0))))
    out = h.until(ny(10, 22, 1))
    (skip,) = of(out, NightlySkipped)
    assert skip.by_run_id == r.request.run_id and skip.at == ny(10, 22, 0)
    (a,) = of(out, Alert)
    assert a.route == sd.nightly_skipped_alert() and a.route.priority == "P2"
    assert {"harness_on_call", "system_owner"} <= set(a.route.notified)
    writes = {(w.key, w.value) for w in of(out, StateWrite)}
    assert writes == {("nightly_skipped_for", r.request.run_id),
                      ("canaries_pending", f"last Nightly skipped for RC run {r.request.run_id}")}
    assert sd.canaries_pending_banner(h.q.state("canaries_pending")) == \
        f"canaries pending: last Nightly skipped for RC run {r.request.run_id}"
    assert not of(out, RunStart) and not of(out, DeadlineReached)
    # the catch-up of canaries, fire drill and safety canaries runs on rig 5 after the RC run
    h.until(ny(11, 0, 30))
    catch = h.started(h.send(RunFinished(r.request.run_id, "complete")))
    assert catch.request.catch_up and catch.rigs == (5,) and not catch.request.aa_pass
    out = h.send(RunFinished(catch.request.run_id, "complete", canaries_passed=True))
    assert {(w.key, w.value) for w in of(out, StateWrite)} == {("canaries_pending", ""), ("nightly_skipped_for", "")}
    assert sd.canaries_pending_banner(h.q.state("canaries_pending")) is None


def test_a_catch_up_that_never_ran_is_replaced_at_the_next_22_00():
    h = Harness(ny(10, 21, 0))
    r = h.started(h.send(rc("rc-b1", ny(10, 21, 0))))
    h.until(ny(10, 22, 1))
    catch_id = h.q.queued()[0].run_id
    h.send(PoolReleased(r.request.run_id))
    h.until(ny(11, 21, 59), step=600)
    # the RC still holds rig 5 the next night: a second skip, the stale catch-up replaced by the new one
    out = h.until(ny(11, 22, 1))
    assert [o.run_id for o in of(out, Replaced)] == [catch_id] and len(of(out, NightlySkipped)) == 1
    # rig 5 out of service when the RC run ends: the catch-up waits, and the next regular Nightly replaces it
    catch2 = h.q.queued()[0].run_id
    h.send(RigState(5, up=False))
    assert not of(h.send(RunFinished(r.request.run_id, "complete")), RunStart)
    out = h.until(ny(12, 22, 1), step=1800)
    assert not of(out, NightlySkipped) and [o.run_id for o in of(out, Replaced)] == [catch2]
    assert [(q.run_type, q.catch_up) for q in h.q.queued()] == [("nightly", False)]


def test_a_nightly_that_passes_clears_a_seeded_banner_and_one_that_fails_keeps_it():
    h = Harness(ny(10, 21, 59), canaries_pending="nightly run n-0 did not start")
    n = h.started(h.until(ny(10, 22, 0)))
    h.send(RunReachedRunning(n.request.run_id))
    assert not of(h.send(RunFinished(n.request.run_id, "complete", canaries_passed=False)), StateWrite)
    assert h.q.state("canaries_pending") == "nightly run n-0 did not start"
    n2 = h.started(h.until(ny(11, 22, 0), step=3600))
    out = h.send(RunFinished(n2.request.run_id, "complete", canaries_passed=True))
    assert [(w.key, w.value) for w in of(out, StateWrite)] == [("canaries_pending", "")]


def test_nightly_starts_at_22_00_on_rigs_4_and_5_and_waits_for_a_merge_holding_rig_4():
    h = Harness(ny(10, 21, 0))
    m = h.started(h.send(merge("m1", "m0", 101, ny(10, 21, 30))))
    out = h.until(ny(10, 22, 0))
    assert not of(out, RunStart) and of(out, RunQueued)[-1].run_type == "nightly"
    assert of(out, RunQueued)[-1].deadline_at == ny(11, 2, 0)
    h.clock.advance(3600)
    n = h.started(h.send(RunFinished(m.request.run_id, "complete")))
    assert n.request.run_type == "nightly" and n.rigs == (4, 5) and n.request.aa_pass
    assert n.request.triggered_at == ny(10, 22, 0)


def test_nightly_on_a_rig_not_at_running_by_02_00_is_start_failed_written_by_the_orchestrator():
    h = Harness(ny(10, 21, 59))
    n = h.started(h.until(ny(10, 22, 0)))
    out = h.until(ny(11, 2, 0), step=600)
    (dl,) = of(out, DeadlineReached)
    d = dl.decision
    assert dl.request.run_id == n.request.run_id and d.writer == "orchestrator"
    assert d.result.run_reason == "start_failed" and d.result.unreached == Outcome("harness_error", "start_failed")
    assert d.result.has("teardown")
    (a,) = of(out, Alert)
    assert a.route == alerts.aborted_harness() and a.route.priority == "P1"
    assert [(w.key, w.value) for w in of(out, StateWrite)] == \
        [("canaries_pending", f"nightly run {n.request.run_id} did not start")]
    assert not of(h.until(ny(11, 3, 0)), DeadlineReached)                    # once
    h.send(RunFinished(n.request.run_id, "aborted_harness"))                # the rigs come back


def test_nightly_that_reached_running_is_never_start_failed():
    h = Harness(ny(10, 21, 59))
    n = h.started(h.until(ny(10, 22, 0)))
    h.clock.advance(15 * 60)
    h.send(RunReachedRunning(n.request.run_id))
    assert not of(h.until(ny(11, 2, 30), step=600), DeadlineReached)


def test_queued_nightly_never_started_is_start_failed_written_by_the_reporter():
    h = Harness(ny(10, 21, 0), rigs_down=frozenset({5}))                  # rig 5 cannot be provisioned
    out = h.until(ny(11, 2, 0), step=900)
    (dl,) = of(out, DeadlineReached)
    assert dl.decision.writer == "reporter" and not dl.decision.result.has("teardown")
    assert h.q.queued() == ()


def test_only_the_latest_nightly_is_due_after_an_outage():
    h = Harness(ny(9, 21, 0))
    h.clock.advance(50 * 3600)                                               # Wed 23:00, two 22:00s passed
    out = h.q.tick()
    (s,) = of(out, RunStart)
    assert s.request.triggered_at == ny(11, 22, 0)


# -- the operating day (SD-76, 7.10, 11.2) --------------------------------------------------------------------
def test_a_pr_triggered_at_23_00_waits_until_06_00_and_its_deadline_starts_then():
    h = Harness(ny(10, 22, 30))
    h.send(merge("m1", "m0", 101, ny(10, 22, 30)))
    out = h.send(pr(3, "late", ny(10, 23, 0)))
    (ph,) = of(out, Placeholder)
    assert ph.text == "queued until 06:00 America/New_York"
    assert of(out, RunQueued)[-1].deadline_at is None and not of(out, RunStart)
    out = h.until(ny(11, 6, 0), step=600)
    starts = of(out, RunStart)
    assert [s.request.run_type for s in starts if s.request.run_type != "nightly"] == ["merge"]
    assert starts[-1].at == ny(11, 6, 0)
    assert not of(h.until(ny(11, 6, 44)), DeadlineReached)
    (dl,) = of(h.until(ny(11, 6, 45)), DeadlineReached)
    assert dl.decision.result.run_reason == "pool_saturated" and dl.decision.deadline == ny(11, 6, 45)


def test_a_pr_still_queued_at_22_00_waits_until_06_00_with_its_deadline_suspended():
    h = Harness(ny(10, 21, 30))
    h.send(merge("m1", "m0", 101, ny(10, 21, 30)))
    h.send(pr(3, "p", ny(10, 21, 50)))
    out = h.until(ny(10, 22, 0))
    assert [p.text for p in of(out, Placeholder)] == ["queued until 06:00 America/New_York"]
    assert not of(h.until(ny(11, 6, 34), step=900), DeadlineReached, "pr")    # 10 min used before 22:00
    (dl,) = of(h.until(ny(11, 6, 35)), DeadlineReached, "pr")
    assert dl.decision.result.run_reason == "pool_saturated"


def test_the_operating_day_predicate_is_injected():
    weekdays_only = lambda at: fixed_operating_day(at) and sd.to_local(at).weekday() < 5    # noqa: E731
    h = Harness(ny(14, 10, 0), operating_day=weekdays_only, rigs_down=frozenset({5}))     # Saturday; no Nightly
    out = h.send(pr(1, "sat", ny(14, 10, 0)))
    assert not of(out, RunStart) and of(out, Placeholder)[0].text == "queued until 06:00 America/New_York"
    (s,) = of(h.until(ny(16, 6, 0), step=3600 * 4), RunStart, "pr")
    assert s.request.candidate == "sat" and s.at == ny(16, 6, 0)


def test_open_time_integrates_the_operating_day_exactly():
    assert open_time(fixed_operating_day, ny(10, 5, 50), ny(10, 6, 20)) == timedelta(minutes=20)
    assert open_time(fixed_operating_day, ny(10, 21, 47, 13), ny(10, 22, 30)) == timedelta(minutes=12, seconds=47)
    assert open_time(fixed_operating_day, ny(10, 0, 0), ny(11, 0, 0)) == timedelta(hours=16)
    assert open_time(fixed_operating_day, ny(10, 23, 0), ny(11, 5, 0)) == timedelta(0)


# -- Calibration runs (11.2, 7.10, 7.13) ----------------------------------------------------------------------
def test_calibration_slots():
    night, start, due = calibration_slot("weekend", ny(11, 10, 0))           # Wednesday
    assert (night, start, due) == (date(2026, 11, 13), ny(13, 22, 15), ny(14, 2, 0))
    # inside the window, before Saturday 02:00, the window that is on still counts
    assert calibration_slot("weekend", ny(14, 1, 30))[0] == date(2026, 11, 13)
    assert calibration_slot("weekend", ny(14, 2, 0))[0] == date(2026, 11, 20)
    assert calibration_slot("emergency", ny(10, 10, 0)) == (date(2026, 11, 10), ny(10, 22, 15), ny(11, 1, 0))
    assert calibration_slot("emergency", ny(11, 0, 30)) == (date(2026, 11, 10), ny(10, 22, 15), ny(11, 1, 0))
    assert calibration_slot("emergency", ny(11, 1, 30))[0] == date(2026, 11, 11)
    # a holiday-aware calendar opening all_quiet on Thursday moves the run with it
    thursday = lambda after: ny(12, 20, 0) if after < ny(12, 20, 0) else friday_all_quiet(after)   # noqa: E731
    assert calibration_slot("weekend", ny(11, 10, 0), thursday)[:2] == (date(2026, 11, 12), ny(12, 22, 15))
    with pytest.raises(ValueError):
        calibration_slot("monthly", ny(11, 10, 0))


def test_weekend_calibration_runs_on_the_pool_and_the_nightly_keeps_rig_5_without_the_aa_pass():
    h = Harness(ny(13, 10, 0))                                                # Friday
    (sch,) = of(h.send(CalibrationRequest("weekend", ny(13, 10, 0))), Scheduled)
    assert sch.night == date(2026, 11, 13) and sch.start == ny(13, 22, 15) and sch.deadline_at == ny(14, 2, 0)
    assert h.q.queued() == ()                                                 # scheduled, not queued
    n = h.started(h.until(ny(13, 22, 0)))
    assert n.request.run_type == "nightly" and n.rigs == (5,) and not n.request.aa_pass
    c = h.started(h.until(ny(13, 22, 15)))
    assert c.request.run_type == "calibration" and c.rigs == (1, 2, 3, 4) and c.request.calibration == "weekend"


def test_a_weekend_calibration_not_running_by_saturday_02_00_is_start_failed_and_requeued():
    h = Harness(ny(13, 10, 0))
    h.send(CalibrationRequest("weekend", ny(13, 10, 0)))
    h.until(ny(13, 21, 0), step=3600)
    m = h.started(h.send(merge("m1", "m0", 101, ny(13, 21, 0))))              # holds the pool all night
    out = h.until(ny(14, 2, 0), step=600)
    (dl,) = of(out, DeadlineReached, "calibration")                          # the Nightly on rig 5 has its own
    assert dl.decision.result.run_reason == "start_failed"
    assert dl.decision.requeue and dl.decision.writer == "reporter"
    (again,) = of(out, Scheduled)
    assert again.night == date(2026, 11, 20) and again.request.requeue_of == dl.request.run_id
    assert ("canaries_pending", f"calibration run {dl.request.run_id} did not start") in \
        [(w.key, w.value) for w in of(out, StateWrite)]
    assert m.request.run_id != dl.request.run_id


def test_a_second_weekend_calibration_request_for_the_same_night_joins_the_first():
    # w4-calendar-queue:1: one weekend Calibration run per all_quiet window (11.2); the duplicate request a
    # fingerprint change gives each run until Friday must not wait behind it and end start_failed with a P1
    h = Harness(ny(12, 10, 0))                                                # Thursday
    (first,) = of(h.send(CalibrationRequest("weekend", ny(12, 10, 0))), Scheduled)
    thu = h.started(h.until(ny(12, 22, 0), step=3600))
    h.send(RunReachedRunning(thu.request.run_id))
    h.send(RunFinished(thu.request.run_id, "complete", canaries_passed=True))
    h.until(ny(13, 10, 0), step=3600)
    assert not of(h.send(CalibrationRequest("weekend", ny(13, 10, 0))), Scheduled)       # Friday: same night
    fri = h.started(h.until(ny(13, 22, 0), step=600))
    h.send(RunReachedRunning(fri.request.run_id))
    c = h.started(h.until(ny(13, 22, 15)))
    assert c.request.run_id == first.request.run_id and c.rigs == (1, 2, 3, 4)
    h.send(RunReachedRunning(c.request.run_id))
    assert not of(h.send(CalibrationRequest("weekend", ny(13, 23, 0))), Scheduled)       # the run on now covers it
    out = h.until(ny(14, 3, 0), step=600)
    assert not of(out, DeadlineReached) and not of(out, Alert) and not of(out, Scheduled)
    assert not [w for w in of(out, StateWrite) if w.key == "canaries_pending" and w.value]
    (nxt,) = of(h.send(CalibrationRequest("weekend", ny(14, 3, 0))), Scheduled)          # after 02:00: next week
    assert nxt.night == date(2026, 11, 20)


def test_two_emergency_calibration_requests_on_one_day_give_one_run():
    h = Harness(ny(10, 9, 0))
    assert len(of(h.send(CalibrationRequest("emergency", ny(10, 9, 0))), Scheduled)) == 1
    assert not of(h.send(CalibrationRequest("emergency", ny(10, 15, 0))), Scheduled)
    assert len(of(h.send(CalibrationRequest("weekend", ny(10, 15, 0))), Scheduled)) == 1   # another kind


def test_a_requeued_calibration_joins_a_run_already_scheduled_for_the_next_window():
    h = Harness(ny(10, 9, 0))
    h.send(CalibrationRequest("emergency", ny(10, 9, 0)))
    h.until(ny(10, 21, 0), step=3600)
    h.started(h.send(merge("m1", "m0", 101, ny(10, 21, 0))))                 # holds rigs 1 to 4 all night
    h.until(ny(11, 0, 59), step=600)
    h.clock.advance(60)                                                      # Wednesday 01:00, the start deadline
    out = h.send(CalibrationRequest("emergency", ny(11, 1, 0)))              # tonight's, a moment before it
    (dl,) = of(out, DeadlineReached, "calibration")
    assert dl.decision.requeue and dl.decision.result.run_reason == "start_failed"
    (sch,) = of(out, Scheduled)                                              # the requeue joins it
    assert sch.night == date(2026, 11, 11) and sch.request.requeue_of is None
    assert [e.run_id for e in h.q._entries.values() if e.run_type == "calibration"] == [sch.request.run_id]
    # and a request after a weekend run was requeued joins the requeued run
    w = Harness(ny(13, 10, 0))
    w.send(CalibrationRequest("weekend", ny(13, 10, 0)))
    w.until(ny(13, 21, 0), step=3600)
    w.started(w.send(merge("m1", "m0", 101, ny(13, 21, 0))))
    (again,) = of(w.until(ny(14, 2, 0), step=600), Scheduled)
    assert again.night == date(2026, 11, 20) and again.request.requeue_of is not None
    assert not of(w.send(CalibrationRequest("weekend", ny(14, 9, 0))), Scheduled)


def test_emergency_calibration_runs_tonight_on_rigs_1_to_3_beside_the_nightly():
    h = Harness(ny(10, 10, 0))
    (sch,) = of(h.send(CalibrationRequest("emergency", ny(10, 10, 0))), Scheduled)
    assert sch.start == ny(10, 22, 15) and sch.deadline_at == ny(11, 1, 0)
    n = h.started(h.until(ny(10, 22, 0)))
    assert n.rigs == (4, 5) and n.request.aa_pass
    c = h.started(h.until(ny(10, 22, 15)))
    assert c.rigs == (1, 2, 3) and c.request.calibration == "emergency"


# -- rigs, provisioning failures, wiring errors (7.2, 10.7) ----------------------------------------------------
def test_a_failed_provisioning_requeues_the_run_on_the_rigs_left():
    h = Harness(ny(10, 10, 0))
    s = h.started(h.send(pr(1, "b", ny(10, 10, 0))))
    h.send(RigState(3, up=False))
    again = h.started(h.send(Requeue(s.request.run_id)))
    assert again.request.run_id == s.request.run_id and again.rigs == (1, 2, 4)


def test_a_pr_with_no_pool_rig_in_service_takes_its_start_deadline():
    h = Harness(ny(10, 10, 0), rigs_down=frozenset({1, 2, 3, 4}))
    h.send(pr(1, "b", ny(10, 10, 0)))
    (dl,) = of(h.until(ny(10, 10, 45)), DeadlineReached)
    assert dl.decision.abort["runs_ahead"] == 0


def test_wiring_errors_are_refused():
    h = Harness(ny(10, 10, 0))
    with pytest.raises(ValueError):
        h.send(RunFinished("nope", "complete"))
    h.send(merge("m1", "m0", 101, ny(10, 10, 0)))
    q = of(h.send(pr(1, "b", ny(10, 10, 0))), RunQueued)[0]
    with pytest.raises(ValueError):
        h.send(RunFinished(q.run_id, "complete"))                             # never started
    with pytest.raises(ValueError):
        h.send(Requeue(q.run_id))
    with pytest.raises(ValueError):
        h.send(RigState(7, up=True))
    with pytest.raises(ValueError):
        PrBuild(1, "b", datetime(2026, 11, 10, 10, 0))                       # naive
    with pytest.raises(ValueError):
        CalibrationRequest("monthly", ny(10, 10, 0))


def test_a_requeued_pr_keeps_the_deadline_it_had_since_its_trigger():
    h = Harness(ny(10, 10, 0))
    s = h.started(h.send(pr(1, "b", ny(10, 10, 0))))
    h.clock.advance(30 * 60)
    for rig in (1, 2, 3, 4):
        h.send(RigState(rig, up=False))                                     # the hypervisor is unreachable
    assert not of(h.send(Requeue(s.request.run_id)), RunStart)
    assert not of(h.until(ny(10, 10, 44)), DeadlineReached)
    (dl,) = of(h.until(ny(10, 10, 45)), DeadlineReached)                    # 45 min from the trigger, not 10:30
    assert dl.request.run_id == s.request.run_id


def test_a_sparse_tick_still_posts_the_abort_p2_naming_the_queue_depth():
    h = Harness(ny(10, 10, 0))
    h.send(merge("m1", "m0", 101, ny(10, 10, 0)))
    pid = of(h.send(pr(1, "b", ny(10, 10, 0))), RunQueued)[0].run_id
    h.clock.advance(50 * 60)
    out = h.q.tick()
    (a,) = of(out, Alert)
    assert a.run_id == pid and a.alert == "pool saturated" and "queue depth 1" in a.detail
    assert of(out, DeadlineReached)[0].decision.deadline == ny(10, 10, 45)


def test_supersede_is_what_the_orchestrator_turns_into_aborted_budget_superseded():
    from sentinel.orchestrator.budgets import run_budget
    h = Harness(ny(10, 10, 0))
    h.send(rc("rc-b1", ny(10, 10, 0)))
    (sup,) = of(h.send(rc("rc-b2", ny(10, 11, 0))), Supersede)
    r = run_budget(now_s=3600, wall_budget_s=8 * 3600, report_margin_s=600, superseded=sup is not None)
    assert r.run_status == "aborted_budget" and r.run_reason == "superseded"
    assert r.unreached == Outcome("not_run", "budget")


def test_a_second_respin_replaces_the_queued_successor_and_keeps_the_reservation():
    h = Harness(ny(10, 10, 0))
    old = h.started(h.send(rc("rc-b1", ny(10, 10, 0))))
    h.send(rc("rc-b2", ny(10, 11, 0)))
    out = h.send(rc("rc-b3", ny(10, 11, 5)))
    (rep,) = of(out, Replaced)
    assert rep.run_type == "rc" and not of(out, Supersede) and h.q.reserved
    new = h.started(h.send(RunFinished(old.request.run_id, "aborted_budget")))
    assert new.request.candidate == "rc-b3" and new.request.supersedes == old.request.run_id


def test_weekend_saturday_nightly_stays_on_rig_5_while_the_calibration_holds_the_pool():
    h = Harness(ny(13, 21, 0))
    h.send(CalibrationRequest("weekend", ny(13, 21, 0)))
    fri = h.started(h.until(ny(13, 22, 0)))
    h.started(h.until(ny(13, 22, 15)))
    h.send(RunFinished(fri.request.run_id, "complete", canaries_passed=True))
    sat = h.started(h.until(ny(14, 22, 0), step=3600))
    assert sat.rigs == (5,) and not sat.request.aa_pass


def test_queue_positions_and_injected_run_ids():
    ids = iter(["r-a", "r-b", "r-c"])
    h = Harness(ny(10, 10, 0), run_ids=lambda run_type: next(ids))
    h.send(pr(1, "x", ny(10, 10, 0)))
    h.send(pr(2, "y", ny(10, 10, 1)))
    h.send(merge("m1", "m0", 101, ny(10, 10, 2)))
    assert [r.run_id for r in h.q.queued()] == ["r-c", "r-b"]
    assert (h.q.position("r-c"), h.q.position("r-b"), h.q.position("r-a")) == (1, 2, None)
    clash = Harness(ny(10, 10, 0), run_ids=lambda run_type: "same")
    clash.send(pr(1, "x", ny(10, 10, 0)))
    with pytest.raises(ValueError):
        clash.send(pr(2, "y", ny(10, 10, 0)))
    with pytest.raises(ValueError):
        clash.send(PoolReleased("same"))                                     # not an RC run


# -- the pipeline webhook (5.1, 1.7 T+0:00, 11.7 row 1) --------------------------------------------------------
def test_webhook_to_request_reads_pr_merge_and_rc_payloads():
    p = webhook_to_request({"kind": "pr", "build_id": "br-1", "commit": "abc", "pr": 48213,
                            "merge_base": "main@abc123", "triggered_at": "2026-11-10T10:31:00-05:00",
                            "manifest_ref": "manifests/48213.yaml", "diff": "..."})
    assert isinstance(p, PrBuild) and p.pr == 48213 and p.triggered_at == ny(10, 10, 31)
    assert p.payload["diff"] == "..." and p.manifest_ref == "manifests/48213.yaml"
    m = webhook_to_request({"kind": "merge", "build_id": "main-9", "commit": "def", "merged_pr": 48213,
                            "previous_main": "main-8", "triggered_at": "2026-11-10T11:00:00-05:00"})
    assert isinstance(m, MergeBuild) and m.previous_main == "main-8" and m.manifest_ref is None
    r = webhook_to_request({"kind": "rc", "build_id": "rc-1", "commit": "0f0", "train": "26.4",
                            "triggered_at": "2026-11-10T09:00:00-05:00", "signoff_at": "2026-11-17T17:00:00-05:00"})
    assert isinstance(r, RcBuild) and r.signoff_at == ny(17, 17, 0)
    q = RunQueue(ManualClock(ny(10, 10, 31)))
    assert of(q.handle(p), RunStart)[0].request.triggers == (p,)


@pytest.mark.parametrize("payload", [
    {"kind": "push", "build_id": "b", "commit": "c", "triggered_at": "2026-11-10T10:00:00-05:00"},
    {"kind": "pr", "build_id": "b", "commit": "c", "triggered_at": "2026-11-10T10:00:00-05:00", "merge_base": "m"},
    {"kind": "pr", "build_id": "b", "commit": "c", "pr": 0, "merge_base": "m",
     "triggered_at": "2026-11-10T10:00:00-05:00"},
    {"kind": "pr", "build_id": "b", "commit": "c", "pr": True, "merge_base": "m",
     "triggered_at": "2026-11-10T10:00:00-05:00"},
    {"kind": "merge", "build_id": "b", "commit": "c", "triggered_at": "2026-11-10T10:00:00"},
    {"kind": "merge", "build_id": "b", "commit": "c", "triggered_at": "yesterday"},
    {"kind": "rc", "build_id": "b", "commit": "c", "triggered_at": "2026-11-10T10:00:00-05:00"},
    {"kind": "rc", "build_id": "", "commit": "c", "train": "t", "triggered_at": "2026-11-10T10:00:00-05:00"},
    ["not", "a", "mapping"],
])
def test_webhook_to_request_refuses_bad_payloads(payload):
    with pytest.raises(WebhookError):
        webhook_to_request(payload)
