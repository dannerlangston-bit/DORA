"""The real Reviewer adapter, part 2: the synchronous path, failure mapping, the borderline vote, caps
and ceilings, the version stamp, the cache and the pipeline adapter (3.11 row 23, 3.13, 3.15, 10.7).
Every transport is a fake; nothing touches the network."""
from __future__ import annotations

import pytest
from test_reviewer import ENTRY, MODEL, FakeTransport, answer, config, item_data, make_item, outcome, resp, step

from sentinel.pipeline.reviewer import ReviewerAdapter, ReviewRequest, to_review, validate
from sentinel.reviewer import (SCHEMA_HASH, ModelReviewer, ReviewerJob, ReviewerUnavailable, SpendLedger,
                               TransportError, Usage, cost_usd, run_health, visibility_by_entry, vote)
from sentinel.reviewer.config import KNOWN_PRICES
from sentinel.reviewer.schema import TASK_A, TASK_B, TASK_C

T = 0.80


def job(transport: FakeTransport, run_kind: str = "pr", **kw) -> ReviewerJob:
    ledger = kw.pop("ledger", None)
    return ReviewerJob(config(**kw), transport, run_kind, ledger=ledger, clock=lambda: 0.0)


# -- answered verdicts carry the version tuple ----------------------------------------------------

def test_every_verdict_is_stamped_with_model_prompt_and_schema_hash():
    fake = FakeTransport([resp(answer(), rid="req_abc", usage=Usage(3000, 300, 100, 2000)),
                          resp(answer(), stop="refusal")])
    j = job(fake)
    v = j.review_now(make_item())
    assert v.state == "answered" and v.path == "sync" and v.calls == 1
    assert (v.version.model_id, v.version.prompt_hash, v.version.schema_hash) == (MODEL, j.prompt_hash, SCHEMA_HASH)
    assert v.response_models == (MODEL,) and v.request_ids == ("req_abc",)
    assert (v.few_shot_version, v.mask_version, v.redaction_version, v.registry_version) == ("none", "m1", "r1", "g1")
    assert v.usage == Usage(3000, 300, 100, 2000) and v.images_sent == 3 and v.bytes_sent > 0
    assert v.cost_usd == pytest.approx((3000 * 4 + 300 * 20 + 100 * 8 + 2000 * 0.2) / 1e6)
    r = v.to_review(T, "buy-button")
    assert (r.state, r.model_id, r.prompt_hash, r.defect_class) == ("answered", MODEL, j.prompt_hash, "color_change")
    assert outcome(r)[:2] == ("intended", None)                                # row 20
    refused = j.review_now(make_item(key="other", base_hash="d" * 64))
    assert refused.state == "unavailable" and refused.version == v.version     # failures are stamped too


# -- every failure maps to the right outcome (3.11 row 23, 3.15, 10.7) -----------------------------

UNAVAILABLE_DONE = ("needs_human", "reviewer_unavailable", "done")
UNAVAILABLE_PENDING = ("needs_human", "reviewer_unavailable", "pending")


@pytest.mark.parametrize("script, state, cause, calls", [
    ([resp(answer(), stop="refusal", category="cyber")], "unavailable", "refusal", 1),
    ([resp(text='{"schema_version": "rev', stop="max_tokens")], "unavailable", "max_tokens", 1),
    ([resp(answer(), model="claude-opus-5")], "unavailable", "model_mismatch", 1),
    ([resp(answer(confidence=1.4)), resp(answer(confidence=1.4))], "unavailable", "schema_violation", 2),
    ([resp(text="Sure! Here is the JSON"), resp(text="{}")], "unavailable", "schema_violation", 2),
    ([TransportError("client", "bad request", 400)], "unavailable", "client_error_400", 1),
    ([TransportError("client", "model not found", 404)], "unavailable", "client_error_404", 1),
    ([TransportError("timeout", "read timeout")], "pending", "timeout", 1),
    ([TransportError("server", "overloaded", 529)], "pending", "server", 1),
    ([TransportError("rate_limit", "slow down", 429)], "pending", "rate_limit", 1),
    ([TransportError("connection", "reset")], "pending", "connection", 1),
])
def test_failures_map_to_reviewer_unavailable(script, state, cause, calls):
    fake = FakeTransport(script)
    v = job(fake).review_now(make_item())
    assert (v.state, v.cause, len(fake.calls)) == (state, cause, calls)
    assert outcome(v.to_review(T))[:3] == (UNAVAILABLE_DONE if state == "unavailable" else UNAVAILABLE_PENDING)


def test_refusal_category_is_recorded_and_a_violation_is_logged_and_resent_once():
    v = job(FakeTransport([resp(answer(), stop="refusal", category="cyber")])).review_now(make_item())
    assert v.refusal_category == "cyber"
    fake = FakeTransport([resp(answer(confidence=2.0)), resp(answer())])
    v = job(fake).review_now(make_item())
    assert v.state == "answered" and v.calls == 2 and fake.calls[0] == fake.calls[1]
    assert v.violations == ("confidence outside [0, 1]",)


def test_run_health_alerts_on_call_above_2_percent_unavailable():
    fake = FakeTransport([resp(answer(), stop="refusal")], respond=lambda p: resp(answer(confidence=0.95)))
    items = [make_item(key=f"k{i:02d}", base_hash=f"{i:064d}", steps=(step(48, 32),)) for i in range(50)]
    health = run_health(job(fake).review(items).verdicts.values())
    assert health["items"] == 50 and health["by_state"] == {"answered": 49, "unavailable": 1}
    assert health["by_cause"] == {"refusal": 1} and not health["alert_on_call"]          # 2 percent exactly
    assert run_health([*job(FakeTransport([resp(answer(), stop="refusal")] * 2, respond=lambda p: resp(
        answer(confidence=0.95)))).review(items).verdicts.values()])["alert_on_call"]


# -- borderline rule: majority of three (3.15) -----------------------------------------------------

def test_borderline_confidence_is_resent_twice_and_the_majority_kept():
    fake = FakeTransport([resp(answer(confidence=0.85)), resp(answer(confidence=0.82)),
                          resp(answer(verdict="does_not_match_manifest", confidence=0.40))])
    v = job(fake).review_now(make_item())
    assert len(fake.calls) == 3 and fake.calls[0] == fake.calls[1] == fake.calls[2]
    assert v.votes == ("matches_manifest", "matches_manifest", "does_not_match_manifest")
    assert v.answer["verdict"] == "matches_manifest" and v.answer["confidence"] == pytest.approx(0.835)
    assert outcome(v.to_review(T))[:2] == ("intended", None)


def test_three_distinct_verdicts_are_unsure():
    fake = FakeTransport([resp(answer(confidence=0.75)), resp(answer(verdict="cannot_judge", confidence=0.5)),
                          resp(answer(verdict="does_not_match_manifest", confidence=0.9))])
    v = job(fake).review_now(make_item())
    assert v.answer["verdict"] == "cannot_judge" and v.answer["matched_manifest_entry_id"] is None
    assert v.answer["confidence"] == pytest.approx(0.75)
    assert outcome(v.to_review(T))[:3] == ("needs_human", "reviewer_uncertain", "done")


@pytest.mark.parametrize("confidence, calls", [(0.95, 1), (0.91, 1), (0.90, 3), (0.80, 3), (0.70, 3), (0.69, 1)])
def test_the_borderline_band_is_within_0_10_of_the_routing_threshold(confidence, calls):
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=confidence)))
    job(fake).review_now(make_item())
    assert len(fake.calls) == calls


def test_only_task_a_votes_and_a_failed_extra_copy_is_not_a_vote():
    fake = FakeTransport(respond=lambda p: resp(answer(TASK_B, confidence=0.8)))
    assert job(fake).review_now(make_item(TASK_B)).votes == () and len(fake.calls) == 1
    fake = FakeTransport([resp(answer(confidence=0.85)), resp(answer(), stop="refusal"), resp(answer(confidence=0.83))])
    v = job(fake).review_now(make_item())
    assert v.votes == ("matches_manifest", "matches_manifest") and v.answer["confidence"] == pytest.approx(0.84)
    fake = FakeTransport([resp(answer(confidence=0.85)), TransportError("timeout")])
    assert job(fake).review_now(make_item()).state == "pending"


def test_vote_keeps_the_agreeing_answer_nearest_the_median():
    a = [answer(confidence=0.9, description="a"), answer(confidence=0.7, description="b"),
         answer(confidence=0.75, description="c")]
    kept, votes = vote(a)
    assert kept["description"] == "c" and kept["confidence"] == 0.75 and votes == ("matches_manifest",) * 3


# -- caps and the dollar ceilings (3.15, 11.4) -----------------------------------------------------

def test_pr_call_cap_of_60_reviews_tier_1_first_and_caps_the_rest():
    items = [make_item(key=f"k{i:02d}", tier=1 if i % 2 else 2, area=i, steps=(step(48, 32),)) for i in range(62)]
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    result = job(fake).review(items)
    capped = [k for k, v in result.verdicts.items() if v.state == "capped"]
    assert len(fake.calls) == 60 and sorted(capped) == ["k00", "k02"]          # the two smallest tier-2 changes
    v = result.verdicts["k00"]
    assert (v.cause, v.queued, v.calls) == ("call_cap", False, 0)
    assert outcome(v.to_review(T)) == ("needs_human", "reviewer_cap", "none", "beyond the run's reviewer call cap")


@pytest.mark.parametrize("kind, cap", [("merge", 200), ("rc", 500), ("pr", 60)])
def test_caps_per_run_kind(kind, cap):
    assert config().call_cap(kind) == cap


def test_per_run_ceiling_stops_model_calls_and_caps_items_awaiting_a_verdict():
    ledger = SpendLedger(run_ceiling_usd=0.05)
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))     # 0.018 USD per call
    items = [make_item(key=f"k{i}", steps=(step(48, 32),)) for i in range(5)]
    result = job(fake, ledger=ledger).review(items)
    states = [result.verdicts[f"k{i}"].state for i in range(5)]
    assert len(fake.calls) == 3 and states == ["answered"] * 3 + ["capped"] * 2
    assert result.run_ceiling_reached and ledger.run_spent_usd == pytest.approx(0.054)
    v = result.verdicts["k4"]
    assert (v.cause, v.queued) == ("dollar_cap", True) and "per-run ceiling of 0.05 USD" in v.detail
    assert outcome(v.to_review(T)) == ("needs_human", "reviewer_cap", "done", "cause: dollar_cap")


def test_spend_from_other_components_counts_and_the_weekly_cap_excludes_from_the_start():
    ledger = SpendLedger()
    ledger.charge(49.99, "agent_rescue")
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    result = job(fake, ledger=ledger).review([make_item(key="a"), make_item(key="b", base_hash="e" * 64)])
    assert len(fake.calls) == 1 and result.verdicts["b"].cause == "dollar_cap" and result.run_ceiling_reached
    week = SpendLedger(week_spent_usd=500.0)
    fake = FakeTransport()
    v = job(fake, ledger=week).review([make_item()]).verdicts[make_item().queue_key]
    assert fake.calls == [] and (v.state, v.cause, v.queued) == ("capped", "dollar_cap", False)
    assert "weekly cap of 500 USD" in v.detail and not week.run_ceiling_reached
    assert outcome(v.to_review(T))[:3] == ("needs_human", "reviewer_cap", "none")


def test_cost_uses_batch_and_cache_prices():
    p = KNOWN_PRICES[MODEL]
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_creation_input_tokens=10_000,
              cache_read_input_tokens=500_000)
    assert cost_usd(u, p, batch=False) == pytest.approx(4.0 + 2.0 + 0.08 + 0.10)
    assert cost_usd(u, p, batch=True) == pytest.approx((4.0 + 2.0 + 0.08 + 0.10) / 2)


# -- fallback to the Batch path after transient failures (10.7) ------------------------------------

def _deadline():
    from datetime import datetime, timezone
    return datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def test_three_consecutive_transient_failures_send_the_rest_to_the_batch_path():
    fake = FakeTransport([TransportError("timeout")] * 3)
    items = [make_item(key=f"k{i}", region=f"r{i}") for i in range(5)]
    j = ReviewerJob(config(zero_data_retention=False), fake, "pr", deadline=_deadline())
    result = j.review(items)
    assert len(fake.calls) == 3 and list(fake.batches) == ["msgbatch_01"]
    regions = sorted({item_data(p)["header"]["region"] for _, p in fake.batches["msgbatch_01"]})
    assert regions == ["r0", "r1", "r2", "r3", "r4"]
    assert {v.path for v in result.verdicts.values()} == {"batch"} and result.batch_ids == ("msgbatch_01",)


def test_a_429_switches_at_once_and_without_a_batch_path_the_rest_waits_for_the_drainer():
    fake = FakeTransport([TransportError("rate_limit", "", 429)])
    items = [make_item(key=f"k{i}", region=f"r{i}") for i in range(3)]
    ReviewerJob(config(zero_data_retention=False), fake, "pr", deadline=_deadline()).review(items)
    assert len(fake.calls) == 1 and len(fake.batches["msgbatch_01"]) == 3 * 3   # three copies per task A item
    fake = FakeTransport([TransportError("rate_limit", "", 429)])
    result = job(fake).review(items)                                            # ZDR on: no Batch path
    assert len(fake.calls) == 1 and fake.batches == {}
    assert [result.verdicts[f"k{i}"].cause for i in range(3)] == ["rate_limit", "sync_paused", "sync_paused"]
    assert all(outcome(v.to_review(T))[:3] == UNAVAILABLE_PENDING for v in result.verdicts.values())


def test_concurrency_4_keeps_each_answer_with_its_item():
    def respond(params):
        region = item_data(params)["header"]["region"]
        return resp(answer(confidence=0.95, description=f"seen {region}"))
    fake = FakeTransport(respond=respond)
    items = [make_item(key=f"k{i}", region=f"r{i}", base_hash=f"{i:064d}") for i in range(9)]
    result = ReviewerJob(config(sync_concurrency=4), fake, "pr").review(items)
    assert all(result.verdicts[f"k{i}"].answer["description"] == f"seen r{i}" for i in range(9))


# -- verdict cache (3.13) -------------------------------------------------------------------------

def test_a_repeat_item_is_served_from_the_cache_without_a_call():
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    j = job(fake)
    first = j.review_now(make_item(key="run-1"))
    again = j.review_now(make_item(key="run-2"))
    assert len(fake.calls) == 1 and again.path == "cache" and again.queue_key == "run-2"
    assert again.answer == first.answer and again.cost_usd == 0 and again.version == first.version
    other_entry = ENTRY.__class__(**{**ENTRY.__dict__, "expected_visual": "The Sell button turns red instead."})
    j.review_now(make_item(key="run-3", entry=other_entry))
    assert len(fake.calls) == 2                                                 # a different intent is not a repeat


# -- the pipeline adapter (pipeline/reviewer.py ReviewerAdapter) -----------------------------------

def _request(task: str = TASK_A) -> ReviewRequest:
    return ReviewRequest(task=task, checkpoint_ids=make_item().checkpoint_ids, surface_id="dialog.order-confirmation",
                         region_id="buy-button", tier=1, data_mode="frozen", workspace_id="ws.tier-a.swing-trader",
                         diff={"changed_px": 1200}, observations=(), crop=(0, 0, 120, 60),
                         entry={"entry_id": ENTRY.entry_id})


def test_model_reviewer_implements_the_pipeline_adapter_protocol():
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    adapter = ModelReviewer(job(fake), lambda req: make_item(req.task))
    typed: ReviewerAdapter = adapter
    assert (typed.model_id, typed.prompt_hash) == (MODEL, adapter.job.prompt_hash)
    got = adapter.review(_request())
    assert validate(got, _request()) == []                                    # the pipeline's own 3.13 checks
    review = to_review(got, adapter, "buy-button")
    assert (review.model_id, review.prompt_hash) == (MODEL, adapter.prompt_hash)
    refusing = ModelReviewer(job(FakeTransport([resp(answer(), stop="refusal")])), lambda req: make_item())
    with pytest.raises(ReviewerUnavailable) as e:
        refusing.review(_request())
    assert e.value.review(T).state == "unavailable" and refusing.verdicts[0].cause == "refusal"


# -- task C: reverse reconciliation (3.12 item 4, 2.12) --------------------------------------------

def test_task_c_answers_feed_the_manifest_coverage_visibility_map():
    answers = iter([answer(TASK_C, visibility="not_visible", confidence=0.9),
                    answer(TASK_C, visibility="visible", confidence=0.9),
                    answer(TASK_C, visibility="unsure", confidence=0.5)])
    fake = FakeTransport(respond=lambda p: resp(next(answers)))
    other = ENTRY.__class__(**{**ENTRY.__dict__, "entry_id": "tos-48121-1"})
    j = job(fake)
    items = [make_item(TASK_C, key="c1", base_hash="1" * 64), make_item(TASK_C, key="c2", base_hash="2" * 64),
             make_item(TASK_C, key="c3", base_hash="3" * 64, entry=other)]
    verdicts = [j.review_now(i) for i in items]
    assert len(fake.calls) == 3 and all(v.votes == () for v in verdicts)        # no vote on task C
    text = fake.calls[0]["messages"][-1]["content"][0]["text"]
    assert text.startswith("Task: manifest_visibility") and '"manifest_entry"' in text
    assert visibility_by_entry(verdicts) == {"tos-48120-1": "visible", "tos-48121-1": "unsure"}
    with pytest.raises(ValueError, match="manifest coverage"):
        verdicts[0].to_review(T)
