"""The real Reviewer adapter, part 3: the Message Batches path of merge, RC and Nightly runs (3.15, 10.7):
custom_id keying with results in any order, expiry and error retries, the borderline copies sent up
front, the deadline fallback to the synchronous path and the 24-hour give-up. Fake transport only."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from test_reviewer import FakeTransport, answer, config, item_data, make_item, outcome, resp

from sentinel.reviewer import BatchEntry, ReviewerJob, SpendLedger, custom_id
from sentinel.reviewer.schema import TASK_A, TASK_B

T = 0.80
DEADLINE = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
EARLY = DEADLINE - timedelta(hours=2)
UNAVAILABLE_DONE = ("needs_human", "reviewer_unavailable", "done")
UNAVAILABLE_PENDING = ("needs_human", "reviewer_unavailable", "pending")


def bjob(fake: FakeTransport, run_kind: str = "merge", **kw) -> ReviewerJob:
    ledger = kw.pop("ledger", None)
    return ReviewerJob(config(zero_data_retention=False, **kw), fake, run_kind, deadline=DEADLINE, ledger=ledger,
                       clock=lambda: 0.0)


def items() -> list:
    return [make_item(TASK_A, key="run-7|a1", region="r1", tier=1, base_hash="1" * 64),
            make_item(TASK_A, key="run-7|a2", region="r2", tier=2, base_hash="2" * 64),
            make_item(TASK_B, key="run-7|b3", region="r3", tier=2, base_hash="3" * 64)]


def deliver(fake: FakeTransport, bid: str, make, order=reversed) -> None:
    """Results for every request of batch `bid`, built by `make(custom_id, params)`, in `order`."""
    fake.results[bid] = [make(cid, p) for cid, p in order(fake.batches[bid])]


def ok(cid: str, params: dict, confidence: float = 0.95) -> BatchEntry:
    data = item_data(params)
    task = TASK_B if "dependency_edge" in data else TASK_A
    region = data["header"]["region"]
    return BatchEntry(cid, "succeeded", resp(answer(task, confidence=confidence, description=f"seen {region}")))


def test_merge_run_sends_one_batch_keyed_by_custom_id_with_three_copies_of_task_a():
    fake = FakeTransport()
    result = bjob(fake).review(items())
    assert fake.calls == [] and list(fake.batches) == ["msgbatch_01"]
    reqs = fake.batches["msgbatch_01"]
    ids = [cid for cid, _ in reqs]
    assert len(reqs) == 3 + 3 + 1 and len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid) for cid in ids)       # the API's custom_id rule
    assert ids[:3] == [custom_id("run-7|a1", c, 0) for c in range(3)]
    assert reqs[0][1] == reqs[1][1] == reqs[2][1]                               # identical copies, up front
    assert {v.state for v in result.verdicts.values()} == {"pending"} and result.batch_ids == ("msgbatch_01",)
    assert all(outcome(v.to_review(T))[:3] == UNAVAILABLE_PENDING for v in result.verdicts.values())


@pytest.mark.parametrize("kind", ["merge", "rc", "nightly"])
def test_merge_rc_and_nightly_use_the_batch_path_and_zero_data_retention_forces_sync(kind):
    fake = FakeTransport()
    bjob(fake, kind).review(items()[:1])
    assert len(fake.batches) == 1 and fake.calls == []
    zdr = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    v = ReviewerJob(config(), zdr, kind, deadline=DEADLINE).review(items()[:1]).verdicts["run-7|a1"]
    assert zdr.batches == {} and len(zdr.calls) == 1 and v.path == "sync"     # Batches are not ZDR-eligible


def test_results_in_any_order_are_matched_by_custom_id():
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items())
    assert j.poll(EARLY + timedelta(minutes=1)) == {}                         # not ended yet: nothing read
    deliver(fake, "msgbatch_01", ok, order=lambda r: sorted(r, key=lambda x: x[0]))
    final = j.poll(EARLY + timedelta(minutes=30))
    assert {k: v.answer["description"] for k, v in final.items()} == {
        "run-7|a1": "seen r1", "run-7|a2": "seen r2", "run-7|b3": "seen r3"}
    a1 = final["run-7|a1"]
    assert a1.path == "batch" and a1.calls == 3 and a1.cost_usd == pytest.approx(3 * 0.018 / 2)
    assert a1.version == j.version and j.pending() == {}
    assert outcome(a1.to_review(T))[:2] == ("intended", None)


def test_the_lowest_valid_copy_decides_whatever_the_arrival_order():
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[:1])
    first = custom_id("run-7|a1", 0, 0)
    deliver(fake, "msgbatch_01", lambda cid, p: BatchEntry(cid, "expired") if cid == first else ok(cid, p, 0.95))
    assert j.poll(EARLY) == {}                                                  # copy 0 resubmitted, still open
    assert [cid for cid, _ in fake.batches["msgbatch_02"]] == [custom_id("run-7|a1", 0, 1)]
    deliver(fake, "msgbatch_02", lambda cid, p: ok(cid, p, 0.85))              # borderline: vote over all three
    v = j.poll(EARLY + timedelta(minutes=5))["run-7|a1"]
    assert v.votes == ("matches_manifest",) * 3 and v.answer["confidence"] == pytest.approx(0.95)


def test_borderline_copy_waits_for_the_other_copies_and_the_majority_is_kept():
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[:1])
    conf = {0: 0.85, 1: 0.10, 2: 0.88}
    verdict = {0: "matches_manifest", 1: "does_not_match_manifest", 2: "matches_manifest"}
    deliver(fake, "msgbatch_01", lambda cid, p: BatchEntry(cid, "succeeded", resp(answer(
        verdict=verdict[int(cid.split("-")[2])], confidence=conf[int(cid.split("-")[2])]))))
    v = j.poll(EARLY)["run-7|a1"]
    assert v.votes == ("matches_manifest", "does_not_match_manifest", "matches_manifest")
    assert v.answer["confidence"] == pytest.approx(0.865)


def test_expired_entries_are_resubmitted_once_then_unavailable():
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[2:])
    deliver(fake, "msgbatch_01", lambda cid, p: BatchEntry(cid, "expired"))
    assert j.poll(EARLY) == {} and [c for c, _ in fake.batches["msgbatch_02"]] == [custom_id("run-7|b3", 0, 1)]
    assert fake.batches["msgbatch_02"][0][1] == fake.batches["msgbatch_01"][0][1]   # rebuilt byte for byte
    deliver(fake, "msgbatch_02", lambda cid, p: BatchEntry(cid, "expired"))
    v = j.poll(EARLY + timedelta(hours=1))["run-7|b3"]
    assert (v.state, v.cause) == ("unavailable", "batch_expired") and len(fake.batches) == 2
    assert outcome(v.to_review(T))[:3] == UNAVAILABLE_DONE


@pytest.mark.parametrize("second, state, cause", [
    ("succeeded", "answered", None), ("errored", "unavailable", "batch_errored")])
def test_errored_entries_are_retried_once(second, state, cause):
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[2:])
    deliver(fake, "msgbatch_01", lambda cid, p: BatchEntry(cid, "errored", error_type="api_error"))
    j.poll(EARLY)
    deliver(fake, "msgbatch_02", lambda cid, p: ok(cid, p) if second == "succeeded" else
            BatchEntry(cid, "errored", error_type="api_error"))
    v = j.poll(EARLY + timedelta(minutes=10))["run-7|b3"]
    assert (v.state, v.cause) == (state, cause)


def test_batch_schema_violation_is_resent_once_and_a_refusal_is_final():
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[2:])
    deliver(fake, "msgbatch_01", lambda cid, p: BatchEntry(cid, "succeeded", resp(text="not json")))
    j.poll(EARLY)
    deliver(fake, "msgbatch_02", lambda cid, p: BatchEntry(cid, "succeeded", resp(text="still not json")))
    failed = j.poll(EARLY + timedelta(minutes=1))["run-7|b3"]
    assert failed.cause == "schema_violation" and len(failed.violations) == 2
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[2:])
    deliver(fake, "msgbatch_01", lambda cid, p: BatchEntry(cid, "succeeded", resp(answer(TASK_B), stop="refusal")))
    v = j.poll(EARLY)["run-7|b3"]
    assert (v.state, v.cause, len(fake.batches)) == ("unavailable", "refusal", 1)


def test_tier_1_items_go_synchronous_at_the_deadline_minus_10_minutes_and_the_rest_give_up_after_24_hours():
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    j = bjob(fake)
    j.review(items())
    assert j.poll(DEADLINE - timedelta(minutes=11)) == {} and fake.calls == []
    at_margin = j.poll(DEADLINE - timedelta(minutes=10))
    assert list(at_margin) == ["run-7|a1"] and at_margin["run-7|a1"].path == "sync" and len(fake.calls) == 1
    assert sorted(j.pending()) == ["run-7|a2", "run-7|b3"]
    assert all(outcome(v.to_review(T))[:3] == UNAVAILABLE_PENDING for v in j.pending().values())
    assert j.poll(DEADLINE + timedelta(hours=23, minutes=59)) == {}
    gave_up = j.poll(DEADLINE + timedelta(hours=24))
    assert {v.cause for v in gave_up.values()} == {"pending_past_deadline"} and sorted(gave_up) == ["run-7|a2",
                                                                                                    "run-7|b3"]
    assert all(outcome(v.to_review(T))[:3] == UNAVAILABLE_DONE for v in gave_up.values())
    deliver(fake, "msgbatch_01", ok)                                            # late results change nothing
    assert j.poll(DEADLINE + timedelta(hours=25)) == {} and j.ledger.run_spent_usd > 0.018


def test_a_batch_that_cannot_be_created_falls_back_to_the_synchronous_path():
    fake = FakeTransport(respond=lambda p: ok("sync", p).response)
    fake.fail_batch_create = 1
    result = bjob(fake).review(items())
    assert fake.batches == {} and len(fake.calls) == 3
    assert {v.path for v in result.verdicts.values()} == {"sync"}


def test_the_per_run_ceiling_caps_every_item_still_awaiting_a_verdict():
    fake = FakeTransport()
    ledger = SpendLedger()
    j = bjob(fake, ledger=ledger)
    j.review(items())
    ledger.charge(50.0, "explorer")
    capped = j.poll(EARLY)
    assert len(capped) == 3 and all((v.state, v.cause, v.queued) == ("capped", "dollar_cap", True)
                                    for v in capped.values())
    assert all(outcome(v.to_review(T)) == ("needs_human", "reviewer_cap", "done", "cause: dollar_cap")
               for v in capped.values())


def test_unknown_custom_ids_are_ignored():
    fake = FakeTransport()
    j = bjob(fake)
    j.review(items()[2:])
    fake.results["msgbatch_01"] = [BatchEntry("rv-foreign-0-0", "expired")] + [
        ok(cid, p) for cid, p in fake.batches["msgbatch_01"]]
    assert list(j.poll(EARLY)) == ["run-7|b3"]


def test_a_batch_capable_run_needs_its_deadline_before_any_call():
    fake = FakeTransport()
    with pytest.raises(ValueError, match="deadline"):
        ReviewerJob(config(zero_data_retention=False), fake, "merge").review(items())
    assert fake.calls == [] and fake.batches == {}
