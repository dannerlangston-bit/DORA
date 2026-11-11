"""The agent tool dispatcher (5.6 Requests outside the vocabulary, 5.4.9, 6.11).

Every tool call the model makes is validated before anything reaches the gateway:

1. The tool name must be one the session's profile was given (`tools.tool_names`): `drag_named` exists under
   profile `explorer` only. Anything else is `unknown_tool`, naming the allowed tools.
2. The arguments must satisfy the tool's JSON schema (`tools.schema_of`): a key outside the `press` list is
   `key_not_allowed` naming the keys; any other violation is `malformed`, naming the tool's arguments.
3. Every name must be in the session's name catalog (the registry's control ids for the current surface, the flow's
   declared targets and the OCR words on screen): `unknown_target`, naming the catalog. A name that several catalog
   entries share returns `ambiguous` with the candidates by their qualified names, never by position.
4. A fixture id must be one of the fixture's `typed_values`: `unknown_fixture`, naming the ids (never the values).

Each of those tool errors is logged with the full request (`request_text`) and counts as one refusal; a refusal ends
an agent rescue at once, and the third ends an Explorer session, both with `agent_failed(vocabulary)`
(`REFUSAL_LIMITS`, SD-53). A valid call becomes a `ToolRequest`, which `ToolRequest.action` turns into the gateway
`Action` (profile `agent` or `explorer`) once the handler has resolved the names on rungs 1 to 3; the gateway's slow
half then applies the never-tokenize list, the deny profile and the flag (5.4.3 stage 4). `check_response` ends a
session with `agent_failed(refusal)` on `stop_reason` `refusal` or `max_tokens` or a model id that differs from the
pin. Agent mode never runs in flows of class `login`, `fixture_reset`, `teardown`, `canary` or `paper_send_ok`
(`AgentRefused`).

Choices (reported):
1. `ambiguous` is a tool error that does not count as a refusal: the name is in the catalog, and 5.6 only says the
   candidates are returned by name.
2. A qualified name (a registry control ref `<surface>.<control>`) always resolves to its one entry, so the model can
   answer an `ambiguous` error with one of the candidates it was given.
3. `press` keys map to virtual keys as the gateway's `AGENT_KEYS` hold them; `shift_tab` is a `key_chord` of Shift
   and Tab, the one chord the slow half admits for the agent profiles.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from ..canonical import canonical_json
from ..orchestrator.gateway import (VK_DOWN, VK_END, VK_ESCAPE, VK_HOME, VK_LEFT, VK_NEXT, VK_PRIOR, VK_RIGHT, VK_TAB,
                                    VK_UP, Action)
from . import tools
from .zoom import ZoomRect

REFUSAL_LIMITS = {"agent": 1, "explorer": 3}         # 5.6: a refusal ends a rescue, the third ends an Explorer session
EXCLUDED_CLASSES = frozenset({"login", "fixture_reset", "teardown", "canary", "paper_send_ok"})   # 5.6, 6.11, 4.7
REFUSAL_STOP_REASONS = frozenset({"refusal", "max_tokens"})
TOOL_ERRORS = ("unknown_tool", "malformed", "key_not_allowed", "unknown_target", "unknown_fixture", "ambiguous",
               "blocked_by_gateway")
KEY_VK = {"tab": VK_TAB, "shift_tab": VK_TAB, "escape": VK_ESCAPE, "page_up": VK_PRIOR, "page_down": VK_NEXT,
          "up": VK_UP, "down": VK_DOWN, "left": VK_LEFT, "right": VK_RIGHT, "home": VK_HOME, "end": VK_END}
NAME_ARGS = {"click_named": ("target",), "scroll": ("target",), "drag_named": ("from", "to")}
CATALOG_SOURCES = ("registry", "declared", "ocr")


class AgentRefused(ValueError):
    """Agent mode asked for where 5.6 forbids it (a flow class it never runs in, an unknown profile)."""


@dataclass(frozen=True)
class CatalogEntry:
    """One name the model may use (5.6 `click_named`): its name, where it comes from (`registry`, `declared`, `ocr`),
    the registry control ref when it is one, the resolver's target kind (splitter, detach_handle, order_bubble, ...),
    the window it is in, and the labels the resolver will report for it (resolved name, under-point text)."""

    name: str
    source: str = "registry"
    ref: str | None = None
    target_kind: str | None = None
    window: str | None = None
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source not in CATALOG_SOURCES:
            raise ValueError(f"catalog source {self.source!r}; one of {CATALOG_SOURCES}")

    @property
    def qualified(self) -> str:
        """The unambiguous name: the registry ref when there is one, else the name (choice 2)."""
        return self.ref or self.name


class NameCatalog:
    """The name catalog of one screen (5.6): names to entries, with the qualified names of choice 2."""

    def __init__(self, entries: Iterable[CatalogEntry]):
        self.entries = tuple(entries)

    def names(self) -> tuple[str, ...]:
        """Every name the model may use, sorted (the allowed values of an `unknown_target` error)."""
        return tuple(sorted({e.name for e in self.entries} | {e.ref for e in self.entries if e.ref}))

    def lookup(self, name: str, window: str | None = None) -> list[CatalogEntry]:
        """The entries a name denotes (in `window` when given): a qualified ref matches its one entry first."""
        pool = [e for e in self.entries if window is None or e.window in (None, window)]
        exact = [e for e in pool if e.ref == name]
        return exact or [e for e in pool if e.name == name]


@dataclass(frozen=True)
class ToolCall:
    """One tool call from the model: its name, its input and the call id the result answers."""

    name: str
    input: Mapping[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class ToolError:
    """A tool error returned to the model (5.6): its code, a message naming the allowed values, the allowed values,
    and whether it counts as a refusal (choice 1)."""

    code: str
    message: str
    allowed: tuple[str, ...] = ()
    refusal: bool = True

    def __post_init__(self) -> None:
        if self.code not in TOOL_ERRORS:
            raise ValueError(f"tool error {self.code!r}; one of {TOOL_ERRORS}")


@dataclass(frozen=True)
class ToolRequest:
    """A validated tool call: the tool, its arguments, the catalog entries its names resolved to (one for
    `click_named` and `scroll`, two for `drag_named`), and the press key, fixture id or zoom rectangle."""

    tool: str
    args: Mapping[str, Any]
    entries: tuple[CatalogEntry, ...] = ()
    key: str | None = None
    fixture_id: str | None = None
    zoom: ZoomRect | None = None

    @property
    def target_text(self) -> str:
        """The stated target as the step event records it (8.3 `target_text`)."""
        if self.entries:
            return " -> ".join(e.qualified for e in self.entries)
        return self.key or self.fixture_id or (canonical_json(self.args) if self.args else self.tool)

    def action(self, profile: str, *, point: tuple[int, int] | None = None, flow_class: str | None = None
               ) -> Action | None:
        """The gateway `Action` this call resolves to (5.4.9, choice 3); None for the read-only `zoom`. `point` is the
        physical point rungs 1 to 3 resolved for the (first) name."""
        if self.tool == "zoom":
            return None
        common: dict[str, Any] = {"profile": profile, "point": point, "flow_class": flow_class}
        if self.tool == "press":
            assert self.key is not None
            if self.key == "shift_tab":
                return Action("key_chord", keys=(VK_TAB,), mods=frozenset({"shift"}), **common)
            return Action("press", keys=(KEY_VK[self.key],), **common)
        if self.tool == "type_fixture":
            return Action("type_fixture", **common)
        first = self.entries[0]
        labels = dict(zip(("resolved_name", "under_point_text"), (first.labels or (first.name,)) * 2))
        kind = {"click_named": "click", "scroll": "wheel", "drag_named": "drag"}[self.tool]
        end_kind = self.entries[1].target_kind if self.tool == "drag_named" else None
        return Action(kind, target_kind=first.target_kind or first.ref, end_kind=end_kind, **labels, **common)


def request_text(call: ToolCall) -> str:
    """The full request as the step event stores it (5.6: "logged as a step event with the full request")."""
    return canonical_json({"tool": call.name, "input": dict(call.input) if isinstance(call.input, Mapping)
                           else call.input})


def check_response(*, model: str | None, stop_reason: str | None, pin: str) -> str | None:
    """`refusal` when a response ends the session with `agent_failed(refusal)` (5.6): `stop_reason` `refusal` or
    `max_tokens`, or a `model` field that differs from the pin; else None."""
    if stop_reason in REFUSAL_STOP_REASONS or model != pin:
        return "refusal"
    return None


@dataclass
class Dispatcher:
    """Validates the tool calls of one session (module docstring). `refusals` counts the vocabulary refusals;
    `exhausted` is True once the profile's limit is reached (`agent_failed(vocabulary)`). The caller sets `catalog`
    to each new screen's catalog (the OCR words change with every screenshot)."""

    profile: str
    catalog: NameCatalog
    typed_values: Sequence[str] = ()
    flow_class: str | None = None
    refusals: int = 0
    errors: list[ToolError] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.profile not in tools.PROFILES:
            raise AgentRefused(f"profile {self.profile!r}; agent mode runs under {tools.PROFILES}")
        if self.flow_class in EXCLUDED_CLASSES:
            raise AgentRefused(f"agent mode never runs in flows of class {self.flow_class} (5.6, 6.11)")
        self.typed_values = tuple(self.typed_values)

    @property
    def limit(self) -> int:
        return REFUSAL_LIMITS[self.profile]

    @property
    def exhausted(self) -> bool:
        """The refusal limit is reached: the session ends with `agent_failed(vocabulary)`."""
        return self.refusals >= self.limit

    def _error(self, code: str, message: str, allowed: Iterable[str] = (), *, refusal: bool = True) -> ToolError:
        err = ToolError(code, message, tuple(allowed), refusal)
        self.errors.append(err)
        if refusal:
            self.refusals += 1
        return err

    def dispatch(self, call: ToolCall) -> ToolRequest | ToolError:
        """Validate one call (module docstring); a `ToolError` is already counted."""
        allowed = tools.tool_names(self.profile)
        if call.name not in allowed:
            return self._error("unknown_tool", f"unknown tool {call.name!r}; the tools are {', '.join(allowed)}",
                               allowed)
        args = call.input if isinstance(call.input, Mapping) else None
        schema = tools.schema_of(call.name)
        if call.name == "press" and args is not None and set(args) == {"key"} and args["key"] not in tools.PRESS_KEYS:
            return self._error("key_not_allowed", f"key {args['key']!r} is not offered; the keys are "
                                                  f"{', '.join(tools.PRESS_KEYS)}", tools.PRESS_KEYS)
        problems = [] if args is None else sorted(Draft202012Validator(schema).iter_errors(dict(args)), key=str)
        if args is None or problems:
            params = tuple(schema["properties"])
            why = problems[0].message if problems else "the input is not an object"
            return self._error("malformed", f"{call.name}: {why}; its arguments are {', '.join(params)} "
                                            f"(required: {', '.join(schema.get('required', ()))})", params)
        entries: list[CatalogEntry] = []
        for arg in NAME_ARGS.get(call.name, ()):
            name = args[arg]
            found = self.catalog.lookup(name, args.get("window"))
            if not found:
                names = self.catalog.names()
                return self._error("unknown_target", f"{name!r} is not in the name catalog; the names are "
                                                     f"{', '.join(names)}", names)
            if len({e.qualified for e in found}) > 1:
                cands = tuple(sorted({e.qualified for e in found}))
                return self._error("ambiguous", f"{name!r} is ambiguous; name one of {', '.join(cands)}", cands,
                                   refusal=False)
            entries.append(found[0])
        if call.name == "type_fixture" and args["fixture_id"] not in self.typed_values:
            return self._error("unknown_fixture", f"fixture id {args['fixture_id']!r} is not a typed value; the ids "
                                                  f"are {', '.join(self.typed_values)}", self.typed_values)
        zoom = None
        if call.name == "zoom":
            r = args["region"]
            if r["x"] + r["w"] > tools.SCREEN_W or r["y"] + r["h"] > tools.SCREEN_H:
                return self._error("malformed", f"zoom region {dict(r)} lies outside the {tools.SCREEN_W}x"
                                                f"{tools.SCREEN_H} screenshot", ("x", "y", "w", "h"))
            zoom = ZoomRect(r["x"], r["y"], r["w"], r["h"])
        return ToolRequest(call.name, dict(args), tuple(entries), key=args.get("key"),
                           fixture_id=args.get("fixture_id"), zoom=zoom)

    def gateway_refused(self, reason: str) -> ToolError:
        """The tool error a gateway refusal returns to the model (5.6 `blocked_by_gateway`); the ledger, not this
        counter, applies its consequences (5.4.8), so it is not a vocabulary refusal."""
        return self._error("blocked_by_gateway", f"blocked_by_gateway: {reason}", refusal=False)
