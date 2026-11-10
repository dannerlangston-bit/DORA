"""The 3.13 structured output schema and its client-side checks.

`SCHEMA` is the 3.13 schema verbatim. It is sent as `output_config.format` (type `json_schema`) and
also printed in the system prompt, so an endpoint without structured output gets the same contract
and the same client-side validation (3.15). The vendor's structured-output docs (read 2026-11-10)
confirm what 3.13 assumes: every object needs `additionalProperties: false`, `const` and `enum`
are supported, numeric range constraints are not (so `confidence` in [0, 1] is checked here), and
conformance does not hold on `stop_reason: refusal` or `max_tokens`.

`check_answer` returns every problem it finds; an empty list means the answer is usable. Choices
where the spec is ambiguous (3.12, 3.13):

- Task B's verdict is "fixed to `does_not_match_manifest` by the orchestrator": the prompt states it
  and the check requires it, so a model that disagrees is a schema violation, not a silent rewrite.
- "Pointers lie inside the crop": a pointer names `base` or `candidate` and is inside that image's
  1:1 crop (never a tile or a 2x copy); with two size steps it may lie inside either step's crop. A
  pointer needs a positive width and height.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "reviewer-1"

SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "task", "verdict", "confidence", "matched_manifest_entry_id",
                 "observed_changes", "description", "visibility", "uncertainty_reasons"],
    "properties": {
        "schema_version": {"type": "string", "const": SCHEMA_VERSION},
        "task": {"type": "string", "enum": ["manifest_match", "describe_collateral", "manifest_visibility"]},
        "verdict": {"type": "string", "enum": ["matches_manifest", "partially_matches_manifest",
                                               "does_not_match_manifest", "no_visible_difference", "cannot_judge",
                                               "not_applicable"]},
        "confidence": {"type": "number"},
        "matched_manifest_entry_id": {"type": ["string", "null"]},
        "observed_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["change_type", "pointer", "explained_by_manifest", "text"],
                "properties": {
                    "change_type": {"type": "string", "enum": ["overlap", "missing_element", "added_element",
                                                               "clipped_text", "shifted", "resized", "text_change",
                                                               "color_change", "color_semantic_flip", "other"]},
                    "pointer": {
                        "type": "object", "additionalProperties": False,
                        "required": ["image", "x", "y", "w", "h"],
                        "properties": {"image": {"type": "string", "enum": ["base", "candidate"]},
                                       "x": {"type": "integer"}, "y": {"type": "integer"},
                                       "w": {"type": "integer"}, "h": {"type": "integer"}},
                    },
                    "explained_by_manifest": {"type": "boolean"},
                    "text": {"type": "string"},
                },
            },
        },
        "description": {"type": "string"},
        "visibility": {"type": "string", "enum": ["visible", "not_visible", "unsure", "not_applicable"]},
        "uncertainty_reasons": {"type": "array", "items": {"type": "string", "enum": [
            "crop_cut", "occluded", "low_contrast", "ambiguous_manifest", "no_manifest_text", "text_too_small",
            "none"]}},
    },
}

TASKS: tuple[str, ...] = tuple(SCHEMA["properties"]["task"]["enum"])
VERDICTS: tuple[str, ...] = tuple(SCHEMA["properties"]["verdict"]["enum"])
CHANGE_TYPES: tuple[str, ...] = tuple(SCHEMA["properties"]["observed_changes"]["items"]["properties"]
                                      ["change_type"]["enum"])
VISIBILITY: tuple[str, ...] = tuple(SCHEMA["properties"]["visibility"]["enum"])
MATCHING_VERDICTS = frozenset({"matches_manifest", "partially_matches_manifest"})
TASK_A, TASK_B, TASK_C = TASKS


def canonical_json(obj: Any) -> str:
    """The one serialization used for hashing and for every JSON byte this package sends."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


SCHEMA_JSON = canonical_json(SCHEMA)
SCHEMA_HASH = hashlib.sha256(SCHEMA_JSON.encode()).hexdigest()
_VALIDATOR = Draft202012Validator(SCHEMA)


def check_answer(answer: Any, *, task: str, entry_id: str | None,
                 crop_sizes: Mapping[str, Sequence[tuple[int, int]]]) -> list[str]:
    """The schema plus the client-side checks of 3.13 for one answer to one item.

    `crop_sizes` maps `base` and `candidate` to the (width, height) of each step's 1:1 crop.
    """
    errors = sorted(_VALIDATOR.iter_errors(answer), key=lambda e: list(e.absolute_path))
    if errors:
        return [f"schema: {'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors]
    out: list[str] = []
    c = answer["confidence"]
    if isinstance(c, bool) or not math.isfinite(float(c)) or not 0.0 <= float(c) <= 1.0:
        out.append("confidence outside [0, 1]")
    if answer["task"] != task:
        out.append(f"task {answer['task']!r} answers a {task!r} item")
    task_c = task == TASK_C
    if (answer["verdict"] == "not_applicable") != task_c:
        out.append("verdict not_applicable belongs to task C only")
    if (answer["visibility"] != "not_applicable") != task_c:
        out.append("visibility other than not_applicable belongs to task C only")
    if task == TASK_B and answer["verdict"] != "does_not_match_manifest":
        out.append("task B verdict is fixed to does_not_match_manifest")
    m = answer["matched_manifest_entry_id"]
    if m is not None and (answer["verdict"] not in MATCHING_VERDICTS or m != entry_id):
        out.append("matched_manifest_entry_id only with a matching verdict, equal to the entry supplied")
    for i, oc in enumerate(answer["observed_changes"]):
        p = oc["pointer"]
        inside = any(0 <= p["x"] and 0 <= p["y"] and p["w"] >= 1 and p["h"] >= 1
                     and p["x"] + p["w"] <= w and p["y"] + p["h"] <= h for w, h in crop_sizes.get(p["image"], ()))
        if not inside:
            out.append(f"observed_changes/{i}: pointer outside the {p['image']} crop")
    return out


def parse_answer(text: str) -> tuple[Any, str | None]:
    """The response text as JSON, or the reason it is not."""
    try:
        return json.loads(text), None
    except (TypeError, ValueError) as e:
        return None, f"not JSON: {e}"
