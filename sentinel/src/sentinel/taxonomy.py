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

    # -- generated lists of 4.1 beyond the outcome sets --------------------------------------
    @property
    def quarantine_reasons(self) -> list[str]:
        """10.5: the reasons a quarantine.yaml record may carry (SD-96)."""
        return self.data["quarantine_reasons"]

    @property
    def money_roles(self) -> list[str]:
        """4.8: the closed money-cell list (SD-28)."""
        return self.data["money_roles"]

    @property
    def popup_actions(self) -> list[str]:
        """4.10: the popup catalog actions."""
        return self.data["popup_actions"]

    @property
    def canary_kinds(self) -> list[str]:
        """4.1 and 8.3: the kinds of a canary_results row (SD-81)."""
        return self.data["canary_kinds"]

    @property
    def step_event_actions(self) -> list[str]:
        """8.3: the step_events actions (SD-16)."""
        return self.data["step_event_actions"]

    @property
    def decision_statuses(self) -> list[str]:
        """4.14: decision status."""
        return self.data["decision_statuses"]

    @property
    def decision_sources(self) -> list[str]:
        """4.14: where a decision was taken."""
        return self.data["decision_sources"]

    def reason_meta(self, outcome: str, reason: str) -> dict:
        """8.3 seed metadata of one reason: {owner_section, detail_means}. `reason` may carry its parameter
        (`step_failed:s04`, `preflight.H8`); an inherited reason (1.10) answers with its parent's row."""
        code = reason.split(":", 1)[0]
        if code.startswith("preflight."):
            code = "preflight"
        meta = self.data.get("reason_metadata", {})
        for o in (outcome, self.data.get("reason_inheritance", {}).get(outcome)):
            row = (meta.get(o) or {}).get(code) if o else None
            if row is not None:
                return row
        raise TaxonomyError(f"no 8.3 seed row for {outcome} ({reason})")

    def rule_operands(self, rule: str) -> tuple[frozenset[str], frozenset[str]]:
        """2.8.3: (required, optional) operand names of rule R01 to R10 (SD-34)."""
        try:
            ops = self.data["rule_operands"][rule]
        except KeyError:
            raise TaxonomyError(f"unknown rule {rule!r}") from None
        return (frozenset(k for k, v in ops.items() if v["required"]),
                frozenset(k for k, v in ops.items() if not v["required"]))

    def check_canary(self, kind: str, defect_class: str) -> None:
        """SD-81: a mutant row carries one of the 4.11 classes; every other kind carries its own kind."""
        if kind not in self.canary_kinds:
            raise TaxonomyError(f"unknown canary kind {kind!r}")
        if kind == "mutant" and defect_class not in self.data["mutant_classes"]:
            raise TaxonomyError(f"defect_class {defect_class!r} is not a mutant class of 4.11")
        if kind != "mutant" and defect_class != kind:
            raise TaxonomyError(f"a {kind} row carries defect_class {kind!r}, not {defect_class!r}")

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
# 4.1 sweep hosts: only flexgrid, detached and dialog hosts take w x h steps; tab and sidebar hosts are swept
# through their splitters only (SD-05). A step of Ladder F's ascending pass ends in -asc (SD-04).
SWEEP_HOSTS = ("flexgrid", "detached", "dialog")
CHECKPOINT_ID = re.compile(
    rf"^(?:census/{SLUG}/{SLUG}(?:/[a-z]+=[A-Za-z0-9.-]+(?:,[a-z]+=[A-Za-z0-9.-]+)*)?"
    rf"|sweep/{SLUG}/(?:{'|'.join(SWEEP_HOSTS)})/[0-9]+x[0-9]+(?:-asc)?"
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
    """{kind, surface, qualifier} of a checkpoint id; a sweep id adds its host, w, h and whether it is a step of
    Ladder F's ascending pass (`-asc`, SD-04)."""
    if not is_checkpoint_id(cid):
        raise TaxonomyError(f"bad checkpoint id {cid!r}")
    kind, surface, *rest = cid.split("/")
    parts: dict = {"kind": kind, "surface": surface, "qualifier": "/".join(rest)}
    if kind == "sweep":
        w, h = rest[1].removesuffix("-asc").split("x")
        parts.update(host=rest[0], w=int(w), h=int(h), ascending=rest[1].endswith("-asc"))
    return parts
