"""Generate config/taxonomy.yaml from the spec (4.1, 4.13, 4.14, 1.10, 12.7).

Run: python tools/gen_taxonomy.py            # writes config/taxonomy.yaml
     python tools/gen_taxonomy.py --check    # exits 1 if the file is stale (CI, L-XREF-08)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import spec_tables as st  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "config" / "taxonomy.yaml"

# Prose mentions inside the spec's reason cells that are not reason codes.
PROSE = {
    "assertion_failed": {"detail", "reason"},
    "env_error": {"aborted_safety"},
    "harness_error": {"aborted_harness", "step_failed", "harness_error"},
    "aborted_harness": {"complete"},
    "complete_with_env_errors": {"env_error"},
    "unregistered_surface": {"detail"},
}


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def preflight_items() -> list[str]:
    text = st.section("12")
    body = text[text.index("### 12.7"):text.index("### 12.8")]
    return re.findall(r"^\| ([RBHNG]\d+) \|", body, flags=re.M)


def decision_enums() -> tuple[list[str], list[str]]:
    text = st.section("04")
    body = text[text.index("### 4.14"):text.index("### 4.15")]
    actions = re.search(r"action: enum\[([^\]]+)\]", body).group(1).split("|")
    verdicts = re.search(r"verdict: enum\[([^\]]+)\]", body).group(1).split("|")
    return actions, verdicts


def build() -> dict:
    outcomes = st.outcome_lists()
    raw = st.reason_table()
    reasons: dict[str, list[str]] = {}
    for key, codes in raw.items():
        codes = [c for c in _dedupe(codes) if c not in PROSE.get(key, set())]
        codes = [("probe:<Pn>" if c == "probe:<P1..P8>" else c) for c in codes]
        reasons[key] = codes
    reasons.setdefault("identical", [])
    actions, verdicts = decision_enums()
    gates = ["provisioned", "launched", "authenticated", "paper_mode_asserted", "settings_attested",
             "workspace_loaded", "fixture_verified", "canary_flow_passed", "running", "teardown",
             "reported", "connectivity", "rig_lost"] + [f"preflight.{i}" for i in preflight_items()]
    return {
        "schema_version": 1,
        "generated_from": "sentinel-spec sections 1.10, 4.1, 4.13, 4.14, 12.7 (tools/gen_taxonomy.py)",
        "checkpoint_outcomes": outcomes["checkpoint_outcomes"],
        "step_outcomes": outcomes["step_outcomes"],
        "run_outcomes": outcomes["run_outcomes"],
        "reasons": reasons,
        "reason_inheritance": {
            "env_error": "aborted_safety",     # unreached checkpoints of an aborted_safety run (1.10)
            "harness_error": "aborted_harness",  # unreached checkpoints of an aborted_harness run (1.10)
        },
        "assertion_builtins": ["app_alive", "size_reached", "soak.<metric>"],
        "app_alive_causes": ["jvm_crash", "hang", "oom", "process_exit"],
        "gates": gates,
        "finding_kinds": st.finding_kinds(),
        "triage_states": ["open", "confirmed_bug", "intended", "noise", "agent_mistake",
                          "expected_behavior", "fixed_pending_verify", "closed"],
        "decision_actions": actions,
        "decision_verdicts": verdicts,
        "canary_states": ["detected", "missed", "detector_blind", "detector_degraded", "generation_failed"],
        "review_states": ["none", "pending", "done"],
        "reviewer_verdicts": ["matches_manifest", "partially_matches_manifest", "does_not_match_manifest",
                              "no_visible_difference", "cannot_judge", "not_applicable"],
    }


def render(data: dict) -> str:
    header = "# GENERATED from the spec by tools/gen_taxonomy.py. Do not edit by hand.\n"
    return header + yaml.safe_dump(data, sort_keys=False, width=120)


def main() -> int:
    text = render(build())
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("config/taxonomy.yaml is stale: run python tools/gen_taxonomy.py")
            return 1
        print("taxonomy.yaml matches the spec")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
