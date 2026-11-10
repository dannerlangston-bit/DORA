"""Read the closed vocabularies out of the spec's markdown tables.

Used by tools/gen_taxonomy.py (to draft config/taxonomy.yaml) and by the taxonomy
test (L-XREF-08), which fails when taxonomy.yaml and the spec disagree.
"""
from __future__ import annotations

import re
from pathlib import Path

SPEC = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections"
CODE = re.compile(r"`([^`]+)`")


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


def _table_after(text: str, marker: str) -> list[list[str]]:
    start = text.index(marker)
    rows = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            rows.append(cells)
        elif rows:
            break
    return rows[1:]  # drop header


def _codes(cell: str) -> list[str]:
    toks = CODE.findall(_strip_parens(cell))
    return [t for t in toks if re.fullmatch(r"(?:[a-z][a-z0-9_]*|INV-\d\d|preflight\.<item>|preflight)(?::<[^>]+>|:<P1\.\.P8>)?", t)]


def section(name: str) -> str:
    return next(SPEC.glob(f"{name}-*.md")).read_text(encoding="utf-8")


def reason_table() -> dict[str, list[str]]:
    rows = _table_after(section("04"), "**Reason vocabulary.**")
    out: dict[str, list[str]] = {}
    for outcome_cell, codes_cell, _ in rows:
        key = CODE.findall(outcome_cell)[0]
        codes = _codes(codes_cell)
        if key == "invariant_violation":
            codes = [f"INV-{i:02d}" for i in range(1, 16)]
        if key == "env_error":
            # first backticked token "gate" is the generic code; keep it
            pass
        out[key] = codes
    return out


def finding_kinds() -> list[str]:
    rows = _table_after(section("04"), "**Finding vocabulary.**")
    kinds: list[str] = []
    for kind_cell, *_ in rows:
        kinds += CODE.findall(kind_cell)
    return kinds


def outcome_lists() -> dict[str, list[str]]:
    """The three outcome lists from 1.10's tables (first column, backticked)."""
    text = section("01")
    start = text.index("### 1.10")
    end = text.index("### 1.11")
    body = text[start:end]
    tables, cur = [], []
    for line in body.splitlines():
        if line.startswith("|"):
            cur.append(line)
        elif cur:
            tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    lists = []
    for t in tables:
        names = []
        for line in t[2:]:
            first = line.strip().strip("|").split("|")[0]
            m = CODE.findall(first)
            if m:
                names.append(m[0])
        if names:
            lists.append(names)
    return {"checkpoint_outcomes": lists[0], "step_outcomes": lists[1], "run_outcomes": lists[2]}
