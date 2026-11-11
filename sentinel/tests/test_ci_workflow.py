"""The CI workflow runs the harness checks of 4.15 "Pull request to sentinel" (04b-schemas-committed-ci)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "sentinel.yml"
pytestmark = pytest.mark.skipif(not WORKFLOW.exists(), reason="repository checkout without .github")


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))          # YAML 1.1 reads the key `on` as True
    return data


def test_triggers_on_sentinel_and_spec_changes():
    on = _workflow()["on"]
    for event in ("push", "pull_request"):
        assert {"sentinel/**", "sentinel-spec/**"} <= set(on[event]["paths"])


def test_runs_every_check_with_pinned_first_party_actions():
    steps = _workflow()["jobs"]["sentinel"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses == ["actions/checkout@v4", "actions/setup-python@v5"]
    assert next(s for s in steps if "uses" in s and "setup-python" in s["uses"])["with"]["python-version"] == "3.12"
    runs = "\n".join(s.get("run", "") for s in steps)
    for command in ("pip install -e 'sentinel[test]'", "python -m pytest", "sentinel lint config/example --today 2026-11-10",
                    "python tools/gen_taxonomy.py --check", "sentinel schemas --check", "tools/xcheck.py"):
        assert command in runs, command
