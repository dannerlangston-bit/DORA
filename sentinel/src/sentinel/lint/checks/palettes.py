"""L-XREF-30 (4.16, 2.7, 5.9): every theme a Workspace Corpus entry declares has its INV-15 palette, and every
registry `invariants` block names controls the surface registers.

- `palettes/<theme>.yaml` exists for each `display_config.theme`, declares that `theme`, and gives `positive`,
  `negative` and `neutral` as `#rrggbb` colors (`effective.checker_config` reads it at run start, 2.7 INV-15);
- a surface's `invariants.overlap_allowlist` pairs and `allowed_scrollbars` keys name control ids of that surface (or
  its component names, which the checker also accepts: a name with a dot or a slash is taken as a component path).

Choice (4.16 is silent on component names): an entry that is not a registered control id is reported only when it
looks like a control id (no `.` and no `/`), since INV-01 and INV-12 also match component names the bounds source
gives (`invariants.py`).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

RULE_IDS = ("L-XREF-30",)
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
KEYS = ("positive", "negative", "neutral")


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def check(repo: Any, add: Callable[..., None], phase: int, today: date) -> None:
    themes: dict[str, str] = {}
    for doc in repo.of("workspace"):
        theme = _d(_d(doc.data).get("display_config")).get("theme")
        if isinstance(theme, str) and theme:
            themes.setdefault(theme, doc.rel)
    for theme, where in sorted(themes.items()):
        rel = f"palettes/{theme}.yaml"
        doc = repo.docs.get(rel)
        data = _d(doc.data) if doc is not None and not doc.error else {}
        if not data:
            add("L-XREF-30", where, f"theme {theme} has no palette: {rel} is missing (INV-15, 2.7)", "display_config.theme")
            continue
        if data.get("theme") != theme:
            add("L-XREF-30", rel, f"declares theme {data.get('theme')!r}, not {theme}", "theme")
        colors = _d(data.get("colors"))
        for k in KEYS:
            if not (isinstance(colors.get(k), str) and COLOR.match(colors[k])):
                add("L-XREF-30", rel, f"colors.{k} is not a #rrggbb color", f"colors.{k}")
    for doc in repo.of("surface"):
        s = _d(doc.data)
        block = _d(s.get("invariants"))
        ids = {str(_d(c).get("id")) for c in s.get("controls") or () if _d(c).get("id")}

        def named(x: Any) -> bool:
            t = str(x)
            return t in ids or "." in t or "/" in t
        for i, pair in enumerate(block.get("overlap_allowlist") or ()):
            for x in pair if isinstance(pair, list) else ():
                if not named(x):
                    add("L-XREF-30", doc.rel, f"overlap_allowlist names {x!r}, not a control of {s.get('id')}",
                        f"invariants.overlap_allowlist[{i}]")
        for k in _d(block.get("allowed_scrollbars")):
            if not named(k):
                add("L-XREF-30", doc.rel, f"allowed_scrollbars names {k!r}, not a control of {s.get('id')}",
                    f"invariants.allowed_scrollbars.{k}")
