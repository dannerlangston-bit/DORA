"""`ModelReviewer`: the real Reviewer behind the pipeline's `ReviewerAdapter` protocol.

`sentinel.pipeline.reviewer.ReviewerAdapter` asks for `model_id`, `prompt_hash` and
`review(request) -> dict` (a 3.13 answer). The pipeline's `ReviewRequest` carries no pixels, so the
integrator supplies `item_for`, which builds the full 3.12 `ReviewItem` (redacted 1:1 crops from the
artifact store, flow context, Oracle values) for a request. `review` returns only answers that passed
the 3.13 checks, after the one re-send and the borderline vote; every other end raises
`ReviewerUnavailable`, whose `review(threshold)` is the classifier's input R for that item (pending,
unavailable or capped, 3.11 row 23). The pipeline's own re-send on a failed `validate` is then never
reached, so the 3.13 retry happens once, here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from ..classify import ROUTING_THRESHOLD, Review
from .job import ReviewerJob, Verdict
from .prompt import ReviewItem

if TYPE_CHECKING:
    from ..pipeline.reviewer import ReviewRequest


class ReviewerUnavailable(RuntimeError):
    """The item ended without an answer; `verdict` says how (3.11 row 23)."""

    def __init__(self, verdict: Verdict):
        super().__init__(f"{verdict.state}: {verdict.cause}")
        self.verdict = verdict

    def review(self, threshold: float = ROUTING_THRESHOLD) -> Review:
        return self.verdict.to_review(threshold)


class ModelReviewer:
    """Implements `pipeline.reviewer.ReviewerAdapter` over a `ReviewerJob` (synchronous path)."""

    def __init__(self, job: ReviewerJob, item_for: Callable[["ReviewRequest"], ReviewItem]):
        self.job = job
        self.item_for = item_for
        self.model_id = job.model_id
        self.prompt_hash = job.prompt_hash
        self.verdicts: list[Verdict] = []

    def review(self, request: "ReviewRequest") -> dict:
        verdict = self.job.review_now(self.item_for(request))
        self.verdicts.append(verdict)
        if verdict.state != "answered":
            raise ReviewerUnavailable(verdict)
        return dict(verdict.answer)
