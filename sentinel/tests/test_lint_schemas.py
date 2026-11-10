"""The JSON Schemas generated from the notation of section 4 (4.1 Schema notation, L-XREF-09)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sentinel.lint import schemas
from sentinel.lint.repo import load_yaml_text
from sentinel.taxonomy import load

SPEC = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections" / "04-inputs-and-schemas.md"
needs_spec = pytest.mark.skipif(not SPEC.exists(), reason="spec checkout not present")


@needs_spec
def test_notation_is_verbatim_from_the_spec():
    """L-XREF-09's source: every embedded block is a code block of section 4, character for character."""
    text = SPEC.read_text(encoding="utf-8")
    for name, block in schemas.NOTATION.items():
        assert block in text, name


@pytest.mark.parametrize("name", schemas.schema_names())
def test_every_schema_is_valid_draft_2020_12(name):
    s = schemas.schema(name)
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(s)


@needs_spec
def test_spec_worked_examples_validate():
    """The worked examples of 4.2 to 4.12 validate (their abbreviated hashes made full length)."""
    text = SPEC.read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.S)
              if b.startswith(("schema_version", "- popup_id", "- mutant_id"))]
    names = ["build", "manifest", "dependency-map", "surface", "workspace", "flow", "flow", "regions", "mask",
             "fixture", "popup", "mutant", "fingerprint"]
    assert len(blocks) == len(names)
    for block, name in zip(blocks, names):
        data = load_yaml_text(re.sub(r'"?sha256:[0-9a-f]+\.\.\.[0-9a-f]+"?', "sha256:" + "a" * 64, block))
        for item in data if isinstance(data, list) else [data]:
            errors = schemas.validate(name, item)
            if item.get("flow_id") == "flow.trade.order-ticket.stock.limit":
                # 4.7 abbreviates this example: "normalize, preconditions and teardown as in the futures example"
                assert {m for _, m in errors} == {f"'{k}' is a required property" for k in ("normalize", "preconditions", "teardown")}
            else:
                assert errors == [], (name, errors)


def test_taxonomy_enums_and_fingerprint_fields():
    mutant = schemas.schema("mutant")
    assert mutant["properties"]["expected"]["properties"]["outcomes"]["items"]["enum"] == load().checkpoint_outcomes
    assert schemas.fingerprint_field_names() == set(schemas.FINGERPRINT_FIELDS_7_4)
    assert len(schemas.FINGERPRINT_FIELDS_7_4) == 22
    flow = schemas.schema("flow")
    assert flow["properties"]["schema_version"] == {"type": "integer", "const": 2}
    checkpoint = next(b["then"] for b in flow["$defs"]["step"]["allOf"] if b["if"]["properties"]["type"]["const"] == "checkpoint")
    assert {"surface_id", "target_window", "data_mode", "checkpoint_id", "regions"} <= set(checkpoint["required"])


def test_notation_parser_details():
    n = schemas.parse_notation("rec:\n  a: {dx: int, dy: int} = {dx: 0, dy: 0}\n  b: ref:surface.id | glob | \"*\"   # c\n  c: list[{x: int, y: str?}]?\n  d: enum[1|2|3]\n")
    a, b, c, d = n.records["rec"]
    assert a.default == {"dx": 0, "dy": 0} and not a.optional
    assert b.type == ("union", (("ref", "surface.id"), ("prim", "glob"), ("const", "*")))
    assert c.optional and c.type[1][1][1].optional
    assert d.type == ("enum", (1, 2, 3))


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
