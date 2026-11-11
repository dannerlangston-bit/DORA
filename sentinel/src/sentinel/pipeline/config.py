"""The run's view of a `sentinel-config` checkout (4.1), read after lint passed.

`materialize` copies a config directory and lays a scenario's overlay on top (the PR's manifest and
build records, copied from the branch at run creation as 4.3 says, or a simulated config edit), so
every run lints and reads exactly the tree it used; the copy stays in the output for inspection.
`SentinelConfig.load` reads the tree with `lint.repo.Repo` (the lint's own loader) into the
inputs scope, the Diff Engine, the Invariant Checker and the canaries take.

The run-start lint (`lint.runstart.lint_at_run_start`) leaves every manifest out of the report that decides
`config_invalid` (4.3), so a manifest the run then treats as missing may be malformed: manifests are keyed by
their file name (`manifests/<candidate build id>.yaml`, the copy made at run creation), never by a field read from
them. A surface's `invariant_exceptions` (4.5) reach the Invariant Checker through `rules`; the run drops the
ones expired at run start (`invariants.active_exceptions`, L-SRF-11).
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .. import invariants as inv
from ..canary import Catalog, parse_catalog
from ..diff.regions import MaskRecord
from ..lint.repo import Repo
from ..scope import DependencyMap, Flow, Registry, Workspace


def materialize(config_dir: str | Path, overlay: Mapping[str, str | bytes], dest: str | Path) -> Path:
    """Copy `config_dir` to `dest` and write each overlay file (repository-relative path -> text or bytes)."""
    src, out = Path(config_dir), Path(dest)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out)
    for rel, text in sorted(overlay.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
    return out


def tree_sha256(root: str | Path) -> str:
    """sha256 over every file's path and bytes, in path order: the config commit the run pins."""
    h = hashlib.sha256()
    base = Path(root)
    for p in sorted(base.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(base).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


@dataclass
class SentinelConfig:
    """Everything the simulated run reads from the config, parsed."""

    root: Path
    repo: Repo
    registry: Registry
    workspaces: list[Workspace]
    flows: list[Flow]
    flow_docs: dict[str, dict]
    dep_map: DependencyMap
    manifests: dict[str, Any]                     # candidate build id (the file name) -> manifest document
    regions: dict[str, dict]                      # checkpoint id -> regions document
    masks: dict[str, MaskRecord]
    surfaces: dict[str, dict]                     # surface id -> registry entry document
    builds: dict[str, dict]                       # build id -> build document
    catalog: Catalog | None
    entitlements: frozenset[str]
    quarantine: frozenset[str]
    send_policy: str
    fixture: dict = field(default_factory=dict)
    send_policy_ref: str = "OPEN"                 # the risk owner decision of policy/send_policy.yaml (6.6, 8.3)

    @classmethod
    def load(cls, root: str | Path) -> "SentinelConfig":
        repo = Repo.load(root)
        surfaces = {d.data["id"]: d.data for d in repo.of("surface")}
        flows = {d.data["flow_id"]: d.data for d in repo.of("flow")}
        fixture = (repo.one("fixture").data if repo.one("fixture") else {}) or {}
        kinds = {a.get("kind") for a in fixture.get("accounts", [])}
        quarantine = repo.one("quarantine")
        q = frozenset(r.get("checkpoint_id") or r.get("flow_id") for r in ((quarantine.data or {}).get("records") or [])
                      ) if quarantine else frozenset()
        send = repo.one("send_policy")
        mutants = Path(root) / "canaries" / "mutants.yaml"
        return cls(
            root=Path(root), repo=repo, registry=Registry.from_entries(surfaces.values()),
            workspaces=[Workspace.from_dict(d.data) for d in repo.of("workspace")],
            flows=[Flow.from_dict(f) for f in flows.values()], flow_docs=flows,
            dep_map=DependencyMap.from_dict(repo.one("dependency_map").data),
            manifests={Path(d.rel).stem: d.data for d in repo.of("manifest")},
            regions={d.data["checkpoint_id"]: d.data for d in repo.of("regions")},
            masks={d.data["mask_id"]: MaskRecord.from_dict(d.data) for d in repo.of("mask")},
            surfaces=surfaces, builds={d.data["build"]["build_id"]: d.data for d in repo.of("build")},
            catalog=parse_catalog(mutants.read_bytes()) if mutants.exists() else None,
            entitlements=frozenset("margin" if k == "margin" else k for k in kinds if k),
            quarantine=frozenset(x for x in q if x),
            send_policy="no_sends" if not ((send.data or {}).get("enabled") if send else False) else "gated",
            fixture=fixture, send_policy_ref=str((send.data or {}).get("decision") or "OPEN") if send else "OPEN")

    # -- helpers ---------------------------------------------------------------------------
    def regions_doc(self, checkpoint_id: str, surface_id: str, kind: str, host: str | None) -> dict | None:
        """The checkpoint's regions file, or for a sweep or split step without one, the file of another
        step of the same surface and host (the module docstring of `sim`)."""
        if checkpoint_id in self.regions:
            return self.regions[checkpoint_id]
        if kind in ("sweep", "split"):
            prefix = f"{kind}/{surface_id}/{host}/" if host else f"{kind}/{surface_id}/"
            for cid in sorted(self.regions):
                if cid.startswith(prefix):
                    return self.regions[cid]
        return None

    def rules(self, surface_id: str, data_mode: str) -> inv.SurfaceRules:
        """The invariant inputs a Surface Registry entry declares (4.5): its tier, required labels, required
        controls (template plus OCR anchor) and its invariant exceptions (expired ones included: the run drops them
        with `invariants.active_exceptions`)."""
        s = self.surfaces.get(surface_id, {})
        labels = tuple(inv.RequiredLabel(str(t)) for t in s.get("labels") or ())
        controls = tuple(inv.RequiredControl(str(c["id"]), ocr_text=c.get("ocr_text"))
                         for c in s.get("controls") or () if c.get("required"))
        exceptions = tuple(inv.InvariantException(str(x["invariant_id"]), str(x.get("owner", "")),
                                                  str(x.get("reason", "")), utc_text(x["expires_at"]))
                           for x in s.get("invariant_exceptions") or () if isinstance(x, dict))
        return inv.SurfaceRules(surface_id, tier=int(s.get("tier", 3)), data_mode=data_mode, labels=labels,
                                controls=controls, exceptions=exceptions)

    def control_ids(self, surface_id: str) -> dict[str, str]:
        """OCR text -> control id of the surface's registry controls (what a template match reports)."""
        return {str(c["ocr_text"]): str(c["id"]) for c in self.surfaces.get(surface_id, {}).get("controls") or ()
                if c.get("ocr_text")}

    def flow_step(self, flow_id: str, step_id: str) -> dict:
        for st in self.flow_docs.get(flow_id, {}).get("steps") or ():
            if st.get("id") == step_id:
                return st
        return {}

    def target_window(self, surface_id: str) -> str:
        tw = self.surfaces.get(surface_id, {}).get("target_window") or {}
        return str(tw.get("kind", "main"))


def utc_text(value: Any) -> str:
    """A YAML timestamp (text or datetime) as UTC ISO text, so `invariants.active_exceptions` compares like with
    like."""
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def parse_time(text: str) -> datetime:
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def as_dict(obj: Any) -> Any:
    """YAML-loaded data with dates as strings (manifest dicts for the Reporter)."""
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [as_dict(v) for v in obj]
    return obj
