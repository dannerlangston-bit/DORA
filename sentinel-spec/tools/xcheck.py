#!/usr/bin/env python3
"""Cross-section consistency check for the spec sections.

Flags (1) superseded vocabulary that the FINAL DECISIONS in integration-notes.md retired,
(2) cross-references "N.x" or "N.x.y" that do not match any heading in any section,
(3) outcome tokens outside the closed taxonomy.
Usage: python3 -I xcheck.py [files...]   (default: all 12 section files)
"""
import glob
import os
import re
import sys

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sections")
SECTIONS = sorted(glob.glob(os.path.join(HERE, "[0-1][0-9]-*.md")))
SECTIONS = [p for p in SECTIONS if not os.path.basename(p).startswith("00-")]

SUPERSEDED = [
    (r"harness_error\s*\(?\s*\(?reason:\s*step_failed", "step_failed is never a harness_error reason (F4)"),
    (r"harness_error\s*\(?\s*\(?reason:\s*base_step_failed", "base_step_failed is a not_run reason (F4)"),
    (r"harness_error[:\s]+step_failed", "step_failed is never a harness_error reason (F4)"),
    (r"needs_human\s*\(reason:\s*unstable\)", "instability is not_settled (reason: unstable)"),
    (r"\bbuild_mismatch\b", "use version_mismatch"),
    (r"\bpending_review\b", "use review_state: pending"),
    (r"env_error\s*\(reason:\s*app_crash\)", "app crash is assertion_failed (assertion: app_alive) + not_run (reason: app_crash)"),
    (r"fingerprint:calibration|calibration_missing|no_calibration\b", "missing calibration is needs_human (reason: uncalibrated) (F2)"),
    (r"not_comparable\s*\(reason:\s*(fingerprint:|calibration)", "not_comparable reasons are fingerprint_mismatch|window_mismatch|surface_mismatch (F2)"),
    (r"env_error\s*\(gate:\s*soak\)", "soak failures are the app failure trio (F13)"),
    (r"\bsize_sweep/", "checkpoint kind is sweep (id grammar)"),
    (r"\b(trade|monitor|analyze|scan|marketwatch|charts|tools|help|dialog|setup|settings|sidebar|gadget)\.[a-z0-9.-]*_[a-z0-9_]*", "surface ids use hyphens inside segments, not underscores"),
    (r"\blocator_fallback\b", "finding kind is locator_failure (step outcome passed_via_agent_fallback is fine)"),
    (r"app_alive[^.\n]{0,80}exception_dialog|exception_dialog[^.\n]{0,80}app_alive", "an exception dialog is INV-14, not an app_alive failure"),
    (r"not_run\s*\(\s*(reason:\s*)?scope\s*\)[^.\n]{0,60}(failed|blocked) step|(failed|blocked) step[^.\n]{0,60}not_run\s*\(\s*(reason:\s*)?scope", "downstream of a failed step is not_run (reason: step_failed:<step>) (F4)"),
    (r"Nightly run is `?harness_error", "F1: nightly run stays complete; canary state carries detector_blind"),
    (r"\bopens_ticket\b", "flag is order_stage"),
    (r"kind[^\n]{0,20}`crash`|finding[^\n]{0,20}`crash`", "finding kind is app_crash"),
    (r"Watchdog-owned hook|hook[^.\n]{0,40}owned by the Watchdog", "the hook host owns the hook (F11)"),
    (r"Orchestrator'?s local API", "the Reporter service owns the API (F11)"),
    (r"Position Statement[^.\n]{0,40}15 percent|15 percent[^.\n]{0,40}Position Statement", "Position Statement mask budget is 10 percent"),
    (r"temperature\s*[=:]?\s*0\b(?![^.\n]{0,120}(no sampling|sampling parameters|means))", "temperature 0 must be explained as no sampling parameters (03 owns)"),
    (r"9\.6 Alerting", "alerting is 9.10"),
    (r"10\.9 (acceptance|end-to-end)", "acceptance test is 10.10"),
    (r"\bP0[1-9]\b|\bP1[0-7]\b(?![0-9])", "popup ids are POP-nn; P1..P8 are registry probes only"),
    (r"charts\.flexible_grid|active_trader_cell|order_entry\b", "underscored surface ids"),
    (r"—|–", "em/en dash"),
    (r"^Word count", "trailing word count line"),
]

HEADING_RE = re.compile(r"^(#{2,4})\s+((\d+)(?:\.(\d+))?(?:\.(\d+))?)\.?\s+(.*)$", re.M)
XREF_RE = re.compile(r"(?<![\w.])(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?(?![\w.%])")


def headings(paths):
    found = set()
    for p in paths:
        text = open(p, encoding="utf-8").read()
        for m in HEADING_RE.finditer(text):
            num = m.group(2).rstrip(".")
            found.add(num)
    return found


def main():
    paths = [a for a in sys.argv[1:] if a.endswith(".md")] or SECTIONS
    all_heads = headings(SECTIONS)
    total = 0
    for p in paths:
        name = os.path.basename(p)
        text = open(p, encoding="utf-8").read()
        lines = text.split("\n")
        hits = []
        for pat, why in SUPERSEDED:
            for i, line in enumerate(lines, 1):
                if re.search(pat, line):
                    hits.append((i, why, line.strip()[:140]))
        # dangling cross references: only inside prose/tables, skip code blocks and numbers like versions
        in_code = False
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or line.startswith("#"):
                continue
            for m in XREF_RE.finditer(line):
                a, b, c = m.group(1), m.group(2), m.group(3)
                if int(a) < 1 or int(a) > 12:
                    continue
                # skip obvious non-references: decimals followed by units or percent handled by lookahead; versions like 3.12 in "Python 3.12"
                ctx = line[max(0, m.start() - 12):m.start()].lower()
                if any(k in ctx for k in ("python", "sqlite", "java ", "jdk", "version", "zulu", "v", "px", "ratio", "score", "threshold", "sigma", "k1", "k2", "x ", "at ", "to ", "over ", "under ", "above ", "below ", "of ", "= ", "> ", "< ", "tolerance", "delta", "floor")):
                    continue
                if int(b) > 19 or (len(b) == 2 and b.startswith("0")) or (c and (int(c) > 19 or (len(c) == 2 and c.startswith("0")))):
                    continue
                num = f"{a}.{b}" + (f".{c}" if c else "")
                if num not in all_heads:
                    hits.append((i, f"cross-reference {num} matches no heading", line.strip()[:140]))
        if hits:
            print(f"\n== {name}: {len(hits)} issues")
            for i, why, snippet in hits[:80]:
                print(f"  L{i}: {why}\n      {snippet}")
            if len(hits) > 80:
                print(f"  ... {len(hits) - 80} more")
        total += len(hits)
    print(f"\nTOTAL issues: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
