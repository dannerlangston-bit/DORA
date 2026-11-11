"""The Oracle checks of census and sweep checkpoints (2.1, 2.8.1, 3.8, SD-29).

Census and sweep checkpoints carry no flow assertions, so their Oracle checks are exactly the `rule` regions of
their regions file: "census and sweep checkpoints run every `rule` region of their regions file" (3.8, 2.8.1).
`checks_for(checkpoint, regions_file)` names them for one checkpoint; the run evaluates them on each side's
capture with `region_rules.RegionRuleChecker.check`, whose results fold into the checkpoint's facts through
`summarize(results).facts_fields()` exactly as a flow checkpoint's assertions do (3.11 H). A census money cell is a
`rule` region (L-RGN-04 census half, 4.8): `CensusChecks.unchecked_money_cells` lists any money cell that is not,
which the lint refuses and which would get no Oracle check at run time.

Choices where the spec is ambiguous (reported):

- Splitter-step checkpoints (`split/...`) are sweep checkpoints: 2.1 sweeps produce "one checkpoint per (surface,
  host, size or splitter step)", and they carry no assertions either.
- A census or sweep checkpoint without a regions file runs no Oracle check (empty `rules`); one with a flow
  checkpoint id is refused, since a flow checkpoint's Oracle checks are its flow assertions plus its rule regions,
  which the flow path evaluates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..taxonomy import checkpoint_parts
from .region_rules import RegionRule, region_entries, rule_regions
from .tables import OracleConfigError

#: The checkpoint kinds (4.1 grammar) whose Oracle checks are their rule regions only.
CENSUS_KINDS: tuple[str, ...] = ("census", "sweep", "split")


@dataclass(frozen=True)
class CensusChecks:
    """The Oracle checks of one census or sweep checkpoint: every rule region of its regions file, in file order,
    and the money cells that are not rule regions (L-RGN-04 census half)."""

    checkpoint_id: str
    kind: str
    rules: tuple[RegionRule, ...]
    unchecked_money_cells: tuple[str, ...] = ()

    @property
    def assertion_ids(self) -> tuple[str, ...]:
        """Every assertion id the checks produce (`rule.<region_id>.<field>`), in evaluation order."""
        return tuple(r.assertion_id(c) for r in self.rules for c in r.checks)


def checks_for(checkpoint: str, regions_file: Any) -> CensusChecks:
    """The Oracle checks of a census, sweep or splitter-step checkpoint (`regions_file`: a
    `diff.regions.RegionsFile`, the 4.8 mapping, or None when the checkpoint has none). Raises
    `OracleConfigError` for a flow checkpoint or a rule the Oracle cannot evaluate."""
    kind = checkpoint_parts(checkpoint)["kind"]
    if kind not in CENSUS_KINDS:
        raise OracleConfigError(f"{checkpoint}: a {kind} checkpoint's Oracle checks are its flow assertions and "
                                "rule regions, evaluated on the flow path, not census checks (2.8.1)")
    money = tuple(e.id for e in region_entries(regions_file, checkpoint) if e.money_cell and e.type != "rule")
    return CensusChecks(checkpoint, kind, rule_regions(regions_file, checkpoint), money)
