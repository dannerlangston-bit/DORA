"""The agent-mode toolset of 5.6: the pure parts, kept off under DR-07 Branch B (6.11).

- `tools`: the six tool definitions (`click_named`, `type_fixture`, `press`, `scroll`, `drag_named`, `zoom`) with
  their JSON schemas, the per-profile tool lists and the toolset schema version (their sha256).
- `dispatcher`: validates every tool call against those schemas and the name catalog, turns a valid call into a
  gateway `Action` (profile `agent` or `explorer`), counts refusals and checks each model response against the pin.
- `session`: the tool loop over a model client: the agent rescue of 5.5 rung 4 (25 actions, 90 s) and the
  Explorer session's loop (budgets through `orchestrator.explorer.ExplorerSession`), with a step event per action.
- `zoom`: the 1.5 screenshot scale, the zoom crop from the redacted native capture and the scale check at
  `canary_flow_passed`.

Nothing here calls a model or the network: the model client, the gateway handler and the screenshots are ports the
Executor supplies. `agent_enabled(branches)` is the switch: DR-07 Branch A only (an undecided branch counts as B,
as `executor.locator` reads it).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def agent_enabled(branches: Mapping[str, Any]) -> bool:
    """True only when `phase0/branches.yaml` records DR-07 (`computer_use`) on Branch A (6.11, 5.6)."""
    v = branches.get("computer_use")
    branch = v.get("branch") if isinstance(v, Mapping) else v
    return branch == "A"
