#!/usr/bin/env python3
"""Lint and assemble the spec sections into one document.

Usage: python3 -I assemble.py [--out FILE]
Reports: em-dashes, missing/duplicate section files, heading numbering, unknown outcome names,
run-type and component vocabulary drift, word counts. Writes the assembled document.
"""
import glob
import os
import re
import sys
from collections import Counter

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sections")
ORDER = [
    "01-overview-and-how-it-works.md",
    "02-testing-model.md",
    "03-comparison-model.md",
    "04-inputs-and-schemas.md",
    "05-architecture-components.md",
    "06-safety.md",
    "07-environment-and-determinism.md",
    "08-storage-and-data-model.md",
    "09-reporting-triage-integrations.md",
    "10-system-reliability.md",
    "11-operations-and-delivery.md",
    "12-appendices.md",
]

CHECKPOINT_OUTCOMES = {
    "identical", "noise", "intended", "collateral", "needs_human", "invariant_violation",
    "assertion_failed", "not_settled", "not_comparable", "landmark_missing", "env_error",
    "harness_error", "not_run", "unregistered_surface", "needs_baseline",
}
STEP_OUTCOMES = {"passed", "passed_after_retry", "passed_via_agent_fallback", "failed", "blocked_by_gateway"}
RUN_OUTCOMES = {"complete", "complete_with_env_errors", "aborted_safety", "aborted_budget", "aborted_harness"}
OTHER_KNOWN = {"dependency_map_gap", "unknown_popup", "unreviewed", "quarantine", "frozen", "masked_live",
               "freeze", "rule", "mask", "diff", "candidate", "approved", "retired"}
ALL_KNOWN = CHECKPOINT_OUTCOMES | STEP_OUTCOMES | RUN_OUTCOMES | OTHER_KNOWN

# Words that look like outcome names (snake_case tokens in backticks) but are not in the closed set.
SNAKE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
# Tokens that are legitimately schema fields or identifiers, not outcomes; suppress from the drift report.
FIELD_HINTS = ("_id", "_at", "_by", "_hash", "_mode", "_window", "_ref", "_path", "_ms", "_px", "_pct",
               "_count", "_sec", "_min", "_s", "_version", "_name", "_key", "_url", "_dir", "_file",
               "_ok", "_list", "_set", "_map", "_tag", "_tags", "_cell", "_cells", "_row", "_rows",
               "_floor", "_threshold", "_tol", "_type", "_kind", "_state", "_flag", "_size", "_width",
               "_height", "_x", "_y", "_w", "_h", "_rect", "_bounds", "_crop", "_region", "_regions",
               "_step", "_steps", "_run", "_runs", "_build", "_builds", "_pair", "_login", "_account",
               "_symbol", "_symbols", "_text", "_score", "_rate", "_stats", "_results", "_log", "_events",
               "_fixture", "_corpus", "_registry", "_catalog", "_manifest", "_budget", "_limit", "_timeout")


def words(text):
    return len(re.findall(r"\S+", text))


def main():
    out = os.path.join(os.path.dirname(HERE), "SPEC.md")
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    problems = []
    parts = []
    total_words = 0
    present = {os.path.basename(p) for p in glob.glob(os.path.join(HERE, "*.md"))}
    for name in ORDER:
        if name not in present:
            problems.append(f"MISSING section file: {name}")
            continue
        text = open(os.path.join(HERE, name), encoding="utf-8").read()
        n = int(name[:2])
        em = text.count("—") + text.count("–")
        if em:
            problems.append(f"{name}: {em} em/en dashes")
        heads = re.findall(r"^(#{2,4})\s+(\S+)\s", text, re.M)
        top = [h for h in heads if h[0] == "##"]
        if not top or not top[0][1].startswith(f"{n}."):
            problems.append(f"{name}: top heading should start with '## {n}.' (found {top[:1]})")
        for level, num in heads:
            if level == "###" and not re.match(rf"{n}\.\d+", num):
                problems.append(f"{name}: subsection numbering off: '### {num}'")
        # outcome vocabulary drift
        drift = Counter()
        for tok in SNAKE.findall(text):
            if tok in ALL_KNOWN:
                continue
            if any(tok.endswith(h) for h in FIELD_HINTS) or tok.startswith(("sun_", "dwm", "vk_", "wh_", "set", "get", "enum")):
                continue
            drift[tok] += 1
        if drift:
            problems.append(f"{name}: snake_case tokens not in the closed vocabulary (check they are not outcome names): "
                            + ", ".join(f"{k}x{v}" for k, v in drift.most_common(25)))
        w = words(text)
        total_words += w
        parts.append((name, text, w))
    # assemble
    toc = []
    body = []
    for name, text, w in parts:
        for m in re.finditer(r"^(##|###)\s+(.+)$", text, re.M):
            level, title = m.group(1), m.group(2).strip()
            anchor = re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")
            toc.append(("  " if level == "###" else "") + f"- [{title}](#{anchor})")
        text = re.sub(r"(?m)^\s*Word count:.*$", "", text)
        body.append(text.strip() + "\n")
    doc = ["# thinkorswim UI Regression System (Sentinel): end-to-end specification\n"]
    preface_path = os.path.join(HERE, "00-preface.md")
    if os.path.exists(preface_path):
        doc.append(open(preface_path, encoding="utf-8").read().strip() + "\n")
    doc.extend(["## Contents\n", "\n".join(toc), "\n"])
    doc.extend(body)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(doc))
    print(f"assembled -> {out}")
    for name, _, w in parts:
        print(f"  {name}: {w} words")
    print(f"  total section words: {total_words}")
    print("PROBLEMS:" if problems else "no lint problems")
    for p in problems:
        print(" -", p)
    return 1 if any(p.startswith("MISSING") for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
