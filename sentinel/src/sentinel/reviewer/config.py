"""Reviewer settings: model pin, request settings, caps, spend ceilings, crop policy (3.12, 3.15, 11.4).

Vendor facts below were read from the claude-api skill and the vendor documentation on 2026-11-10
(3.12 says they are re-read at DR-07); each constant names its source:

- Default model id `claude-opus-5-5`: the Opus-tier id the claude-api skill names as current. The
  Phase 0 security ruling (DR-07) picks the pin; `ReviewerConfig.model_id` is where it goes.
- Current Opus-tier models reject `temperature`, `top_p` and `top_k` with a 400, and API version
  2026-10-21 rejects them on every model (skill, Thinking & Effort table). So the client sends no
  sampling parameter by default; `send_temperature_zero` exists for a pinned model and API version
  that still accept `temperature: 0`, the other branch of 3.12.
- Thinking is always on for `claude-opus-5-5` (adaptive; `{"type": "disabled"}` is a 400), and
  effort defaults to `medium` on it, so effort is pinned explicitly (3.12: initially `high`).
- Prices, USD per million tokens, for `claude-opus-5-5`: input 4, output 20, 1-hour cache write 8,
  cache read 0.20; Message Batches halve every token price and the discounts stack with caching
  (skill model table and migration guide; batch-processing docs).
- Image limits: Claude 4.7 and later are the high-resolution tier (2576 px long edge, 4784 visual
  tokens of 28x28 px); others are standard (1568 px, 1568 tokens). An image over either limit is
  downscaled unless its block sets `transformations.oversized_image: "error"` (vision docs).
- Zero data retention: Message Batches are not ZDR-eligible (29-day result retention); prompt caching
  and structured outputs are ZDR-eligible except for Covered Models; the Fable and Mythos families
  are Covered Models that require 30-day retention (batch-processing, prompt-caching and
  structured-outputs docs; skill error-codes).

Choices where the spec is ambiguous:

- `zero_data_retention` (the documented config switch for the ZDR caveat of 3.12 and 6.10) defaults
  to True, because 3.12 puts the Reviewer under zero-data-retention terms. While it is True the
  Batch path is off (every run uses the synchronous path, the 3.15 fallback for an endpoint without
  a usable Batch API) and a Covered Model pin is refused. Set it False only when the security
  ruling's terms allow 29-day batch retention.
- Nightly runs have no cap in 3.15 (PR 60, merge 200, RC 500); they take the RC cap, which covers
  the 300 nightly canary items of PA-11.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

DEFAULT_MODEL_ID = "claude-opus-5-5"
RUN_KINDS = ("pr", "merge", "rc", "nightly")
DEFAULT_CALL_CAPS = {"pr": 60, "merge": 200, "rc": 500, "nightly": 500}           # 3.15, 11.4
EFFORTS = ("low", "medium", "high", "xhigh", "max")
COVERED_MODEL_PREFIXES = ("claude-fable-", "claude-mythos-")                       # 30-day retention models
IMAGE_TIERS = {"high_resolution": (2576, 4784), "standard": (1568, 1568)}         # (long edge px, visual tokens)


class ConfigError(ValueError):
    """A reviewer setting is invalid or conflicts with the data terms."""


@dataclass(frozen=True)
class Prices:
    """Standard (synchronous) USD prices per million tokens; `batch_factor` applies to all of them."""

    input: float
    output: float
    cache_write: float           # 1-hour TTL writes: the only TTL this client sets (3.12)
    cache_read: float
    batch_factor: float = 0.5


KNOWN_PRICES = {DEFAULT_MODEL_ID: Prices(input=4.0, output=20.0, cache_write=8.0, cache_read=0.20)}


@dataclass(frozen=True)
class CropPolicy:
    """3.12 image policy; its `version` is part of the reviewer version (3.15)."""

    version: str = "crop-1"
    pad_px: int = 24
    max_long_edge_px: int = 1568
    tile_overlap_px: int = 32
    small_text_px: int = 12
    upscale: int = 2


@dataclass(frozen=True)
class ReviewerConfig:
    """Every setting the Reviewer job reads. `request_settings` goes into the prompt hash (3.12)."""

    model_id: str = DEFAULT_MODEL_ID
    effort: str = "high"
    max_tokens: int = 16000
    send_temperature_zero: bool = False
    structured_output: bool = True       # False: an endpoint without output_config.format; client-side checks only
    batch_api: bool = True               # the endpoint serves Message Batches (DR-07)
    zero_data_retention: bool = True     # see the module docstring
    image_tier: str = "high_resolution"
    routing_threshold: float = 0.80      # 3.11 input R until 3.14 derives it
    borderline_band: float = 0.10        # 3.15 borderline rule
    vote_copies: int = 3
    sync_concurrency: int = 4
    consecutive_failure_switch: int = 3  # 10.7: then the rest of the run goes to the Batch path
    call_caps: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_CALL_CAPS))
    run_ceiling_usd: float = 50.0
    weekly_cap_usd: float = 500.0
    deadline_margin_s: int = 600         # tier-1 items go synchronous at the report deadline minus 10 minutes
    give_up_after_s: int = 24 * 3600     # items still pending 24 hours after the deadline are unavailable
    cache_ttl: str = "1h"
    crop: CropPolicy = field(default_factory=CropPolicy)
    prices: Prices | None = None
    endpoint_base_url: str | None = None

    def __post_init__(self) -> None:
        if not self.model_id or self.model_id != self.model_id.strip():
            raise ConfigError("model_id is the exact id string the security ruling approves (3.12)")
        if self.effort not in EFFORTS:
            raise ConfigError(f"effort {self.effort!r} not in {EFFORTS}")
        if self.zero_data_retention and self.model_id.startswith(COVERED_MODEL_PREFIXES):
            raise ConfigError(f"{self.model_id} is a Covered Model (30-day retention); it cannot run under "
                              "zero data retention without a written authorization (3.12, 6.10)")
        if self.image_tier not in IMAGE_TIERS:
            raise ConfigError(f"image_tier {self.image_tier!r} not in {sorted(IMAGE_TIERS)}")
        edge, tokens = IMAGE_TIERS[self.image_tier]
        m = self.crop.max_long_edge_px
        if math.ceil(m / 28) * 28 > edge or math.ceil(m / 28) ** 2 > tokens:
            raise ConfigError(f"a {m}x{m} tile would be downscaled on the {self.image_tier} tier; lower "
                              "crop.max_long_edge_px (a crop policy change, 3.15)")
        if not 0 < self.crop.tile_overlap_px < m:
            raise ConfigError("tile overlap must be positive and below the tile edge")
        if set(self.call_caps) != set(RUN_KINDS) or any(int(v) < 0 for v in self.call_caps.values()):
            raise ConfigError(f"call_caps needs a non-negative cap for each of {RUN_KINDS}")
        if self.vote_copies != 3:
            raise ConfigError("the borderline rule re-sends twice: three copies (3.15)")
        if self.cache_ttl not in ("5m", "1h"):
            raise ConfigError("cache_ttl is 5m or 1h")
        if self.price_table() is None:
            raise ConfigError(f"no prices known for {self.model_id}; set `prices` (11.4 spend ceilings need them)")

    @property
    def batch_enabled(self) -> bool:
        """The Batch path is usable: the endpoint serves it and the data terms allow it."""
        return self.batch_api and not self.zero_data_retention

    def price_table(self) -> Prices | None:
        return self.prices or KNOWN_PRICES.get(self.model_id)

    def call_cap(self, run_kind: str) -> int:
        if run_kind not in RUN_KINDS:
            raise ConfigError(f"run kind {run_kind!r} not in {RUN_KINDS}")
        return int(self.call_caps[run_kind])

    def request_settings(self) -> dict:
        """The exact request settings recorded in the prompt version (3.12, 3.15)."""
        return {"effort": self.effort, "max_tokens": self.max_tokens, "temperature_zero": self.send_temperature_zero,
                "structured_output": self.structured_output, "cache_ttl": self.cache_ttl,
                "image_tier": self.image_tier, "crop": asdict(self.crop)}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ReviewerConfig":
        """Build from a parsed config mapping (the `reviewer:` block); unknown keys are an error."""
        d = dict(data)
        if "crop" in d:
            d["crop"] = CropPolicy(**d["crop"])
        if "prices" in d and d["prices"] is not None:
            d["prices"] = Prices(**d["prices"])
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"unknown reviewer settings {sorted(unknown)}")
        return cls(**d)
