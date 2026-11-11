"""The Reviewer job: admission, the synchronous and Batch paths, the borderline vote, failure mapping
and the version stamp on every verdict (3.11 input R, 3.13, 3.15, 10.7).

Every item ends in one `Verdict`, stamped with the reviewer version (model id, prompt hash, schema
hash) and the few-shot, mask, redaction and registry versions. `Verdict.to_review` gives the
classifier's input R (`classify.Review`), so every way a call can fail lands on the row 23 outcome
3.11 names for it:

- `unavailable` -> `needs_human (reason: reviewer_unavailable)`, `review_state: done`: refusal,
  max_tokens, a second schema violation, a non-rate-limit 4xx, a response model other than the pin,
  a batch entry expired or errored twice, an item still pending 24 hours after the deadline.
- `pending` -> `needs_human (reason: reviewer_unavailable)`, `review_state: pending`: a timeout, 5xx,
  429 or connection failure after the SDK's retries (deferred to the Batch path, or left for the
  drainer), and every item queued on the Batch path.
- `capped`, not queued -> `needs_human (reason: reviewer_cap)`, `review_state: none`: beyond the run's
  call cap (`call_cap`), or a dollar ceiling already reached when the item was admitted (`dollar_cap`).
- `capped` with `dollar_cap`, queued -> `needs_human (reason: reviewer_cap)`, `cause: dollar_cap`,
  `review_state: done`: the per-run ceiling or the weekly cap stopped calls while it awaited a verdict.

Paths (3.15, 10.7): a PR run, and any run while the Batch path is off (`ReviewerConfig.batch_enabled`),
sends every item synchronously with concurrency 4; after 3 consecutive transient failures or a 429
the rest of the run goes to the Batch path (or, without one, waits for the drainer). Merge, RC and
Nightly runs submit one Message Batch; `poll(now)` reads results by `custom_id` in any order,
resubmits `expired` entries once and retries `errored` ones once, sends tier-1 items still open at the
report deadline minus 10 minutes synchronously, and gives up on the rest 24 hours after the deadline.

Choices where the spec is ambiguous:

- `custom_id` (3.15 says `checkpoint_id:region:cluster`): the API allows only `^[a-zA-Z0-9_-]{1,64}$`
  (batch-processing docs, 2026-11-10), so the queue key maps to `rv-<40 hex of its sha256>-<copy>-
  <attempt>` and the job keeps the map back to the queue key.
- The borderline rule (3.15) applies to task A, the only task routed by the threshold; a confidence
  within 0.10 of it (inclusive) triggers it. The vote is over the valid answers: two or more agreeing
  give that verdict with the median of their confidences (the agreeing answer nearest that median is
  kept, lowest copy first); otherwise the item is `cannot_judge` (R `unsure`) at the median of all.
  A copy answered by a model other than the pin makes the item `unavailable` (`model_mismatch`, 3.12:
  every response model must equal the pin); another terminal failure of an extra copy (refusal,
  max_tokens, second violation) is not a vote; a copy stopped by a dollar ceiling makes the item
  `capped`, and a transient failure leaves it `pending`. Copy 0 is the primary on both paths: a terminal
  failure of copy 0 makes the item `unavailable`. The Batch path sends the three copies up front, decides
  on copy 0 once it is resolved and, when copy 0 is borderline, votes once every copy is resolved, so
  the Batch and synchronous paths map the same responses to the same verdict and the outcome never
  depends on result order.
- "3 consecutive failures" counts transient failures only (a refusal is about one item, not the
  endpoint). "A queue over 20 items" (10.7) is not a trigger here: 3.15 sends every PR item
  synchronously and the PR cap is 60.
- A batch that cannot be created falls back to the synchronous path ("the synchronous path is the
  fallback either way", 3.15). `canceled` entries are retried once like `errored` ones.
- The verdict cache (3.13) key adds the task and the intent reference (manifest entry or dependency
  edge) to (base hash, candidate hash, mask version, prompt hash, model id): the same pixels judged
  against a different entry must not reuse a verdict. Only answered verdicts are cached.
"""
from __future__ import annotations

import dataclasses
import hashlib
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..classify import ROUTING_THRESHOLD, Review
from ..egress import row_context
from ..pipeline.reviewer import CLOSEST_ORDER
from .budget import SpendLedger, cost_usd, rank_for_cap
from .config import RUN_KINDS, ReviewerConfig
from .prompt import FewShot, Prompt, ReviewItem
from .schema import SCHEMA_HASH, TASK_A, TASK_C, canonical_json, check_answer, parse_answer
from .transport import BatchEntry, ModelResponse, Transport, TransportError, Usage

SYNC_RUN_KINDS = frozenset({"pr"})


@dataclass(frozen=True)
class ReviewerVersion:
    """The version tuple stamped on every verdict (3.15): model id, prompt hash, schema hash."""

    model_id: str
    prompt_hash: str
    schema_hash: str


@dataclass(frozen=True)
class Verdict:
    """The result of one Reviewer item, answered or not, with everything 3.13 stores beside it."""

    queue_key: str
    task: str
    checkpoint_ids: tuple[str, ...]
    entry_id: str | None
    state: str                           # answered | pending | unavailable | capped
    version: ReviewerVersion
    few_shot_version: str
    answer: Mapping[str, Any] | None = None
    cause: str | None = None
    detail: str | None = None
    queued: bool = False                 # capped: the item was awaiting a verdict when calls stopped
    path: str = "sync"                   # sync | batch | cache | none
    votes: tuple[str, ...] = ()
    response_models: tuple[str, ...] = ()
    request_ids: tuple[str, ...] = ()
    usage: Usage = Usage()
    cost_usd: float = 0.0
    latency_ms: int = 0
    calls: int = 0
    images_sent: int = 0
    bytes_sent: int = 0
    refusal_category: str | None = None
    violations: tuple[str, ...] = ()     # each logged reviewer_schema_violation (3.13)
    mask_version: str = ""
    redaction_version: str = ""
    registry_version: str = ""

    def __post_init__(self) -> None:
        if self.state not in ("answered", "pending", "unavailable", "capped"):
            raise ValueError(f"verdict state {self.state!r}")
        if (self.state == "answered") != (self.answer is not None):
            raise ValueError("an answered verdict carries its answer, and only it")
        if self.state == "capped" and self.cause not in ("call_cap", "dollar_cap"):
            raise ValueError("a capped verdict names call_cap or dollar_cap (3.15)")

    @property
    def defect_class(self) -> str | None:
        """The closest defect class among the observed changes (3.12 task B)."""
        types = [oc["change_type"] for oc in (self.answer or {}).get("observed_changes", [])]
        return min(types, key=CLOSEST_ORDER.index) if types else None

    @property
    def visibility(self) -> str | None:
        """The task C answer, when this is an answered task C item."""
        return self.answer["visibility"] if self.answer is not None and self.task == TASK_C else None

    def to_review(self, threshold: float = ROUTING_THRESHOLD, region_pointer: str | None = None) -> Review:
        """The classifier's input R (3.11) for a task A or B item."""
        if self.task == TASK_C:
            raise ValueError("task C feeds the manifest coverage table, not the classifier (2.12)")
        if self.state == "answered":
            a = self.answer
            return Review("answered", verdict=a["verdict"], confidence=float(a["confidence"]), threshold=threshold,
                          model_id=self.version.model_id, prompt_hash=self.version.prompt_hash,
                          description=a["description"], region_pointer=region_pointer,
                          defect_class=self.defect_class)
        if self.state == "pending":
            return Review("pending")
        if self.state == "capped":
            return Review("capped", cause=self.cause, queued=self.queued)
        return Review("unavailable", cause=self.cause)


class VerdictCache(Protocol):
    """The 3.13 verdict cache (the Central Store in production)."""

    def get(self, key: tuple[str, ...]) -> Verdict | None: ...

    def put(self, key: tuple[str, ...], verdict: Verdict) -> None: ...


@dataclass
class MemoryCache:
    """An in-process `VerdictCache`."""

    entries: dict[tuple[str, ...], Verdict] = field(default_factory=dict)

    def get(self, key: tuple[str, ...]) -> Verdict | None:
        return self.entries.get(key)

    def put(self, key: tuple[str, ...], verdict: Verdict) -> None:
        self.entries[key] = verdict


@dataclass
class JobResult:
    """The verdict of every item handed to `review`, keyed by queue key."""

    verdicts: dict[str, Verdict]
    run_ceiling_reached: bool
    batch_ids: tuple[str, ...] = ()


def vote(answers: Sequence[Mapping[str, Any]]) -> tuple[dict, tuple[str, ...]]:
    """The 3.15 majority vote (see the module docstring for the exact rule)."""
    verdicts = tuple(a["verdict"] for a in answers)
    top, n = Counter(verdicts).most_common(1)[0]
    if n >= 2:
        agreeing = [a for a in answers if a["verdict"] == top]
        med = statistics.median(float(a["confidence"]) for a in agreeing)
        kept = min(range(len(agreeing)), key=lambda i: (abs(float(agreeing[i]["confidence"]) - med), i))
        return dict(agreeing[kept], confidence=med), verdicts
    order = sorted(range(len(answers)), key=lambda i: (float(answers[i]["confidence"]), i))
    med = statistics.median(float(a["confidence"]) for a in answers)
    return dict(answers[order[(len(order) - 1) // 2]], verdict="cannot_judge", matched_manifest_entry_id=None,
                confidence=med), verdicts


def visibility_by_entry(verdicts: Iterable[Verdict]) -> dict[str, str]:
    """Task C answers per manifest entry, the `visibility` input of `coverage.manifest_coverage` (2.12).

    An entry is `visible` when any covering checkpoint showed it, else `unsure` when any answer was
    unsure, else `not_visible`. Unanswered items are left out, so the table falls back to the diff
    evidence for them. An entry with no covering checkpoint never gets an item (3.12 task C).
    """
    seen: dict[str, set[str]] = {}
    for v in verdicts:
        if v.visibility is not None and v.entry_id is not None:
            seen.setdefault(v.entry_id, set()).add(v.visibility)
    return {e: next(x for x in ("visible", "unsure", "not_visible") if x in s) for e, s in sorted(seen.items())}


def run_health(verdicts: Iterable[Verdict], alert_share: float = 0.02) -> dict:
    """The Reviewer's run-health line (3.15): items by state and cause, schema violations, spend, and
    whether unavailable items exceed 2 percent of the run's items (the on-call alert)."""
    vs = list(verdicts)
    unavailable = sum(v.state == "unavailable" for v in vs)
    return {"items": len(vs), "by_state": dict(sorted(Counter(v.state for v in vs).items())),
            "by_cause": dict(sorted(Counter(v.cause for v in vs if v.cause).items())),
            "schema_violations": sum(len(v.violations) for v in vs), "cost_usd": sum(v.cost_usd for v in vs),
            "alert_on_call": bool(vs) and unavailable / len(vs) > alert_share}


def custom_id(queue_key: str, copy: int, attempt: int) -> str:
    """The Message Batches `custom_id` of one copy of one item (module docstring)."""
    return f"rv-{hashlib.sha256(queue_key.encode()).hexdigest()[:40]}-{copy}-{attempt}"


@dataclass
class _Acc:
    """Calls made for one item: usage, cost, ids, latency, egress counts."""

    images: int
    bytes: int
    usage: Usage = Usage()
    cost: float = 0.0
    models: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    latency_ms: int = 0
    calls: int = 0
    refusal_category: str | None = None
    violations: list[str] = field(default_factory=list)


@dataclass
class _Copy:
    status: str = "waiting"              # waiting | answer | failed
    answer: dict | None = None
    cause: str | None = None
    attempt: int = 0
    retries: Counter = field(default_factory=Counter)


@dataclass
class _BatchState:
    items: dict[str, ReviewItem] = field(default_factory=dict)
    copies: dict[str, list[_Copy]] = field(default_factory=dict)
    acc: dict[str, _Acc] = field(default_factory=dict)
    entries: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    open: list[str] = field(default_factory=list)
    all_ids: list[str] = field(default_factory=list)
    unsent: list[tuple[str, int]] = field(default_factory=list)
    final: set[str] = field(default_factory=set)
    unknown_ids: list[str] = field(default_factory=list)
    fallback_done: bool = False


class ReviewerJob:
    """The Reviewer for one run: implements every path of 3.15 over an injected `Transport`."""

    def __init__(self, config: ReviewerConfig, transport: Transport, run_kind: str, *,
                 few_shots: Sequence[FewShot] = (), ledger: SpendLedger | None = None,
                 cache: VerdictCache | None = None, deadline: datetime | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if run_kind not in RUN_KINDS:
            raise ValueError(f"run kind {run_kind!r} not in {RUN_KINDS}")
        self.config, self.transport, self.run_kind = config, transport, run_kind
        self.prompt = Prompt(config, few_shots)
        self.version = ReviewerVersion(config.model_id, self.prompt.prompt_hash, SCHEMA_HASH)
        self.ledger = ledger or SpendLedger(config.run_ceiling_usd, config.weekly_cap_usd)
        self.cache = cache if cache is not None else MemoryCache()
        self.deadline = deadline
        self._clock = clock
        self._prices = config.price_table()
        self._admitted = 0
        self._batch: _BatchState | None = None

    # -- public API -----------------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self.version.model_id

    @property
    def prompt_hash(self) -> str:
        return self.version.prompt_hash

    def review(self, items: Sequence[ReviewItem]) -> JobResult:
        """Admit `items` (cache, call cap, spend) and send them on the run kind's path."""
        keys = [i.queue_key for i in items]
        if len(set(keys)) != len(keys):
            raise ValueError("queue keys must be unique within one review call (10.7)")
        if self.config.batch_enabled and self.deadline is None:
            raise ValueError("the Batch path needs the run's report deadline (3.15)")
        verdicts: dict[str, Verdict] = {}
        todo = self._admit(items, verdicts)
        if self.run_kind in SYNC_RUN_KINDS or not self.config.batch_enabled:
            self._run_sync(todo, verdicts, allow_switch=True)
        else:
            self._submit(todo, verdicts)
        return JobResult(verdicts, self.ledger.run_ceiling_reached, tuple(self._batch.all_ids if self._batch else ()))

    def review_now(self, item: ReviewItem) -> Verdict:
        """One item on the synchronous path, after admission (the pipeline adapter's entry point)."""
        verdicts: dict[str, Verdict] = {}
        for admitted in self._admit([item], verdicts):
            verdicts[admitted.queue_key] = self._sync_item(admitted)
        return verdicts[item.queue_key]

    def poll(self, now: datetime) -> dict[str, Verdict]:
        """Advance the Batch path at time `now`; returns the verdicts that became final."""
        st = self._batch
        if st is None:
            return {}
        newly: dict[str, Verdict] = {}
        if self.ledger.stop_reason() == "run":            # 3.15: the run ends; nothing awaits a verdict
            for key in self._open_keys():
                newly[key] = self._final(key, self._capped(st.items[key], "run", queued=True, acc=st.acc[key]))
            return newly
        retry: list[tuple[str, int]] = list(st.unsent)
        st.unsent.clear()
        for bid in list(st.open):
            try:
                if not self.transport.batch_ended(bid):
                    continue
                entries = list(self.transport.batch_results(bid))
            except TransportError:
                continue                                    # read it at the next poll
            st.open.remove(bid)
            for e in entries:
                self._on_entry(e, retry)
        self._resubmit([r for r in retry if r[0] not in st.final])
        for key in self._open_keys():
            v = self._try_finalize(key)
            if v is not None:
                newly[key] = self._final(key, v)
        if self.deadline is not None and now >= self.deadline - timedelta(seconds=self.config.deadline_margin_s) \
                and not st.fallback_done:
            st.fallback_done = True
            tier1 = [k for k in self._open_keys() if st.items[k].tier == 1]
            with ThreadPoolExecutor(max_workers=self.config.sync_concurrency) as pool:
                late = list(pool.map(lambda k: self._sync_item(st.items[k], acc=st.acc[k]), tier1))
            for key, v in zip(tier1, late):
                if v.state != "pending":
                    newly[key] = self._final(key, v)
        if self.deadline is not None and now >= self.deadline + timedelta(seconds=self.config.give_up_after_s):
            for key in self._open_keys():
                newly[key] = self._final(key, self._verdict(st.items[key], "unavailable", st.acc[key], path="batch",
                                                            cause="pending_past_deadline"))
        return newly

    def pending(self) -> dict[str, Verdict]:
        """Items on the Batch path still awaiting a verdict."""
        st = self._batch
        if st is None:
            return {}
        return {k: self._verdict(st.items[k], "pending", st.acc[k], path="batch") for k in self._open_keys()}

    # -- admission ------------------------------------------------------------------------------

    def _cache_key(self, item: ReviewItem) -> tuple[str, ...] | None:
        if not (item.base_hash and item.candidate_hash):
            return None
        return (item.base_hash, item.candidate_hash, item.mask_version, self.prompt_hash, self.model_id,
                item.task, item.intent_sha256())

    def _admit(self, items: Sequence[ReviewItem], verdicts: dict[str, Verdict]) -> list[ReviewItem]:
        todo = []
        cap = self.config.call_cap(self.run_kind)
        for item in rank_for_cap(items):
            key = self._cache_key(item)
            hit = self.cache.get(key) if key else None
            if hit is not None:
                verdicts[item.queue_key] = dataclasses.replace(
                    hit, queue_key=item.queue_key, checkpoint_ids=item.checkpoint_ids, path="cache", usage=Usage(),
                    cost_usd=0.0, latency_ms=0, calls=0, images_sent=0, bytes_sent=0)
                continue
            stop = self.ledger.stop_reason()
            if self._admitted >= cap:
                verdicts[item.queue_key] = self._verdict(item, "capped", None, path="none", cause="call_cap",
                                                         detail=f"beyond the {self.run_kind} cap of {cap}")
            elif stop is not None:
                verdicts[item.queue_key] = self._capped(item, stop, queued=False, acc=None)
            else:
                self._admitted += 1
                todo.append(item)
        return todo

    # -- one call and its evaluation ------------------------------------------------------------

    def _new_acc(self, params: Mapping[str, Any]) -> _Acc:
        images = sum(1 for m in params["messages"] for b in m["content"] if b.get("type") == "image")
        return _Acc(images=images, bytes=len(canonical_json(params).encode()))

    def _charge(self, acc: _Acc, resp: ModelResponse, *, batch: bool, latency_ms: int = 0) -> None:
        usd = cost_usd(resp.usage, self._prices, batch=batch)
        self.ledger.charge(usd, "reviewer")
        acc.usage, acc.cost = acc.usage + resp.usage, acc.cost + usd
        acc.models.append(resp.model)
        if resp.request_id:
            acc.request_ids.append(resp.request_id)
        acc.latency_ms += latency_ms
        acc.calls += 1
        acc.refusal_category = acc.refusal_category or resp.refusal_category

    def _evaluate(self, resp: ModelResponse, item: ReviewItem) -> tuple[str, Any]:
        """('answer', dict) | ('violation', problems) | ('failed', cause) for one response (3.13, 3.15)."""
        if resp.stop_reason in ("refusal", "max_tokens"):
            return "failed", resp.stop_reason
        if resp.model != self.model_id:
            return "failed", "model_mismatch"
        answer, err = parse_answer(resp.text)
        problems = [err] if err else check_answer(answer, task=item.task, entry_id=item.entry_id,
                                                  crop_sizes=item.crop_sizes())
        return ("violation", problems) if problems else ("answer", answer)

    def _attempt(self, item: ReviewItem, params: Mapping[str, Any], acc: _Acc) -> tuple[str, Any]:
        """One answer, re-sent once on a schema violation (3.13): ('answer', dict), ('failed', cause),
        ('transient', kind) or ('stopped', 'run' | 'week')."""
        for _ in (1, 2):
            stop = self.ledger.stop_reason()
            if stop is not None:
                return "stopped", stop
            t0 = self._clock()
            try:
                resp = self.transport.create_message(params)
            except TransportError as e:
                acc.latency_ms += int((self._clock() - t0) * 1000)
                if e.transient:
                    return "transient", e.kind
                return "failed", f"client_error_{e.status}" if e.status else "client_error"
            self._charge(acc, resp, batch=False, latency_ms=int((self._clock() - t0) * 1000))
            kind, value = self._evaluate(resp, item)
            if kind != "violation":
                return kind, value
            acc.violations.append("; ".join(value))
        return "failed", "schema_violation"

    def _borderline(self, confidence: float) -> bool:
        return round(abs(float(confidence) - self.config.routing_threshold), 9) <= self.config.borderline_band

    # -- verdict construction -------------------------------------------------------------------

    def _verdict(self, item: ReviewItem, state: str, acc: _Acc | None, *, path: str, answer: dict | None = None,
                 cause: str | None = None, detail: str | None = None, queued: bool = False,
                 votes: tuple[str, ...] = ()) -> Verdict:
        acc = acc or _Acc(images=0, bytes=0)
        return Verdict(
            queue_key=item.queue_key, task=item.task, checkpoint_ids=item.checkpoint_ids, entry_id=item.entry_id,
            state=state, version=self.version, few_shot_version=self.prompt.few_shot_version, answer=answer,
            cause=cause, detail=detail, queued=queued, path=path, votes=votes, response_models=tuple(acc.models),
            request_ids=tuple(acc.request_ids), usage=acc.usage, cost_usd=acc.cost, latency_ms=acc.latency_ms,
            calls=acc.calls, images_sent=acc.images * acc.calls, bytes_sent=acc.bytes * acc.calls,
            refusal_category=acc.refusal_category, violations=tuple(acc.violations), mask_version=item.mask_version,
            redaction_version=item.redaction_version, registry_version=item.registry_version)

    def _capped(self, item: ReviewItem, stop: str, *, queued: bool, acc: _Acc | None) -> Verdict:
        detail = (f"per-run ceiling of {self.ledger.run_ceiling_usd:g} USD" if stop == "run"
                  else f"weekly cap of {self.ledger.weekly_cap_usd:g} USD")
        return self._verdict(item, "capped", acc, path="none", cause="dollar_cap", detail=detail, queued=queued)

    def _answered(self, item: ReviewItem, answer: dict, acc: _Acc, *, path: str,
                  votes: tuple[str, ...] = ()) -> Verdict:
        v = self._verdict(item, "answered", acc, path=path, answer=answer, votes=votes)
        key = self._cache_key(item)
        if key:
            self.cache.put(key, v)
        return v

    def _unanswered(self, item: ReviewItem, kind: str, value: str, acc: _Acc, *, path: str) -> Verdict:
        if kind == "stopped":
            return self._capped(item, value, queued=True, acc=acc)
        return self._verdict(item, "pending" if kind == "transient" else "unavailable", acc, path=path, cause=value)

    # -- synchronous path -----------------------------------------------------------------------

    def _sync_item(self, item: ReviewItem, acc: _Acc | None = None) -> Verdict:
        """One item on the synchronous path. Its requests carry the item's egress row fields (6.10, 8.3, 8.9): the
        checkpoint, the redaction version, the stored capture its crops came from, and that the payload is an
        image, so the gate refuses it without a redaction version; set here, in the thread that sends."""
        with row_context(checkpoint_id=item.checkpoint_ids[0], redaction_version=item.redaction_version or None,
                         artifact_sha256=item.artifact_sha256, image=bool(item.steps) or None):
            return self._sync_item_sent(item, acc)

    def _sync_item_sent(self, item: ReviewItem, acc: _Acc | None) -> Verdict:
        params = self.prompt.params(item)
        if acc is None:
            acc = self._new_acc(params)
        kind, value = self._attempt(item, params, acc)
        if kind != "answer":
            return self._unanswered(item, kind, value, acc, path="sync")
        if item.task != TASK_A or not self._borderline(value["confidence"]):
            return self._answered(item, value, acc, path="sync")
        answers = [value]
        for _ in range(self.config.vote_copies - 1):
            k, v = self._attempt(item, params, acc)
            if k == "answer":
                answers.append(v)
            elif k in ("transient", "stopped") or v == "model_mismatch":
                return self._unanswered(item, k, v, acc, path="sync")
        answer, votes = vote(answers)
        return self._answered(item, answer, acc, path="sync", votes=votes)

    def _run_sync(self, items: Sequence[ReviewItem], verdicts: dict[str, Verdict], *, allow_switch: bool) -> None:
        cfg = self.config
        consecutive, switched, i = 0, False, 0
        deferred: list[ReviewItem] = []
        with ThreadPoolExecutor(max_workers=cfg.sync_concurrency) as pool:
            while i < len(items) and not switched:
                wave = items[i:i + cfg.sync_concurrency]
                i += len(wave)
                for item, v in zip(wave, pool.map(self._sync_item, wave)):
                    verdicts[item.queue_key] = v
                    if v.state == "pending":
                        deferred.append(item)
                        consecutive += 1
                        switched = switched or v.cause == "rate_limit" or consecutive >= cfg.consecutive_failure_switch
                    else:
                        consecutive = 0
        rest = list(items[i:])
        if allow_switch and cfg.batch_enabled and (deferred or rest):
            self._submit(deferred + rest, verdicts)
            return
        for item in rest:
            verdicts[item.queue_key] = self._verdict(item, "pending", None, path="sync", cause="sync_paused",
                                                     detail="consecutive transient failures; the drainer retries")

    # -- Batch path -----------------------------------------------------------------------------

    def _open_keys(self) -> list[str]:
        st = self._batch
        return [k for k in st.items if k not in st.final] if st else []

    def _final(self, key: str, v: Verdict) -> Verdict:
        self._batch.final.add(key)
        return v

    def _submit(self, items: Sequence[ReviewItem], verdicts: dict[str, Verdict]) -> None:
        if not items:
            return
        st = self._batch = self._batch or _BatchState()
        stop = self.ledger.stop_reason()
        requests, fresh = [], []
        for item in items:
            if stop is not None:
                verdicts[item.queue_key] = self._capped(item, stop, queued=True, acc=None)
                continue
            params = self.prompt.params(item)
            st.items[item.queue_key] = item
            st.acc[item.queue_key] = self._new_acc(params)
            n = self.config.vote_copies if item.task == TASK_A else 1
            st.copies[item.queue_key] = [_Copy() for _ in range(n)]
            for c in range(n):
                cid = custom_id(item.queue_key, c, 0)
                st.entries[cid] = (item.queue_key, c, 0)
                requests.append((cid, params))
            fresh.append(item)
        if not requests:
            return
        try:
            bid = self.transport.create_batch(requests)
        except TransportError:
            for item in fresh:                              # 3.15: the synchronous path is the fallback
                del st.items[item.queue_key], st.copies[item.queue_key], st.acc[item.queue_key]
            self._run_sync(fresh, verdicts, allow_switch=False)
            return
        st.open.append(bid)
        st.all_ids.append(bid)
        for item in fresh:
            verdicts[item.queue_key] = self._verdict(item, "pending", st.acc[item.queue_key], path="batch")

    def _on_entry(self, e: BatchEntry, retry: list[tuple[str, int]]) -> None:
        st = self._batch
        ref = st.entries.get(e.custom_id)
        if ref is None:
            st.unknown_ids.append(e.custom_id)
            return
        key, ci, attempt = ref
        item, acc, c = st.items[key], st.acc[key], st.copies[key][ci]
        if e.response is not None:
            self._charge(acc, e.response, batch=True)       # billed even when it arrives late
        if key in st.final or c.status != "waiting" or c.attempt != attempt:
            return
        if e.kind == "succeeded":
            kind, value = self._evaluate(e.response, item)
            if kind == "answer":
                c.status, c.answer = "answer", value
                return
            if kind == "failed":
                c.status, c.cause = "failed", value
                return
            acc.violations.append("; ".join(value))
            bucket, cause = "violation", "schema_violation"
        else:
            bucket, cause = e.kind, f"batch_{e.kind}"
        if c.retries[bucket] < 1:                           # 3.13 / 3.15: once
            c.retries[bucket] += 1
            retry.append((key, ci))
        else:
            c.status, c.cause = "failed", cause

    def _resubmit(self, retry: list[tuple[str, int]]) -> None:
        if not retry:
            return
        st = self._batch
        stop = self.ledger.stop_reason()
        if stop is not None:
            for key, ci in retry:
                st.copies[key][ci].status, st.copies[key][ci].cause = "failed", "dollar_cap"
            return
        requests, params_by_key = [], {}
        for key, ci in retry:
            c = st.copies[key][ci]
            c.attempt += 1
            cid = custom_id(key, ci, c.attempt)
            st.entries[cid] = (key, ci, c.attempt)
            if key not in params_by_key:
                params_by_key[key] = self.prompt.params(st.items[key])    # byte-stable: rebuilt, not stored
            requests.append((cid, params_by_key[key]))
        try:
            bid = self.transport.create_batch(requests)
        except TransportError:
            for key, ci in retry:
                st.copies[key][ci].attempt -= 1
            st.unsent.extend(retry)
            return
        st.open.append(bid)
        st.all_ids.append(bid)

    def _try_finalize(self, key: str) -> Verdict | None:
        """The synchronous path's mapping over the copies sent up front (module docstring): copy 0 decides,
        and only a borderline copy 0 consults the extra copies."""
        st = self._batch
        item, acc, copies = st.items[key], st.acc[key], st.copies[key]
        primary = copies[0]
        if primary.status == "waiting":
            return None
        if primary.status == "failed":
            if primary.cause == "dollar_cap":
                return self._capped(item, self.ledger.stop_reason() or "run", queued=True, acc=acc)
            return self._verdict(item, "unavailable", acc, path="batch", cause=primary.cause)
        if item.task != TASK_A or not self._borderline(primary.answer["confidence"]):
            return self._answered(item, primary.answer, acc, path="batch")
        if any(c.status == "waiting" for c in copies):
            return None
        if any(c.cause == "dollar_cap" for c in copies):
            return self._capped(item, self.ledger.stop_reason() or "run", queued=True, acc=acc)
        if any(c.cause == "model_mismatch" for c in copies):
            return self._verdict(item, "unavailable", acc, path="batch", cause="model_mismatch")
        answer, votes = vote([c.answer for c in copies if c.status == "answer"])
        return self._answered(item, answer, acc, path="batch", votes=votes)
