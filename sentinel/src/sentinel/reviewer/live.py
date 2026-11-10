"""`review_live`: the function behind a `sentinel review --live` command (the coordinator wires the CLI).

It is the only place in this package that builds a live transport, and it does so only when the
caller passes `allow_network=True` and `ANTHROPIC_API_KEY` is set (`NetworkNotAllowed` otherwise,
before any client exists). It reviews the given items on the synchronous path, as a PR run does,
under the same caps, ceilings and checks as the job, and returns the stamped verdicts.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from .budget import SpendLedger
from .config import ReviewerConfig
from .job import ReviewerJob, Verdict
from .prompt import FewShot, ReviewItem
from .transport import AnthropicTransport


def review_live(items: Sequence[ReviewItem], *, allow_network: bool = False, config: ReviewerConfig | None = None,
                few_shots: Sequence[FewShot] = (), env: Mapping[str, str] | None = None,
                ledger: SpendLedger | None = None) -> dict[str, Verdict]:
    """Review `items` against the real API; refuses unless `allow_network` and an API key are present."""
    cfg = config or ReviewerConfig()
    transport = AnthropicTransport.from_env(allow_network=allow_network, env=env, base_url=cfg.endpoint_base_url)
    return ReviewerJob(cfg, transport, "pr", few_shots=few_shots, ledger=ledger).review(items).verdicts
