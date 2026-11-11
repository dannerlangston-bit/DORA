"""Generate config/taxonomy.yaml from the spec (1.10, 2.8.3, 3.13, 4.1, 4.8, 4.10, 4.11, 4.13, 4.14, 8.3,
10.3, 10.5, 12.2, 12.7).

Run: python tools/gen_taxonomy.py            # writes config/taxonomy.yaml
     python tools/gen_taxonomy.py --check    # exits 1 if the file is stale (CI, L-XREF-08)

Every list comes from the table or notation line that owns it (4.1 Single source for the taxonomy). Where
the spec states a list twice (the 4.1 key table and its owning table, 1.10 and the 12.2 copy, the 4.13
reason table and the 8.3 seed), the two are compared and a disagreement stops generation (L-XREF-08,
L-OUT-01): the file is never written from a spec that contradicts itself.
"""
from __future__ import annotations

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


class SpecDisagreement(ValueError):
    """Two places of the spec that must agree do not."""


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _same(name: str, owner: list, other: list, where: str) -> list:
    if owner != other:
        raise SpecDisagreement(f"{name}: the owning table gives {owner}, {where} gives {other}")
    return owner


def _code(pattern: str) -> tuple[str, bool]:
    """A reason pattern -> (code as 8.3 stores it, parametric): `step_failed:<step>` -> (`step_failed`, True)."""
    if "<" not in pattern:
        return pattern, False
    return pattern[: pattern.index("<") - 1], True


def reasons() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, codes in st.reason_table().items():
        codes = [c for c in _dedupe(codes) if c not in PROSE.get(key, set())]
        out[key] = [("probe:<Pn>" if c == "probe:<P1..P8>" else c) for c in codes]
    out.setdefault("identical", [])
    return out


def reason_metadata(reason_lists: dict[str, list[str]]) -> dict[str, dict[str, dict]]:
    """{outcome: {code: {owner_section, detail_means}}} from the 8.3 seed, which must list exactly the
    (outcome, code) pairs of the 4.13 table with the same parametric flag (SD-78: fingerprint_mismatch is plain)."""
    seed = st.outcome_reason_seed()
    want = {(o, _code(p)[0]): _code(p)[1] for o, pats in reason_lists.items() for p in pats}
    have = {(o, r): bool(par) for o, r, par, _, _ in seed}
    if set(want) != set(have):
        raise SpecDisagreement(f"8.3 seed and 4.13 reasons differ: only in 4.13 {sorted(set(want) - set(have))}, "
                               f"only in 8.3 {sorted(set(have) - set(want))}")
    flags = sorted(k for k in want if want[k] != have[k])
    if flags:
        raise SpecDisagreement(f"8.3 seed parametric flags disagree with the 4.13 parameters: {flags}")
    out: dict[str, dict[str, dict]] = {}
    for outcome, reason, _, means, owner in seed:
        out.setdefault(outcome, {})[reason] = {"owner_section": owner, "detail_means": means}
    return out


def gates() -> list[str]:
    return (["provisioned", "launched", "authenticated", "paper_mode_asserted", "settings_attested",
             "workspace_loaded", "fixture_verified", "canary_flow_passed", "running", "teardown",
             "reported", "connectivity", "rig_lost"] + [f"preflight.{i}" for i in st.preflight_items()])


def build() -> dict:
    outcomes = st.outcome_lists()
    copy_12_2 = st.outcome_lists(st.subsection("12.2"))
    for key, names in outcomes.items():
        _same(key, names, copy_12_2[key], "the 12.2 copy")
    reason_lists = reasons()
    return {
        "schema_version": 1,
        "generated_from": ("sentinel-spec sections 1.10, 2.8.3, 3.13, 4.1, 4.8, 4.10, 4.11, 4.13, 4.14, 8.3, 10.3, "
                           "10.5, 12.2, 12.7 (tools/gen_taxonomy.py)"),
        "checkpoint_outcomes": outcomes["checkpoint_outcomes"],
        "step_outcomes": outcomes["step_outcomes"],
        "run_outcomes": outcomes["run_outcomes"],
        "reasons": reason_lists,
        "reason_inheritance": {
            "env_error": "aborted_safety",     # unreached checkpoints of an aborted_safety run (1.10)
            "harness_error": "aborted_harness",  # unreached checkpoints of an aborted_harness run (1.10)
        },
        "assertion_builtins": ["app_alive", "size_reached", "soak.<metric>"],
        "app_alive_causes": ["jvm_crash", "hang", "oom", "process_exit"],
        "gates": gates(),
        "finding_kinds": st.finding_kinds(),
        "triage_states": _same("triage_states", st.triage_states(), st.listed_in_key_table("triage_states"),
                               "the 4.1 key table"),
        "decision_actions": st.decision_enum("action"),
        "decision_verdicts": st.decision_enum("verdict"),
        "canary_states": _same("canary_states", st.canary_states(), st.listed_in_key_table("canary_states"),
                               "the 4.1 key table"),
        "review_states": st.review_states(),
        "reviewer_verdicts": _same("reviewer_verdicts", st.reviewer_verdicts(),
                                   st.listed_in_key_table("reviewer_verdicts"), "the 4.1 key table"),
        # -- added with the accepted spec decisions (foundation.taxonomy items 1 to 9) --
        "quarantine_reasons": st.quarantine_reasons(),                                      # 10.5 (SD-96)
        "money_roles": _same("money_roles", st.money_roles(), st.listed_in_key_table("money_roles"),
                             "the 4.1 key table"),                                          # 4.8 (SD-28)
        "popup_actions": _same("popup_actions", st.popup_actions(), st.listed_in_key_table("popup_actions"),
                               "the 4.1 key table"),                                        # 4.10
        "canary_kinds": st.canary_kinds(),                                                  # 4.1, 8.3 (SD-81)
        "mutant_classes": st.mutant_classes(),                                              # 4.11, 10.3
        "step_event_actions": st.step_event_actions(),                                      # 8.3 (SD-16)
        "decision_statuses": st.decision_enum("status"),                                    # 4.14
        "decision_sources": st.decision_enum("source"),                                     # 4.14
        "reason_metadata": reason_metadata(reason_lists),                                   # 8.3 seed (SD-78)
        "rule_operands": st.rule_operands(),                                                # 2.8.3 (SD-34)
    }


def render(data: dict) -> str:
    header = "# GENERATED from the spec by tools/gen_taxonomy.py. Do not edit by hand.\n"
    return header + yaml.safe_dump(data, sort_keys=False, width=120)


def main() -> int:
    try:
        text = render(build())
    except SpecDisagreement as exc:
        print(f"the spec disagrees with itself: {exc}")
        return 2
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("config/taxonomy.yaml is stale: run python tools/gen_taxonomy.py")
            return 1
        print("taxonomy.yaml matches the spec")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
