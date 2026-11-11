"""The agent-mode tool definitions of 5.6 and the toolset schema version (5.6, 6.11, 10.7).

The agent is given exactly these tools as ordinary tool definitions (name, description, JSON input schema) with image
inputs, never the computer-use beta tool, so the model never receives a coordinate action or a free-text typing
action. Two items are pinned: the model id, and the toolset schema version, the sha256 of the canonical JSON of the
definitions below; both are stamped on every agent step and checked by preflight N2 (10.7).

| Tool | Arguments | Profiles |
|---|---|---|
| `click_named` | `target` (a catalog name), `window?` | agent, explorer |
| `type_fixture` | `fixture_id` (an id from the fixture's `typed_values`) | agent, explorer |
| `press` | `key`, one of `PRESS_KEYS` | agent, explorer |
| `scroll` | `target` (a catalog name), `notches` (signed) | agent, explorer |
| `drag_named` | `from`, `to` (catalog names: splitters or detach handles) | explorer only |
| `zoom` | `region` {x, y, w, h} in screenshot coordinates | agent, explorer |

Choices where 5.6 is silent (reported):
1. The schema version hashes all six definitions, whatever the profile, so the ruling pins one value
   (`policy/computer_use_ruling.yaml` `toolset_schema_version`); profile `agent` is offered the list without
   `drag_named`.
2. Catalog names and fixture ids are not enumerated in the schemas (they change with every screen and fixture,
   and the hash must not); the dispatcher checks them at call time and its tool error names the allowed values.
3. `scroll.notches` is a non-zero integer within +-20 (L-FLW-09 refuses a zero wheel; 20 notches bound one call).
4. `zoom.region` lies inside the 1280x720 model screenshot, with positive width and height.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from ..canonical import sha256_id

PROFILES = ("agent", "explorer")
PRESS_KEYS = ("tab", "shift_tab", "escape", "page_up", "page_down", "up", "down", "left", "right", "home", "end")
MAX_NOTCHES = 20                                   # choice 3
SCREEN_W, SCREEN_H = 1280, 720                     # the model screenshot (5.6: 1920x1080 downscaled by 1.5)

_NAME = {"type": "string", "minLength": 1, "maxLength": 200}

TOOLS: tuple[Mapping[str, Any], ...] = (
    {"name": "click_named",
     "description": "Click the control with this name from the name catalog (the registry's control ids for the "
                    "current surface, the flow's declared targets and the OCR words on screen). Never a coordinate.",
     "input_schema": {"type": "object", "properties": {"target": _NAME, "window": _NAME},
                      "required": ["target"], "additionalProperties": False}},
    {"name": "type_fixture",
     "description": "Type the fixture value with this id (symbols, quantities, prices, watchlist names). You never "
                    "see or supply the text.",
     "input_schema": {"type": "object", "properties": {"fixture_id": _NAME},
                      "required": ["fixture_id"], "additionalProperties": False}},
    {"name": "press",
     "description": "Press one key from the fixed list. Enter, Space, chords and function keys are not offered.",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string", "enum": list(PRESS_KEYS)}},
                      "required": ["key"], "additionalProperties": False}},
    {"name": "scroll",
     "description": "Turn the mouse wheel by a signed number of notches over the named control.",
     "input_schema": {"type": "object",
                      "properties": {"target": _NAME,
                                     "notches": {"type": "integer", "minimum": -MAX_NOTCHES, "maximum": MAX_NOTCHES,
                                                 "not": {"const": 0}}},
                      "required": ["target", "notches"], "additionalProperties": False}},
    {"name": "drag_named",
     "description": "Drag from one named splitter or detach handle to another. Order bubbles and ladder cells are "
                    "refused.",
     "input_schema": {"type": "object", "properties": {"from": _NAME, "to": _NAME},
                      "required": ["from", "to"], "additionalProperties": False}},
    {"name": "zoom",
     "description": "Read-only: return a sharper crop of this rectangle of the screenshot (screenshot coordinates).",
     "input_schema": {"type": "object",
                      "properties": {"region": {
                          "type": "object",
                          "properties": {"x": {"type": "integer", "minimum": 0, "maximum": SCREEN_W - 1},
                                         "y": {"type": "integer", "minimum": 0, "maximum": SCREEN_H - 1},
                                         "w": {"type": "integer", "minimum": 1, "maximum": SCREEN_W},
                                         "h": {"type": "integer", "minimum": 1, "maximum": SCREEN_H}},
                          "required": ["x", "y", "w", "h"], "additionalProperties": False}},
                      "required": ["region"], "additionalProperties": False}},
)
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
EXPLORER_ONLY = frozenset({"drag_named"})


def toolset(profile: str) -> list[dict[str, Any]]:
    """The tool list a session of `profile` is given (5.6): every tool for `explorer`, all but `drag_named` for
    `agent`. Deep copies, so a caller cannot change the hashed definitions."""
    if profile not in PROFILES:
        raise ValueError(f"profile {profile!r}; one of {PROFILES}")
    return [copy.deepcopy(dict(t)) for t in TOOLS if profile == "explorer" or t["name"] not in EXPLORER_ONLY]


def tool_names(profile: str) -> tuple[str, ...]:
    """The tool names `profile` may call."""
    return tuple(t["name"] for t in toolset(profile))


def schema_of(name: str) -> Mapping[str, Any]:
    """The JSON input schema of one tool; KeyError for a name 5.6 does not define."""
    for t in TOOLS:
        if t["name"] == name:
            return t["input_schema"]
    raise KeyError(name)


def toolset_schema_version(tools: Sequence[Mapping[str, Any]] = TOOLS) -> str:
    """`sha256:<hex>` of the canonical JSON of the tool definitions (5.6, choice 1): the version the ruling pins and
    preflight N2 checks; any change to any definition changes it."""
    return sha256_id([dict(t) for t in tools])


TOOLSET_SCHEMA_VERSION = toolset_schema_version()
