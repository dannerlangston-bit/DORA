"""The real AI Reviewer adapter (3.12 to 3.16, 10.7, 11.4).

Modules:

- `config`: `ReviewerConfig`, the model pin (default `claude-opus-5-5`, the security ruling picks the
  real one), request settings, caps, ceilings, crop policy and the zero-data-retention switch.
- `schema`: the 3.13 output schema, its hash and the client-side checks.
- `images`: the 3.12 crop policy (1:1 crops padded 24 px, tiles, 2x copies, deterministic PNG).
- `prompt`: the Reviewer item, the frozen system prompt, few-shots, the byte-stable request and the
  prompt hash; screen text only inside the untrusted data block (3.16 item 8).
- `transport`: the injection point; `AnthropicTransport` is the only network code.
- `budget`: the spend ledger, pricing and the cap ranking.
- `job`: `ReviewerJob` (synchronous and Batch paths, majority vote, failure mapping), `Verdict` with
  the version tuple (model id, prompt hash, schema hash), `visibility_by_entry` for reconciliation.
- `adapter`: `ModelReviewer`, the `pipeline.reviewer.ReviewerAdapter` implementation.
- `deprecation`: deprecation notices and the daily Models API check (alarm input).
- `live`: `review_live`, the only entry point that may call the API.

Nothing here calls the network by default or in tests. Each module's docstring lists the choices it
made where the spec is ambiguous, with the section it reads.
"""
from .adapter import ModelReviewer, ReviewerUnavailable
from .budget import SpendLedger, cost_usd, rank_for_cap
from .config import DEFAULT_MODEL_ID, ConfigError, CropPolicy, Prices, ReviewerConfig
from .deprecation import Alarm, DeprecationNotice, alarms_due, model_check, parse_notices
from .images import crop_box, tile_boxes
from .job import (JobResult, MemoryCache, ReviewerJob, ReviewerVersion, Verdict, VerdictCache, custom_id,
                  visibility_by_entry, vote)
from .live import review_live
from .prompt import (DependencyEdge, DiffMetrics, FewShot, FlowContext, ManifestEntryRef, OracleValue, Prompt,
                     ReviewItem, StepCrops)
from .schema import SCHEMA, SCHEMA_HASH, SCHEMA_VERSION, check_answer
from .transport import (AnthropicTransport, BatchEntry, ModelResponse, NetworkNotAllowed, Transport, TransportError,
                        Usage)

__all__ = [
    "Alarm", "AnthropicTransport", "BatchEntry", "ConfigError", "CropPolicy", "DEFAULT_MODEL_ID", "DependencyEdge",
    "DeprecationNotice", "DiffMetrics", "FewShot", "FlowContext", "JobResult", "ManifestEntryRef", "MemoryCache",
    "ModelResponse", "ModelReviewer", "NetworkNotAllowed", "OracleValue", "Prices", "Prompt", "ReviewItem",
    "ReviewerConfig", "ReviewerJob", "ReviewerUnavailable", "ReviewerVersion", "SCHEMA", "SCHEMA_HASH",
    "SCHEMA_VERSION", "SpendLedger", "StepCrops", "Transport", "TransportError", "Usage", "Verdict", "VerdictCache",
    "alarms_due", "check_answer", "cost_usd", "crop_box", "custom_id", "model_check", "parse_notices",
    "rank_for_cap", "review_live", "tile_boxes", "visibility_by_entry", "vote",
]
