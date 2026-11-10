"""The Reviewer of 3.12 and 3.13 for the simulated run: a SIMULATED, offline, deterministic stand-in.

`SimulatedReviewer` is not a model. It never sees pixels and never calls a network. It reads the
same inputs a Reviewer item carries (3.12: item header, manifest entry or dependency edge, diff
metrics) plus the simulated build's change notes (`sim.Observation`), which stand in for what a
vision model would read off the base and candidate crops, and it answers in the exact 3.13 output
shape. Its rules, all deterministic:

- task A `manifest_match`: `no_visible_difference` (0.90) when nothing changed; `matches_manifest`
  (0.93) when every observed change belongs to a change that implements a ticket of the entry;
  `partially_matches_manifest` (0.62) when some do; `does_not_match_manifest` (0.88) when none do.
- task B `describe_collateral`: the verdict is fixed to `does_not_match_manifest` (3.12 item 3);
  it returns a description and the closest defect class. The description cannot move an outcome.
- task C `manifest_visibility`: `visible` when an observed change implements the entry, else
  `not_visible`; verdict `not_applicable`.

Every answer carries `model_id` `simulated-reviewer-offline` and prompt hash `sim-1`, so the PR
comment and report show that a simulated reviewer answered.

`ReviewerAdapter` is the interface a real model client implements; `ModelReviewerAdapter` builds
the 3.12 request payload and hands it to a caller-supplied `send` function. Nothing here imports an
SDK or opens a connection: without a `send` function it refuses to run. `validate` applies the
client-side checks of 3.13 to any answer, simulated or real.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..classify import Review
from ..taxonomy import load
from .sim import Observation

SIMULATED_MODEL_ID = "simulated-reviewer-offline"
SIMULATED_PROMPT_HASH = "sim-1"
SCHEMA_VERSION = "reviewer-1"
TASKS = ("manifest_match", "describe_collateral", "manifest_visibility")
VERDICTS = tuple(load().list("reviewer_verdicts")) + ("not_applicable",)
CHANGE_TYPES = ("overlap", "missing_element", "added_element", "clipped_text", "shifted", "resized", "text_change",
                "color_change", "color_semantic_flip", "other")
# 3.12 task B: "the closest defect class"; a defect that hides or breaks content outranks a move.
CLOSEST_ORDER = ("overlap", "missing_element", "clipped_text", "color_semantic_flip", "shifted", "resized",
                 "text_change", "added_element", "color_change", "other")


@dataclass(frozen=True)
class ReviewRequest:
    """One Reviewer item (3.12 inputs). For a sweep cluster `sizes` names its smallest and largest step."""

    task: str
    checkpoint_ids: tuple[str, ...]
    surface_id: str
    region_id: str | None
    tier: int
    data_mode: str
    workspace_id: str
    diff: Mapping[str, Any]
    observations: tuple[Observation, ...]
    crop: tuple[int, int, int, int]
    entry: Mapping[str, Any] | None = None       # task A and C: the manifest entry
    edge: Mapping[str, Any] | None = None        # task B: {primitive, entry_id}
    sizes: tuple[str, ...] = ()
    ladder: bool = False                         # the crop shows an Active Trader ladder

    def __post_init__(self) -> None:
        if self.task not in TASKS:
            raise ValueError(f"task {self.task!r}")

    def payload(self) -> dict:
        """The canonical request (3.12 text blocks; images are referenced by the crop bounds)."""
        d = asdict(self)
        d["observations"] = [asdict(o) | {"tickets": sorted(o.tickets)} for o in self.observations]
        return d

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True, default=str).encode()).hexdigest()


class ReviewerAdapter(Protocol):
    """What the run calls for a verdict: one structured answer per item (3.13)."""

    model_id: str
    prompt_hash: str

    def review(self, request: ReviewRequest) -> dict: ...


def _closest(types: Sequence[str]) -> str | None:
    return min(types, key=CLOSEST_ORDER.index) if types else None


def _pointer(box: tuple[int, int, int, int], crop: tuple[int, int, int, int]) -> dict:
    x, y, w, h = box
    cx, cy, cw, ch = crop
    x0, y0 = min(max(x - cx, 0), max(cw - 1, 0)), min(max(y - cy, 0), max(ch - 1, 0))
    return {"image": "candidate", "x": x0, "y": y0, "w": max(1, min(w, cw - x0)), "h": max(1, min(h, ch - y0))}


@dataclass
class SimulatedReviewer:
    """SIMULATED REVIEWER: an offline, deterministic stand-in for the model (see the module docstring)."""

    model_id: str = SIMULATED_MODEL_ID
    prompt_hash: str = SIMULATED_PROMPT_HASH
    calls: list[tuple[ReviewRequest, dict]] = field(default_factory=list)

    def review(self, request: ReviewRequest) -> dict:
        from .sim import describe_observations
        obs = list(request.observations)
        tickets = set((request.entry or {}).get("tickets", []))
        explained = [bool(o.tickets & tickets) for o in obs]
        changed = bool(obs) or int(request.diff.get("changed_px", 0)) > 0 or any(request.diff.get("shift", (0, 0)))
        visibility, matched = "not_applicable", None
        if request.task == "describe_collateral":
            verdict, conf = "does_not_match_manifest", 0.90
        elif request.task == "manifest_visibility":
            verdict, conf = "not_applicable", 0.90
            visibility = "visible" if any(explained) else "not_visible"
        elif not changed:
            verdict, conf = "no_visible_difference", 0.90
        elif obs and all(explained):
            verdict, conf = "matches_manifest", 0.93
        elif any(explained):
            verdict, conf = "partially_matches_manifest", 0.62
        else:
            verdict, conf = "does_not_match_manifest", 0.88
        if verdict in ("matches_manifest", "partially_matches_manifest") and request.entry:
            matched = request.entry.get("entry_id")
        answer = {
            "schema_version": SCHEMA_VERSION, "task": request.task, "verdict": verdict, "confidence": conf,
            "matched_manifest_entry_id": matched,
            "observed_changes": [{"change_type": o.change_type, "pointer": _pointer(o.box, request.crop),
                                  "explained_by_manifest": e, "text": o.text} for o, e in zip(obs, explained)],
            "description": describe_observations(obs, request.ladder),
            "visibility": visibility, "uncertainty_reasons": ["none"],
        }
        self.calls.append((request, answer))
        return answer


@dataclass
class ModelReviewerAdapter:
    """Adapter for a real model behind the security-approved endpoint (3.12). The caller supplies
    `send(payload) -> dict` (the structured output); this class only builds the payload and checks
    the answer. No default transport exists, so nothing is ever sent unless a caller wires one."""

    model_id: str
    prompt_hash: str
    send: Callable[[dict], dict] | None = None

    def review(self, request: ReviewRequest) -> dict:
        if self.send is None:
            raise RuntimeError("ModelReviewerAdapter has no transport; the simulated run never calls a model")
        answer = self.send({"model": self.model_id, "prompt_hash": self.prompt_hash, "item": request.payload()})
        problems = validate(answer, request)
        if problems:
            raise ValueError("reviewer_schema_violation: " + "; ".join(problems))
        return answer


def validate(answer: Mapping[str, Any], request: ReviewRequest) -> list[str]:
    """The client-side checks of 3.13; an empty list means the answer is usable."""
    out = []
    keys = {"schema_version", "task", "verdict", "confidence", "matched_manifest_entry_id", "observed_changes",
            "description", "visibility", "uncertainty_reasons"}
    if set(answer) != keys:
        out.append(f"keys {sorted(set(answer) ^ keys)}")
        return out
    if answer["schema_version"] != SCHEMA_VERSION or answer["task"] != request.task:
        out.append("schema_version or task")
    if answer["verdict"] not in VERDICTS:
        out.append(f"verdict {answer['verdict']!r}")
    c = answer["confidence"]
    if not isinstance(c, (int, float)) or not 0.0 <= float(c) <= 1.0:
        out.append("confidence outside [0, 1]")
    task_c = request.task == "manifest_visibility"
    if (answer["verdict"] == "not_applicable") != task_c or (answer["visibility"] != "not_applicable") != task_c:
        out.append("not_applicable verdict and visibility belong to task C only")
    m = answer["matched_manifest_entry_id"]
    if m is not None and (answer["verdict"] not in ("matches_manifest", "partially_matches_manifest")
                          or m != (request.entry or {}).get("entry_id")):
        out.append("matched_manifest_entry_id only with a matching verdict, equal to the entry supplied")
    _, _, cw, ch = request.crop
    for oc in answer["observed_changes"]:
        p = oc["pointer"]
        if oc["change_type"] not in CHANGE_TYPES:
            out.append(f"change_type {oc['change_type']!r}")
        if not (0 <= p["x"] and 0 <= p["y"] and p["x"] + p["w"] <= cw and p["y"] + p["h"] <= ch):
            out.append("pointer outside the crop")
    return out


def to_review(answer: Mapping[str, Any], adapter: ReviewerAdapter, region_id: str | None) -> Review:
    """The classifier's input R from a validated answer (3.11)."""
    types = [oc["change_type"] for oc in answer["observed_changes"]]
    return Review("answered", verdict=answer["verdict"], confidence=float(answer["confidence"]),
                  model_id=adapter.model_id, prompt_hash=adapter.prompt_hash, description=answer["description"],
                  region_pointer=region_id, defect_class=_closest(types))


def change_types(answer: Mapping[str, Any]) -> list[str]:
    """The closest defect class as the report's `evidence.reviewer.change_types` (9.2)."""
    c = _closest([oc["change_type"] for oc in answer["observed_changes"]])
    return [c] if c else []
