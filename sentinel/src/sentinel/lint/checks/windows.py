"""L-XREF-24 and L-XREF-25: `schedule/windows.yaml`, the crypto-futures profile and `ops/credential_rotation.yaml`
(4.15, 4.16, 7.7, 7.10; SD-67, SD-74).

- L-XREF-24: `schedule/windows.yaml` exists (the scheduler and the preflight read it, 4.1) and passes
  `calendar.config_problems`, the check `WindowsConfig.from_doc` applies at run time: the four named windows, `HH:MM`
  times, an operating day that starts before it ends, quiet windows the scheduler resolves; every `requires_window`
  of a flow header or checkpoint names a window the file defines.
- The crypto-futures profile of 7.10 ("/BTC and /MBT have no quiet window; they get an always-live profile with
  `rule` regions and no pixel-diffed quote cell"), reported under L-XREF-24 because the RULES table has no row of its
  own (choice 1). A flow checkpoint whose fixture symbol in force is a CME crypto future (`calendar.is_crypto_future`)
  declares no quiet window, and when `masked_live` its regions file has a `rule` region and no quote cell declared
  `diff` or `freeze`.
- L-XREF-25: `rotation.calendar_problems` over `ops/credential_rotation.yaml` with the logins of
  `gateway/allowlist.yaml`, the DR-03 branch of `phase0/branches.yaml` and `today`. A missing file is an error while
  a rig uses a login (every such login needs an entry).

Schema failures of both files are reported under the same rule ids by rules.py.

Choices where the spec is ambiguous (reported):

1. The crypto check has no RULES row; L-XREF-24 is the windows rule of 7.10, where the profile is stated. A dedicated
   row would be a spec edit.
2. The fixture symbol in force at a checkpoint is the last `fixture.symbols.<id>.` reference in the flow's
   preconditions and steps up to and including that checkpoint step (a `type_fixture` value, a `verify` or an
   `expect` entry), resolved in the flow's fixture. Census checkpoints of a workspace linked to a crypto future are
   not checked here.
3. A quote cell is a region an assertion of the checkpoint (its `expect` entries and the `assert` steps before the
   next checkpoint) reads as a market quote: an `api_within` region, or a `rule` input bound to a quote operand
   (`QUOTE_OPERANDS`: R03's bid, mark, ask, last, low and high, R06's call, put and underlying, R08's mark).
4. Frozen crypto checkpoints keep their regions: frozen data is deterministic by construction (7.10), so only the
   quiet-window declaration applies to them.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import date
from typing import Any, Callable

from ... import branches as br
from ...calendar import NO_WINDOW, config_problems, is_crypto_future
from ...rotation import calendar_problems
from ..repo import Repo

RULE_IDS = ("L-XREF-24", "L-XREF-25")
WINDOWS_PATH = "schedule/windows.yaml"
ROTATION_PATH = "ops/credential_rotation.yaml"
QUOTE_OPERANDS = {"R03": frozenset({"bid", "mark", "ask", "last", "low", "high"}),
                  "R06": frozenset({"call", "put", "underlying"}), "R08": frozenset({"mark"})}
_SYMBOL_REF = re.compile(r"^fixture\.symbols\.([^.]+)\.")


def _strings(node: Any) -> Iterator[str]:
    """Every string inside a step, depth first in document order."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _quote_cells(assertions: list[Any]) -> set[str]:
    """The regions the assertions read as market quotes (choice 3)."""
    out: set[str] = set()
    for a in assertions:
        if not isinstance(a, Mapping):
            continue
        if a.get("kind") == "api_within" and isinstance(a.get("region"), str):
            out.add(a["region"])
        operands = QUOTE_OPERANDS.get(str(a.get("rule_id")), frozenset())
        for name, value in (a.get("inputs") or {}).items():
            if name in operands and isinstance(value, str):
                out.add(value)
    return out


def _windows(repo: Repo, add: Callable[..., None]) -> set[str] | None:
    """L-XREF-24 on the file; returns the window names it defines (None when it cannot be read)."""
    doc = repo.one("windows")
    if doc is None:
        add("L-XREF-24", WINDOWS_PATH, "schedule/windows.yaml is missing; the scheduler, the operating day and the "
                                       "maintenance reschedule read it (4.1, 7.10)")
        return None
    if doc.error or not isinstance(doc.data, Mapping):
        return None                                  # the parse or schema failure is reported by rules.py
    for path, message in config_problems(doc.data):
        add("L-XREF-24", doc.rel, message, path)
    wins = doc.data.get("windows")
    return set(wins) if isinstance(wins, Mapping) else None


def _flows(repo: Repo, add: Callable[..., None], defined: set[str] | None) -> None:
    """`requires_window` references and the crypto-futures profile on every flow (choices 1 to 4)."""
    symbols: dict[str, dict[str, str]] = {}
    for f in repo.of("fixture"):
        if isinstance(f.data, Mapping):
            symbols[str(f.data.get("fixture_id"))] = {str(s.get("id")): str(s.get("symbol", ""))
                                                      for s in f.data.get("symbols") or () if isinstance(s, Mapping)}
    for doc in repo.of("flow"):
        flow = doc.data
        if not isinstance(flow, Mapping):
            continue
        header = flow.get("requires_window") or NO_WINDOW
        if defined is not None and header not in defined:
            add("L-XREF-24", doc.rel, f"requires_window {header} is not a window of schedule/windows.yaml",
                "requires_window")
        syms = symbols.get(str(flow.get("fixture")), {})
        current: str | None = None
        steps = [("preconditions", s) for s in flow.get("preconditions") or ()] + \
                [("steps", s) for s in flow.get("steps") or ()]
        counters = {"preconditions": 0, "steps": 0}
        for i, (block, step) in enumerate(steps):
            at = f"{block}[{counters[block]}]"
            counters[block] += 1
            if not isinstance(step, Mapping):
                continue
            for text in _strings(step):
                m = _SYMBOL_REF.match(text)
                if m and m.group(1) in syms:
                    current = syms[m.group(1)]
            if step.get("type") != "checkpoint":
                continue
            own = step.get("requires_window")
            if own is not None and defined is not None and own not in defined:
                add("L-XREF-24", doc.rel, f"requires_window {own} is not a window of schedule/windows.yaml",
                    f"{at}.requires_window")
            if current is None or not is_crypto_future(current):
                continue
            cid = step.get("checkpoint_id")
            effective = own or header
            if effective != NO_WINDOW:
                add("L-XREF-24", doc.rel, f"{cid} shows crypto future {current}, which has no quiet window (always "
                                          f"live, 7.10); requires_window {effective} never opens for it",
                    f"{at}.requires_window")
            if step.get("data_mode") == "masked_live":
                later = [s for _b, s in steps[i + 1:]]
                stop = next((k for k, s in enumerate(later) if isinstance(s, Mapping) and s.get("type") == "checkpoint"),
                            len(later))
                asserts = [a for s in later[:stop] if isinstance(s, Mapping) and s.get("type") == "assert"
                           for a in s.get("assertions") or ()]
                _crypto_regions(repo, add, doc.rel, at, step, current, list(step.get("expect") or ()) + asserts)


def _crypto_regions(repo: Repo, add: Callable[..., None], rel: str, at: str, step: Mapping[str, Any], symbol: str,
                    assertions: list[Any]) -> None:
    """A masked_live checkpoint on a crypto future: at least one `rule` region, no quote cell diffed or frozen."""
    cid = step.get("checkpoint_id")
    regions_doc = repo.docs.get(str(step.get("regions")))
    regions = regions_doc.data.get("regions") if regions_doc is not None and isinstance(regions_doc.data, Mapping) \
        else None
    if not isinstance(regions, list):
        return                                       # a missing regions file is L-FLW-04's
    if not any(isinstance(r, Mapping) and r.get("type") == "rule" for r in regions):
        add("L-XREF-24", rel, f"{cid} shows crypto future {symbol}: the always-live profile needs rule regions (7.10)",
            f"{at}.regions")
    quotes = _quote_cells(assertions)
    for r in regions:
        if isinstance(r, Mapping) and r.get("id") in quotes and r.get("type") in ("diff", "freeze"):
            add("L-XREF-24", rel, f"{cid} shows crypto future {symbol}: quote cell {r['id']} is {r['type']}; the "
                                  "always-live profile never pixel-diffs a quote cell, declare it rule (7.10)",
                f"{at}.regions")


def _login_mfa_b(repo: Repo) -> bool:
    """DR-03 branch B in force: recorded B, or undecided with the 11.8 default B (choice 4 of rotation)."""
    doc = repo.one("branches")
    data = doc.data if doc is not None and isinstance(doc.data, Mapping) else {}
    choice = data.get("login_mfa")
    letter = choice.get("branch") if isinstance(choice, Mapping) else None
    if letter in br.LETTERS:
        return letter == "B"
    return br.DEFAULTS["login_mfa"] == "B"


def _rotation(repo: Repo, add: Callable[..., None], today: date) -> None:
    allow = repo.one("allowlist")
    rigs = allow.data.get("rigs") if allow is not None and isinstance(allow.data, Mapping) else None
    logins = sorted({str(r["login_ref"]) for r in rigs or () if isinstance(r, Mapping) and r.get("login_ref")})
    doc = repo.one("credential_rotation")
    if doc is None:
        if logins:
            add("L-XREF-25", ROTATION_PATH, "ops/credential_rotation.yaml is missing; every login a rig uses needs "
                                            f"an owner entry (7.7): {', '.join(logins)}")
        return
    if doc.error or not isinstance(doc.data, Mapping):
        return                                       # the parse or schema failure is reported by rules.py
    for path, message in calendar_problems(doc.data, rig_logins=logins, branch_b=_login_mfa_b(repo), today=today):
        add("L-XREF-25", doc.rel, message, path)


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    """Report L-XREF-24 and L-XREF-25 (module docstring)."""
    defined = _windows(repo, add)
    _flows(repo, add, defined)
    _rotation(repo, add, today)
