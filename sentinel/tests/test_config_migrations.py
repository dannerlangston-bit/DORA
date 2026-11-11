"""Config-file schema migrations (4.1 Common conventions, SD-47): the lint refuses an unknown version and names the
migration; `sentinel schemas --migrate DIR` (config_migrations.main) brings the files to the known version."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel import config_migrations as cm
from sentinel.canary import load_catalog
from sentinel.lint import lint

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
FLOW = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    dst = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, dst)
    return dst


def bare(root: Path, rel: str) -> None:
    p = root / rel
    p.write_text(yaml.safe_dump(yaml.safe_load(p.read_text())["entries"], sort_keys=False))


def test_known_versions_come_from_the_generated_schemas():
    assert (cm.known_version("flow"), cm.known_version("popup"), cm.known_version("mutant")) == (2, 1, 1)
    assert cm.schema_of_kind("popups") == "popup" and cm.schema_of_kind("flow") == "flow"
    assert cm.schema_of_kind("odd_inputs") is None


def test_paths_and_hints():
    (step,) = cm.migration_path("mutant", None)
    assert (step.from_version, step.to_version) == (None, 1)
    assert cm.migration_path("mutant", 1) == []
    assert cm.migration_path("flow", 1) is None
    assert cm.hint("popup", None).startswith("run `sentinel schemas --migrate <config dir>`")
    assert cm.hint("flow", 1) == ("no migration from schema_version 1 ships with this harness; rewrite the file at "
                                  "version 2")
    data, steps = cm.migrate_data("popup", [{"popup_id": "POP-01"}])
    assert data == {"schema_version": 1, "entries": [{"popup_id": "POP-01"}]} and len(steps) == 1
    with pytest.raises(ValueError, match="no migration ships"):
        cm.migrate_data("flow", {"schema_version": 1})


def test_a_bare_catalog_is_migrated_and_then_lints_clean(root, capsys):
    bare(root, "canaries/mutants.yaml")
    bare(root, "popups/catalog.yaml")
    assert {v.rule for v in lint(root, 1, TODAY).violations} == {"L-MUT-01", "L-POP-01"}
    before = (root / "canaries/mutants.yaml").read_text()
    assert cm.main([str(root), "--dry-run"]) == 0
    assert (root / "canaries/mutants.yaml").read_text() == before
    assert "would migrate canaries/mutants.yaml: mutant None -> 1" in capsys.readouterr().out
    assert cm.main([str(root)]) == 0
    assert lint(root, 1, TODAY).violations == []
    assert len(load_catalog(root / "canaries/mutants.yaml").entries) == 33
    done, stuck = cm.migrate_dir(root)
    assert done == [] and stuck == []                          # already at the known versions


def test_a_version_no_migration_ships_for_is_refused(root, capsys):
    p = root / FLOW
    p.write_text(p.read_text().replace("schema_version: 2", "schema_version: 1", 1))
    found = [v for v in lint(root, 0, TODAY).violations if v.file == FLOW]
    assert [(v.rule, v.path) for v in found] == [("L-FLW-00", "schema_version")]
    assert "no migration from schema_version 1 ships" in found[0].message
    assert cm.main([str(root)]) == 1
    assert f"cannot migrate {FLOW}" in capsys.readouterr().err


def test_main_refuses_a_missing_directory(tmp_path):
    assert cm.main([str(tmp_path / "nowhere")]) == 2
