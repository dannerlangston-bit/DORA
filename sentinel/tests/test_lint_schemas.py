"""The JSON Schemas generated from the notation of section 4 (4.1 Schema notation, L-XREF-09, SD-35)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sentinel.lint import schemas
from sentinel.lint.repo import load_yaml_text
from sentinel.taxonomy import load

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT.parent / "sentinel-spec" / "sections" / "04-inputs-and-schemas.md"
needs_spec = pytest.mark.skipif(not SPEC.exists(), reason="spec checkout not present")
# the abbreviated hashes of the worked examples (`sha256:9a3f...c1e2`, `"sha256:4c..."`) made full length
_ABBREVIATED = re.compile(r'"?sha256:[0-9a-f]+\.\.\.(?:[0-9a-f]+)?"?')


@needs_spec
def test_notation_is_verbatim_from_the_spec():
    """L-XREF-09's source: every embedded block is a code block of section 4, character for character."""
    text = SPEC.read_text(encoding="utf-8")
    for name, block in schemas.NOTATION.items():
        assert block in text, name


@needs_spec
def test_every_schema_header_of_section_4_has_a_notation_entry():
    """04b-schemas-outcome-decision: each `# schemas/<name>.schema.json` part of section 4 is mirrored and
    generates a schema file of that name, and no spec schema exists without one."""
    text = SPEC.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    headers = {m for b in blocks for m in re.findall(r"(?m)^# (?:schemas/|\S+\s+\(schemas/)([a-z0-9-]+)\.schema\.json", b)}
    assert headers == set(schemas.NOTATION) - {"checkpoint_step", "assertion"}
    assert headers == set(schemas.schema_names(spec_only=True))
    assert {"outcome", "decision", "quarantine", "calibration", "branches"} <= headers
    assert len(headers) == 38


@pytest.mark.parametrize("name", schemas.schema_names())
def test_every_schema_is_valid_draft_2020_12(name):
    s = schemas.schema(name)
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(s)


@needs_spec
def test_spec_worked_examples_validate():
    """The worked examples of 4.2 to 4.14 validate: catalogs entry by entry, the 4.13 ladder overlap and the 4.14
    decision dec-000812 included."""
    text = SPEC.read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.S)
              if b.startswith(("schema_version", "- popup_id", "- mutant_id", "outcome_id", "decision_id"))]
    names = ["build", "manifest", "dependency-map", "surface", "workspace", "flow", "flow", "regions", "mask",
             "fixture", "popup#popup", "mutant#mutant", "fingerprint", "outcome", "decision"]
    assert len(blocks) == len(names)
    for block, name in zip(blocks, names):
        data = load_yaml_text(_ABBREVIATED.sub("sha256:" + "a" * 64, block))
        for item in data if isinstance(data, list) else [data]:
            errors = schemas.validate(name, item)
            if item.get("flow_id") == "flow.trade.order-ticket.stock.limit":
                # 4.7 abbreviates this example: "normalize, preconditions and teardown as in the futures example"
                assert {m for _, m in errors} == {f"'{k}' is a required property" for k in ("normalize", "preconditions", "teardown")}
            else:
                assert errors == [], (name, errors)


def test_outcome_schema_carries_the_four_record_shapes():
    """4.13: one schema file, four shapes; each validates on its own as `outcome#<record>`."""
    s = schemas.schema("outcome")
    assert s["title"] == "outcome (checkpoint_outcome)"
    assert {"step_outcome", "run_outcome", "finding"} <= set(s["$defs"])
    finding = {"finding_id": "fnd-run-1-000001", "run_id": "run-1", "kind": "canary_miss", "description": "d",
               "evidence": {"artifacts": []}, "first_seen_run_id": "run-1", "created_at": "2026-11-09T14:21:44-05:00"}
    assert schemas.validate("outcome#finding", finding) == []
    assert any("kind" in loc for loc, _ in schemas.validate("outcome#finding", {**finding, "kind": "ignored"}))
    run = schemas.schema("outcome#run_outcome")["$defs"]["run_outcome"]
    assert run["properties"]["counts"]["propertyNames"] == {"enum": load().checkpoint_outcomes}
    with pytest.raises(KeyError):
        schemas.schema("outcome#nothing")


def test_decision_schema_signature_and_refs():
    d = schemas.schema("decision")["properties"]
    assert d["finding_ref"] == {"type": "string", "pattern": schemas.SLUG_PATTERN}       # ref:finding.finding_id
    region = d["signature"]["properties"]["region_id"]["anyOf"]
    assert {"type": "string", "pattern": schemas.GRID_CELL} in region                    # id | grid_cell (9.7)
    assert d["status"]["enum"] == load().decision_statuses
    assert d["source"]["enum"] == load().decision_sources


def test_catalog_files_and_entries():
    """4.10, 4.11: popup.schema.json and mutant.schema.json describe the catalog file; one entry is `#popup`."""
    assert schemas.SPEC_SCHEMAS["popup"] == "popup_catalog" and schemas.SPEC_SCHEMAS["mutant"] == "mutant_catalog"
    entry = schemas.schema("popup")["$defs"]["popup"]["properties"]
    assert entry["action"]["enum"] == load().popup_actions
    assert entry["retired"]["default"] is False                                           # SD-95
    assert schemas.validate("popup", {"schema_version": 1, "entries": []}) == []
    assert schemas.validate("popup#popup", {"popup_id": "POP-01"}) != []


def test_taxonomy_enums_and_fingerprint_fields():
    mutant = schemas.schema("mutant")["$defs"]["mutant"]
    assert mutant["properties"]["expected"]["properties"]["outcomes"]["items"]["enum"] == load().checkpoint_outcomes
    assert mutant["properties"]["class"]["enum"] == load().list("mutant_classes")
    regions = schemas.schema("regions")["$defs"]["region"]["properties"]
    assert regions["money_role"]["enum"] == load().money_roles                             # SD-28
    assert schemas.schema("quarantine")["$defs"]["quarantine_record"]["properties"]["reason"]["enum"] == load().quarantine_reasons
    assert schemas.fingerprint_field_names() == set(schemas.FINGERPRINT_FIELDS_7_4)
    assert len(schemas.FINGERPRINT_FIELDS_7_4) == 23
    flow = schemas.schema("flow")
    assert flow["properties"]["schema_version"] == {"type": "integer", "const": 2}
    checkpoint = next(b["then"] for b in flow["$defs"]["step"]["allOf"] if b["if"]["properties"]["type"]["const"] == "checkpoint")
    assert {"surface_id", "target_window", "data_mode", "checkpoint_id", "regions"} <= set(checkpoint["required"])


@needs_spec
def test_fingerprint_fields_equal_the_7_4_table():
    """L-FPR-01: the constant is the 7.4 table, row for row (04b-spec-mirror-constants)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import spec_tables
    assert list(schemas.FINGERPRINT_FIELDS_7_4) == spec_tables.fingerprint_fields_7_4()


def test_notation_parser_details():
    n = schemas.parse_notation("rec:\n  a: {dx: int, dy: int} = {dx: 0, dy: 0}\n  b: ref:surface.id | glob | \"*\"   # c\n  c: list[{x: int, y: str?}]?\n  d: enum[1|2|3]\n")
    a, b, c, d = n.records["rec"]
    assert a.default == {"dx": 0, "dy": 0} and not a.optional
    assert b.type == ("union", (("ref", "surface.id"), ("prim", "glob"), ("const", "*")))
    assert c.optional and c.type[1][1][1].optional
    assert d.type == ("enum", (1, 2, 3))


def test_notation_parser_constructs_of_4_13_and_4_14():
    n = schemas.parse_notation("rec:\n  box: {x,y,w,h}\n  kind: enum[finding kinds of taxonomy.yaml]\n"
                               "  counts: map[checkpoint outcome enum, int]\n  cell: id | grid_cell\n")
    box, kind, counts, cell = n.records["rec"]
    assert box.type == ("obj", tuple(schemas.Field(k, ("prim", "int")) for k in "xywh"))
    assert kind.type == ("special", "finding_kind")
    assert counts.type == ("map", ("special", "checkpoint_outcome"), ("prim", "int"))
    assert cell.type == ("union", (("prim", "id"), ("prim", "grid_cell")))


def test_validate_reports_locations():
    errors = schemas.validate("surface", {"schema_version": 1, "id": "Bad_Id"})
    assert ("id", "'Bad_Id' does not match '^[a-z0-9]+(?:[.-][a-z0-9]+)*$'") in errors
    step = {"id": "s1", "type": "drag", "from": "a.b", "to": {"x": 1, "y": 2}, "steps": 10, "hold_ms": 5, "keys": []}
    assert any("keys" in m for _, m in schemas.validate("flow", {"schema_version": 2, "steps": [step]}))


def test_loader_keeps_on_and_timestamps_as_strings():
    d = load_yaml_text("state: on\nat: 2026-11-09T13:42:10-05:00\nday: 2026-11-20\nok: true\n")
    assert d == {"state": "on", "at": "2026-11-09T13:42:10-05:00", "day": "2026-11-20", "ok": True}


def test_write_and_check_schemas(tmp_path):
    written = schemas.write_schemas(tmp_path)
    assert len(written) == len(schemas.schema_names())
    assert schemas.check_schemas(tmp_path) == []
    p = tmp_path / "surface.schema.json"
    data = json.loads(p.read_text())
    data["properties"].pop("title")
    p.write_text(json.dumps(data))
    (tmp_path / "extra.schema.json").write_text("{}")
    assert {name for name, _ in schemas.check_schemas(tmp_path)} == {"surface.schema.json", "extra.schema.json"}


def test_committed_schemas_are_current():
    """04b-schemas-committed-ci: sentinel/schemas/ holds exactly what the notation generates (SD-35); the CI job
    runs the same check as `sentinel schemas --check`."""
    assert schemas.check_schemas(ROOT / "schemas") == []
    r = subprocess.run([sys.executable, "-m", "sentinel.cli", "schemas", "--check"], capture_output=True, text=True,
                       cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
