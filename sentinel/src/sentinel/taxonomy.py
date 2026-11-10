"""The closed vocabularies of the spec (4.1, 4.13), loaded from config/taxonomy.yaml.

Every module that writes an outcome, a reason, a finding kind or a decision validates it here,
so a value outside the closed set can never be produced (guarantee G2, 1.4).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"


class TaxonomyError(ValueError):
    """A value is outside the closed vocabulary."""


# 12.7 outcomes by group: only H items abort the run (`aborted_harness (reason: preflight.<item>)`, unreached
# checkpoints `harness_error` with the same reason); R, B, G2-G4, N1, N3, N4 and N6 give `env_error (gate:
# preflight.<item>)`; N2 and N5 are a degraded mode, N7 lets the run proceed, G1 is `config_invalid` and G5 is
# `not_comparable`, so these five are never a preflight gate.
PREFLIGHT_NEVER_ENV = frozenset({"N2", "N5", "N7", "G1", "G5"})
# 4.13 assertion_failed: the parameter of `assertion:<id>` is one assertion id (the id grammar of the flow schema);
# the app_alive cause goes in detail, never in the reason.
ASSERTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Taxonomy:
    data: dict

    @property
    def checkpoint_outcomes(self) -> list[str]:
        return self.data["checkpoint_outcomes"]

    @property
    def step_outcomes(self) -> list[str]:
        return self.data["step_outcomes"]

    @property
    def run_outcomes(self) -> list[str]:
        return self.data["run_outcomes"]

    @property
    def finding_kinds(self) -> list[str]:
        return self.data["finding_kinds"]

    @property
    def gates(self) -> list[str]:
        return self.data["gates"]

    def list(self, key: str) -> list[str]:
        return self.data[key]

    # -- reasons ---------------------------------------------------------------------------
    def _patterns(self, outcome: str) -> list[str]:
        pats = list(self.data["reasons"].get(outcome, []))
        parent = self.data.get("reason_inheritance", {}).get(outcome)
        if parent:
            pats += self.data["reasons"].get(parent, [])
        return pats

    def reason_allowed(self, outcome: str, reason: str | None) -> bool:
        """True when `reason` is a legal reason for `outcome` (parameters after a colon allowed)."""
        if outcome not in self.data["reasons"] and outcome not in self.checkpoint_outcomes + self.run_outcomes:
            return False
        if reason is None or reason == "":
            # outcomes whose table says "none" is allowed, and run `complete`
            return outcome in {"identical", "intended", "collateral", "complete"} or not self._patterns(outcome)
        code, _, param = reason.partition(":")
        for pat in self._patterns(outcome):
            pcode, _, pparam = pat.partition(":")
            if pcode == "preflight.<item>" and code.startswith("preflight."):
                item = code[len("preflight."):]
                return item in self._preflight_items() and item.startswith("H")
            if pcode == "assertion" and code == "assertion":
                return bool(ASSERTION_ID.match(param)) and not param.startswith("app_alive.")
            if pcode == code:
                if pparam:
                    return bool(param)
                return not param or outcome == "invariant_violation"
        return False

    def _preflight_items(self) -> set[str]:
        return {g.split(".", 1)[1] for g in self.gates if g.startswith("preflight.")}

    def check_checkpoint(self, outcome: str, reason: str | None = None, gate: str | None = None) -> None:
        if outcome not in self.checkpoint_outcomes:
            raise TaxonomyError(f"unknown checkpoint outcome {outcome!r}")
        if outcome == "env_error":
            if not gate:
                raise TaxonomyError("env_error requires a gate (F65)")
            if gate not in self.gates:
                raise TaxonomyError(f"unknown gate {gate!r}")
            item = gate[len("preflight."):] if gate.startswith("preflight.") else None
            if item is not None and (item.startswith("H") or item in PREFLIGHT_NEVER_ENV):
                raise TaxonomyError(f"preflight item {item} never gives env_error (12.7)")
        if not self.reason_allowed(outcome, reason):
            raise TaxonomyError(f"reason {reason!r} is not allowed with {outcome!r}")

    def check_run(self, outcome: str, reason: str | None = None) -> None:
        if outcome not in self.run_outcomes:
            raise TaxonomyError(f"unknown run outcome {outcome!r}")
        if outcome.startswith("aborted_") and not reason:
            raise TaxonomyError(f"{outcome} requires a reason")
        if reason and not self.reason_allowed(outcome, reason):
            raise TaxonomyError(f"reason {reason!r} is not allowed with run outcome {outcome!r}")

    def check_step(self, outcome: str) -> None:
        if outcome not in self.step_outcomes:
            raise TaxonomyError(f"unknown step outcome {outcome!r}")

    def check_member(self, key: str, value: str) -> None:
        if value not in self.data[key]:
            raise TaxonomyError(f"{value!r} is not in {key}")


@lru_cache(maxsize=4)
def load(path: str | os.PathLike | None = None) -> Taxonomy:
    p = Path(path) if path else Path(os.environ.get("SENTINEL_TAXONOMY", DEFAULT_PATH))
    return Taxonomy(yaml.safe_load(p.read_text(encoding="utf-8")))


SLUG = r"[a-z0-9]+(?:[.-][a-z0-9]+)*"
CHECKPOINT_ID = re.compile(
    rf"^(?:census/{SLUG}/{SLUG}(?:/[a-z]+=[A-Za-z0-9.-]+(?:,[a-z]+=[A-Za-z0-9.-]+)*)?"
    rf"|sweep/{SLUG}/(?:tab|sidebar|flexgrid|detached|dialog)/[0-9]+x[0-9]+"
    rf"|split/{SLUG}/{SLUG}/(?:0|1)\.[0-9]{{2}}"
    rf"|flow/{SLUG}/{SLUG}/[a-z][a-z0-9-]*)$"
)
SLUG_RE = re.compile(rf"^{SLUG}$")


def is_slug(s: str) -> bool:
    return bool(SLUG_RE.match(s))


def is_checkpoint_id(s: str) -> bool:
    """The checkpoint id grammar of 4.1: <kind>/<surface-id>/<qualifier>."""
    return bool(CHECKPOINT_ID.match(s))


def checkpoint_parts(cid: str) -> dict:
    if not is_checkpoint_id(cid):
        raise TaxonomyError(f"bad checkpoint id {cid!r}")
    kind, surface, *rest = cid.split("/")
    return {"kind": kind, "surface": surface, "qualifier": "/".join(rest)}
