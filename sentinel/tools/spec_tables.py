"""Read the closed vocabularies and the mirrored tables out of the spec's markdown.

Used by tools/gen_taxonomy.py (to write config/taxonomy.yaml) and by the taxonomy and mirror
tests (L-XREF-08, L-XREF-12, L-FPR-01, 12.2), which fail when the code and the spec disagree.
Every function reads the section files under sentinel-spec/sections; none of them guesses: a table
or a notation line that is not where it should be raises (ValueError, StopIteration or IndexError).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SPEC = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections"
CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^(#{2,4}) (\d+(?:\.\d+)*)\b", re.M)


def _strip_parens(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def split_row(line: str) -> list[str]:
    """The cells of one markdown table row (a `|` inside backticks does not split, `\\|` neither)."""
    cells, cur, tick, i = [], [], False, 0
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def tables(text: str) -> list[list[list[str]]]:
    """Every markdown table of `text`, each a list of rows (header included, separator dropped)."""
    out, cur = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            cells = split_row(line)
            if not (set("".join(cells)) <= set("-: ")):
                cur.append(cells)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def table_after(text: str, marker: str) -> list[list[str]]:
    """The body rows (header dropped) of the first table at or after `marker` (a prose marker, or the
    table's own header row)."""
    start = text.rindex("\n", 0, text.index(marker)) + 1
    rows: list[list[str]] = []
    for line in text[start:].splitlines():
        if line.startswith("|"):
            cells = split_row(line)
            if set("".join(cells)) <= set("-: "):
                continue
            rows.append(cells)
        elif rows:
            break
    return rows[1:]  # drop header


# Backward-compatible name used by older callers.
_table_after = table_after


def _codes(cell: str) -> list[str]:
    toks = CODE.findall(_strip_parens(cell))
    return [t for t in toks if re.fullmatch(r"(?:[a-z][a-z0-9_]*|INV-\d\d|preflight\.<item>|preflight)(?::<[^>]+>|:<P1\.\.P8>)?", t)]


def section(name: str) -> str:
    """The whole text of section file NN (`01`, `04`, ...)."""
    return next(SPEC.glob(f"{name}-*.md")).read_text(encoding="utf-8")


def subsection(number: str) -> str:
    """The text of subsection `number` (for example `4.13` or `2.8.3`), heading included, up to the next
    heading of the same or a higher level."""
    text = section(f"{int(number.split('.')[0]):02d}")
    heads = list(_HEADING.finditer(text))
    for i, m in enumerate(heads):
        if m.group(2) == number:
            level = len(m.group(1))
            end = next((h.start() for h in heads[i + 1:] if len(h.group(1)) <= level), len(text))
            return text[m.start():end]
    raise ValueError(f"no subsection {number}")


def notation_enum(text: str, field: str) -> list[str]:
    """The values of the first `  <field>: enum[a|b|...]` notation line in `text`."""
    m = re.search(rf"^\s+{re.escape(field)}: enum\[([^\]]+)\]", text, re.M)
    if not m:
        raise ValueError(f"no enum notation for {field}")
    return [v.strip() for v in m.group(1).split("|")]


# -- 4.13 and 1.10: reasons, findings and the outcome lists ------------------------------------------

def reason_table(text: str | None = None) -> dict[str, list[str]]:
    """The 4.13 reason vocabulary (or the same table in `text`, such as the 12.2 copy) as raw code lists."""
    rows = table_after(text if text is not None else section("04"), "| Outcome | Reason codes |")
    out: dict[str, list[str]] = {}
    for outcome_cell, codes_cell, _ in rows:
        key = CODE.findall(outcome_cell)[0]
        codes = _codes(codes_cell)
        if key == "invariant_violation":
            codes = [f"INV-{i:02d}" for i in range(1, 16)]
        out[key] = codes
    return out


def finding_kinds(text: str | None = None) -> list[str]:
    """The finding vocabulary of 4.13 (or of the same table in `text`)."""
    rows = table_after(text if text is not None else section("04"), "| Kind | Meaning | Section |")
    kinds: list[str] = []
    for kind_cell, *_ in rows:
        kinds += CODE.findall(kind_cell)
    return kinds


def outcome_lists(text: str | None = None) -> dict[str, list[str]]:
    """The three outcome lists from 1.10's tables (first column, backticked); with `text`, from the
    tables of that text (the 12.2 copy)."""
    if text is None:
        text = subsection("1.10")
    lists = []
    for t in tables(text):
        if t and t[0][:2] == ["Outcome", "Definition"]:
            names = [CODE.findall(r[0])[0] for r in t[1:] if CODE.findall(r[0])]
            lists.append(names)
    return {"checkpoint_outcomes": lists[0], "step_outcomes": lists[1], "run_outcomes": lists[2]}


# -- verbatim copies (12.2, 12.3, 12.6, 12.8) -------------------------------------------------------

def table_block(text: str, header: str, nth: int = 0) -> str:
    """The exact text of the `nth` markdown table whose header row starts with `header`, header to last row."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith(header)]
    i = starts[nth]
    j = i
    while j < len(lines) and lines[j].lstrip().startswith("|"):
        j += 1
    return "".join(lines[i:j])


def outcome_taxonomy_block(text: str) -> str:
    """From the `Precedence.` paragraph through the last row of the per-run outcome table: the part of 1.10
    that 12.2 copies verbatim."""
    start = text.index("\nPrecedence.") + 1
    last = table_block(text, "| Outcome | Definition |", nth=2)
    end = text.index(last, start) + len(last)
    return text[start:end]


# -- 4.1 taxonomy key table -------------------------------------------------------------------------

def taxonomy_key_table() -> dict[str, str]:
    """4.1 Single source for the taxonomy: key -> its Contents cell."""
    rows = table_after(subsection("4.1"), "| Key | Contents | Source of truth |")
    out: dict[str, str] = {}
    for key_cell, contents, _ in rows:
        for key in CODE.findall(key_cell):
            out[key] = contents
    return out


def listed_in_key_table(key: str) -> list[str]:
    """The backticked values a 4.1 key row lists before its first colon or semicolon (`a`, `b`: ...)."""
    contents, tick = taxonomy_key_table()[key], False
    for i, ch in enumerate(contents):
        tick = not tick if ch == "`" else tick
        if ch in ":;" and not tick:
            return CODE.findall(contents[:i])
    return CODE.findall(contents)


# -- owning tables of the other vocabularies --------------------------------------------------------

def quarantine_reasons() -> list[str]:
    """10.5 Quarantine reasons table, first column."""
    rows = table_after(subsection("10.5"), "| Reason | Entered when | Introduced by |")
    return [CODE.findall(r[0])[0] for r in rows]


def money_roles() -> list[str]:
    """4.8 region `money_role` enum."""
    return notation_enum(subsection("4.8"), "money_role")


def popup_actions() -> list[str]:
    """4.10 popup entry `action` enum."""
    return notation_enum(subsection("4.10"), "action")


def mutant_classes() -> list[str]:
    """4.11 mutant entry `class` enum (the 26 classes of the 10.3 class table)."""
    return notation_enum(subsection("4.11"), "class")


def canary_kinds() -> list[str]:
    """4.1 `canary_kinds` row: the kinds of a `canary_results` row (8.3, 10.3)."""
    return listed_in_key_table("canary_kinds")


def canary_states() -> list[str]:
    """10.3 Scoring: 'Result states per mutant are `detected`, ... and `generation_failed`'."""
    text = subsection("10.3")
    m = re.search(r"Result states per mutant are (.*?) \(", text)
    if not m:
        raise ValueError("10.3 result states sentence not found")
    return CODE.findall(m.group(1))


def reviewer_verdicts() -> list[str]:
    """3.13 structured output schema: the `verdict` enum of the reviewer's JSON answer."""
    text = subsection("3.13")
    m = re.search(r'"verdict": \{"type": "string", "enum": (\[[^\]]*\])', text)
    if not m:
        raise ValueError("3.13 verdict enum not found")
    return json.loads(m.group(1))


def triage_states() -> list[str]:
    """4.13 finding `triage_state` enum."""
    return notation_enum(subsection("4.13"), "triage_state")


def review_states() -> list[str]:
    """4.13 checkpoint outcome `review_state` enum."""
    return notation_enum(subsection("4.13"), "review_state")


def decision_enum(field: str) -> list[str]:
    """One enum of the 4.14 decision record (`action`, `verdict`, `status`, `source`)."""
    return notation_enum(subsection("4.14"), field)


def step_event_actions() -> list[str]:
    """8.3 `step_events.action`: the action names its column comment lists."""
    text = subsection("8.3")
    body = text[text.index("CREATE TABLE step_events"):]
    lines = body.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"\s+action\s+TEXT", ln))
    words = [lines[start].split("--", 1)[1]]
    for ln in lines[start + 1:]:
        s = ln.strip()
        if not s.startswith("--"):
            break
        words.append(s[2:])
    return [w.strip() for w in ",".join(words).split(",") if w.strip()]


def outcome_reason_seed() -> list[tuple[str, str, int, str | None, str]]:
    """8.3 `outcome_reasons` seed rows (outcome, reason, parametric, detail_means, owner_section), the
    VALUES of the INSERT only (the INSERT ... SELECT inheritance rows are not part of it)."""
    text = subsection("8.3")
    start = text.index("INSERT INTO outcome_reasons")
    end = text.index(";\n", start)
    body = "\n".join(ln for ln in text[start:end].splitlines() if not ln.lstrip().startswith("--"))
    lit = r"'((?:[^']|'')*)'"
    row = re.compile(rf"\({lit},{lit},([01]),(NULL|{lit}),{lit}\)")
    out = []
    for m in row.finditer(body):
        outcome, reason, par, _, means, owner = m.groups()
        out.append((outcome, reason, int(par), None if means is None else means.replace("''", "'"), owner))
    return out


def rule_operands() -> dict[str, dict[str, dict]]:
    """2.8.3 Rule operands: rule -> operand -> {type, required}; the type drops its parenthetical."""
    rows = table_after(subsection("2.8.3"), "| Rule | Operand | Type | Required |")
    out: dict[str, dict[str, dict]] = {}
    for rule, operand, typ, required in rows:
        name = CODE.findall(operand)[0]
        req = _strip_parens(required).strip()
        if req not in ("yes", "no"):
            raise ValueError(f"2.8.3 {rule} {name}: required is {required!r}")
        out.setdefault(rule, {})[name] = {"type": _strip_parens(typ).strip(), "required": req == "yes"}
    return out


# -- tables other code mirrors (L-FPR-01, L-XREF-12) ------------------------------------------------

def fingerprint_fields_7_4() -> list[str]:
    """The field names of the 7.4 fingerprint table, in table order."""
    rows = table_after(subsection("7.4"), "| Field | How it is read | Class |")
    return [r[0].strip() for r in rows]


def mutant_table_12_5() -> dict[str, tuple[str, bool]]:
    """12.5: mutant id -> (class, money_cell)."""
    rows = table_after(subsection("12.5"), "| Id | Class (10.3) |")
    out = {}
    for r in rows:
        flag = r[-1].strip()
        if flag not in ("yes", "no"):
            raise ValueError(f"12.5 {r[0]}: money_cell is {flag!r}")
        out[r[0].strip()] = (CODE.findall(r[1])[0], flag == "yes")
    return out


def popup_ids_12_8() -> list[str]:
    """12.8: the starter popup ids, in table order."""
    rows = table_after(subsection("12.8"), "| Id | Window |")
    return [r[0].strip() for r in rows]


def preflight_items() -> list[str]:
    """12.7: the preflight item ids, in table order."""
    return re.findall(r"^\| ([RBHNG]\d+) \|", subsection("12.7"), flags=re.M)
