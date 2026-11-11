"""The report UI pages of 9.5 over a read-only Central Store connection (8.7), rendered as escaped HTML.

Every function takes a read-only `sqlite3.Connection` (`store.connect_reader`: `mode=ro`, `query_only`, `hamming()`
registered) and returns a `Page`; `ui.app` wraps it in the layout with the read-only banner and serves it. Screen
text and model text (descriptions, OCR strings, reviewer output, ticket titles, reasons) are data: every value goes
through `e()` and nothing from a row becomes markup (9.1). Lists paginate at 200 rows (8.7).

Pages: run list, run detail, run comparison, coverage matrix (per run and the latest per run type), checkpoint
detail (1:1 crops, flicker at 2 Hz, heatmap, connected-component boxes, region overlay, Oracle values, step events,
re-runs, flake score, 30-run history, the live decision lookup), finding page, triage queue (9.2 ranking, SLA clock,
assignee, proposals awaiting a second person), decisions, baseline approval queue, canary panel, metrics (with a CSV
export), the action form and the artifact endpoint `/a/<sha256>` with its deleted, quarantined and evidence-missing
pages.

Choices where the spec is ambiguous or silent (reported to the coordinator):
- Provenance (9.5): a run's page shows that run's harness commit, config commit, VM image ids (the snapshot ids of
  its shards' rigs), build pair, reviewer model ids with prompt hashes and the reviewer version hash; a page over
  several runs (lists, queues) shows the provenance of the newest run it lists, or of the newest run in the store
  when it lists none.
- The triage queue lists open, non-synthetic, not-held findings without a decision; it ranks them by tier, then the
  9.2 kind order (`KIND_RANK`), then reviewer confidence descending, then novelty (a finding first seen in its own
  run first), then age. The SLA clock is the current assignment's `sla_due_at` when the SLA job set one, else the
  `sla` hook's due time, else the time the clock started (assignment or creation); a proposal shows its 4-business-
  hour clock from `proposed_at` the same way.
- Region overlay: a region's anchored bounds are drawn where a diff component inside it reveals them (its window box
  minus its region-relative box); the other regions are listed with their landmark-relative rectangles.
- The artifact endpoint answers 200 with the bytes (media type from `artifacts`, `Cache-Control: public,
  max-age=31536000, immutable`), 410 for an object retention removed (every copy deleted), 403 for a quarantined
  set, 404 with the evidence-missing page for a hash whose `audit_state` is not `ok`, whose bytes are gone or
  corrupt, or that an outcome names without an `artifacts` row; that page names the `artifact_missing` finding whose
  evidence lists the hash, or says none records it yet.
- The baseline queue shows each candidate beside the approved baseline of its checkpoint and lineage; the nearest
  baseline by perceptual hash comes from the baseline package and is shown when the candidate's source finding
  carries it as `evidence.closest_baseline`.
- Metrics: the run, outcome and finding counts of the window (`days`, default 30) and the Nightly run's
  `summary_json.metrics` when the metrics job wrote it (9.11); the CSV export is the same rows.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlencode

from ..records import Signature
from ..report.actions import RULES
from ..store.artifacts import QUARANTINE_MARKER, SHA256_RE
from ..store.store import lookup_decisions

PAGE_SIZE = 200                     # 8.7: lists paginate at 200 rows
FLICKER_MS = 500                    # 9.1: flicker at 2 Hz, each image shown 500 ms
HISTORY_RUNS = 30                   # 9.5: history over 30 runs
EXPIRING_DAYS = 7                   # 9.5 decisions expiring in 7 days
SUSPECT_REOPENS = 2                 # 9.5, 9.7 suspect decisions
IMMUTABLE = "public, max-age=31536000, immutable"     # 8.7
# 9.2 Unexpected ranking, within a tier: the finding kinds in order (lower first)
KIND_RANK = {"app_crash": 0, "invariant_violation": 1, "assertion_failed": 2, "collateral": 3, "needs_human": 5,
             "landmark_missing": 6, "unregistered_surface": 7, "needs_baseline": 9, "locator_failure": 10,
             "dependency_map_gap": 11, "manifest_missing": 12, "unknown_popup": 13}
FINDING_ACTIONS = ("confirm_bug", "mark_intended", "request_mask", "noise_signature")


@dataclass
class Page:
    """One rendered page body with its title, provenance and status."""
    title: str
    body: str
    provenance: Mapping[str, str] = field(default_factory=dict)
    status: int = 200


class NotFound(LookupError):
    """The item a page names does not exist (404)."""


def e(v: Any) -> str:
    """HTML-escape any value (9.1: screen and model text are data)."""
    return html.escape("" if v is None else str(v), quote=True)


def a(href: str, label: Any) -> str:
    return f'<a href="{e(href)}">{e(label)}</a>'


def q(s: str) -> str:
    return quote(s, safe="")


def table(head: Sequence[str], rows: Iterable[Sequence[str]], *, cls: str = "") -> str:
    """A table; cells are HTML already (build them with `e` and `a`)."""
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<table class="{e(cls)}"><thead><tr>' + "".join(f"<th>{e(h)}</th>" for h in head)
            + f"</tr></thead><tbody>{body}</tbody></table>")


def dl(items: Iterable[tuple[str, Any]]) -> str:
    return "<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in items) + "</dl>"


def _rows(conn: sqlite3.Connection, sql: str, args: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def _one(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def _page(n: Any) -> int:
    try:
        return max(1, int(n))
    except (TypeError, ValueError):
        return 1


def _pager(path: str, page: int, more: bool, params: Mapping[str, Any] = ()) -> str:
    links = []
    base = {k: v for k, v in dict(params).items() if v not in (None, "")}
    if page > 1:
        links.append(a(f"{path}?{urlencode({**base, 'page': page - 1})}", f"previous {PAGE_SIZE}"))
    if more:
        links.append(a(f"{path}?{urlencode({**base, 'page': page + 1})}", f"next {PAGE_SIZE}"))
    return f'<p class="pager">{" | ".join(links)}</p>' if links else ""


def _limit(page: int) -> tuple[int, int]:
    return PAGE_SIZE + 1, (page - 1) * PAGE_SIZE


# ---------------------------------------------------------------------------------------------------------------
# provenance (9.5)

def provenance(conn: sqlite3.Connection, run_id: str | None) -> dict[str, str]:
    """The run's provenance: harness and config commits, VM image ids, build pair, model ids and prompt hashes."""
    if run_id is None:
        return {}
    r = _one(conn, "SELECT r.*, p.candidate_build_id, p.base_build_id FROM runs r LEFT JOIN build_pairs p "
                   "ON p.pair_id = r.pair_id WHERE r.run_id = ?", (run_id,))
    if r is None:
        return {}
    images = sorted({x[0] for x in _rows(conn, "SELECT DISTINCT g.snapshot_id FROM shards s JOIN rigs g "
                                               "ON g.rig_id = s.rig_id WHERE s.run_id = ?", (run_id,))})
    models = sorted({f"{m['model_id']} (prompt {m['prompt_hash']})" for m in _rows(
        conn, "SELECT DISTINCT model_id, prompt_hash FROM reviews WHERE run_id = ?", (run_id,))})
    out = {"run": run_id, "harness commit": r["harness_sha"], "config commit": r["config_commit"],
           "VM image id": ", ".join(images) or "none recorded",
           "build pair": f"{r['candidate_build_id']} vs {r['base_build_id']}" if r["candidate_build_id"] else "none",
           "model ids, prompt hash": "; ".join(models) or "no reviewer calls"}
    if r["reviewer_version_sha256"]:
        out["reviewer version"] = r["reviewer_version_sha256"]
    return out


def _newest(run_ids: Iterable[str | None], conn: sqlite3.Connection) -> dict[str, str]:
    """The provenance of the newest of `run_ids`, or of the newest run in the store when they are none."""
    ids = [r for r in run_ids if r]
    marks = ",".join("?" * len(ids))
    where = f"WHERE run_id IN ({marks})" if ids else ""
    row = _one(conn, f"SELECT run_id FROM runs {where} ORDER BY started_at DESC LIMIT 1", ids)
    return provenance(conn, row[0]) if row else {}


# ---------------------------------------------------------------------------------------------------------------
# runs

def _counts(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    return {r[0]: r[1] for r in _rows(conn, "SELECT outcome, count(*) FROM outcomes WHERE run_id = ? AND "
                                            "synthetic = 0 GROUP BY outcome ORDER BY outcome", (run_id,))}


def _duration(r: sqlite3.Row) -> str:
    if not r["finished_at"]:
        return "running"
    s = int((datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds())
    return f"{s // 60} min {s % 60} s"


def _undelivered(conn: sqlite3.Connection) -> str:
    """9.10: pages no channel accepted within 5 minutes (`system_state.alert_undelivered`), listed above the runs,
    since this local page is always available."""
    row = _rows(conn, "SELECT value FROM system_state WHERE key = 'alert_undelivered'")
    lost = json.loads(row[0]["value"]) if row else []
    if not lost:
        return ""
    items = "".join(f"<li>{e(str(x.get('rule')))} ({e(str(x.get('key')))}), run {e(str(x.get('run_id') or '-'))}, "
                    f"due {e(str(x.get('due_at')))}, found undelivered {e(str(x.get('detected_at')))}</li>"
                    for x in lost)
    return (f'<div class="banner" role="alert"><p>alert_undelivered: {len(lost)} page(s) no channel accepted</p>'
            f"<ul>{items}</ul></div>")


def run_list(conn: sqlite3.Connection, params: Mapping[str, str]) -> Page:
    """Runs by type: outcome, build pair, counts per outcome, duration, cost, open items by tier, outbox state."""
    page = _page(params.get("page"))
    kind = params.get("type") or None
    limit, offset = _limit(page)
    rows = _rows(conn, "SELECT r.*, p.candidate_build_id, p.base_build_id FROM runs r LEFT JOIN build_pairs p ON "
                       "p.pair_id = r.pair_id WHERE (:t IS NULL OR r.run_type = :t) ORDER BY r.started_at DESC, "
                       "r.run_id LIMIT :l OFFSET :o", {"t": kind, "l": limit, "o": offset})
    more, rows = len(rows) > PAGE_SIZE, rows[:PAGE_SIZE]
    out = []
    for r in rows:
        rid = r["run_id"]
        cost = _one(conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM model_usage WHERE run_id = ?", (rid,))[0]
        tiers = _rows(conn, "SELECT tier, count(*) FROM findings WHERE run_id = ? AND triage_state = 'open' AND "
                            "synthetic = 0 AND held = 0 GROUP BY tier ORDER BY tier", (rid,))
        outbox = _rows(conn, "SELECT state, count(*) FROM outbox WHERE run_id = ? GROUP BY state ORDER BY state",
                       (rid,))
        undelivered = _one(conn, "SELECT count(*) FROM outbox WHERE run_id = ? AND kind IN ('page','slack') AND "
                                 "state IN ('failed','abandoned')", (rid,))[0]
        out.append([a(f"/runs/{q(rid)}", rid), e(r["run_type"]), e(r["outcome"] or r["lifecycle"]),
                    e(f"{r['candidate_build_id']} vs {r['base_build_id']}" if r["candidate_build_id"] else "-"),
                    e(", ".join(f"{k} {v}" for k, v in _counts(conn, rid).items()) or "-"), e(_duration(r)),
                    e(f"${cost:.2f}"), e(", ".join(f"tier {t[0]}: {t[1]}" for t in tiers) or "none"),
                    e(", ".join(f"{s[0]} {s[1]}" for s in outbox) or "-"),
                    e(f"alert_undelivered {undelivered}" if undelivered else "-")])
    types = " | ".join(a("/runs?type=" + t, t) for t in ("pr", "merge", "rc", "nightly", "calibration"))
    body = (f"<h1>Runs</h1>{_undelivered(conn)}<p>{types}"
            f"</p>" + table(["Run", "Type", "Outcome", "Build pair", "Counts", "Duration", "Cost", "Open items",
                             "Outbox", "Alerts"], out) + _pager("/runs", page, more, {"type": kind}))
    return Page("Runs", body, _newest((r["run_id"] for r in rows), conn))


def run_detail(conn: sqlite3.Connection, run_id: str, params: Mapping[str, str], *, writable: bool) -> Page:
    """The run page: result, abort facts, the static report and PR comment, alerts with acknowledgements, shards
    and import state, outcomes (tier, then outcome), findings, outbox jobs (8.7, 9.5)."""
    r = _one(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if r is None:
        raise NotFound(f"run {run_id}")
    summary = json.loads(r["summary_json"] or "{}")
    page = _page(params.get("page"))
    limit, offset = _limit(page)
    parts = [f"<h1>{e(r['run_type'])} run {e(run_id)}</h1>",
             dl([("Result", f"{r['outcome'] or r['lifecycle']}" + (f" (reason: {r['abort_reason']})"
                                                                   if r["abort_reason"] else "")),
                 ("Review state", r["review_state"]), ("Duration", _duration(r)),
                 ("Counts", ", ".join(f"{k} {v}" for k, v in _counts(conn, run_id).items()) or "none")])]
    if r["check_override_by"]:
        parts.append(f'<p class="warn">Check overridden by {e(r["check_override_by"])}: '
                     f'{e(r["check_override_reason"])}</p>')
    refs = summary.get("refs") or {}
    links = [a(f"/a/{refs[k]}", label) for k, label in (("report_sha256", "full report (9.4)"),
                                                         ("comment_sha256", "PR comment"))
             if refs.get(k) and SHA256_RE.match(str(refs[k]))]
    links.append(a(f"/runs/{q(run_id)}/coverage", "coverage matrix"))
    parts.append("<p>" + " | ".join(links) + "</p>")
    if summary.get("abort"):
        parts.append("<h2>Abort</h2>" + dl(sorted((k, v if isinstance(v, str) else json.dumps(v))
                                                  for k, v in summary["abort"].items())))
    acks = summary.get("acknowledgements") or {}
    alerts = summary.get("alerts") or []
    parts.append("<h2>Alerts</h2>" + (table(["Alert", "Priority", "Detail", "Acknowledged"], [
        [e(al.get("rule") or al.get("alert")), e(al.get("priority")), e(al.get("detail") or al.get("closed_as")),
         e(acks.get(str(al.get("rule") or al.get("alert"))) or "no")] for al in alerts]) if alerts else "<p>none</p>"))
    shards = _rows(conn, "SELECT * FROM shards WHERE run_id = ? ORDER BY shard_id", (run_id,))
    parts.append("<h2>Shards</h2>" + table(["Shard", "Rig", "State", "Outcome", "Imported"], [
        [e(s["shard_id"]), e(s["rig_id"]), e(s["state"]), e(s["shard_outcome"]), e(s["imported_at"])]
        for s in shards]))
    outs = _rows(conn, "SELECT o.*, c.tier FROM outcomes o JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
                       "WHERE o.run_id = ? AND o.synthetic = 0 ORDER BY c.tier, o.outcome, o.checkpoint_id "
                       "LIMIT ? OFFSET ?", (run_id, limit, offset))
    more, outs = len(outs) > PAGE_SIZE, outs[:PAGE_SIZE]
    parts.append("<h2>Outcomes</h2>" + table(["Tier", "Checkpoint", "Outcome", "Reason", "Decision"], [
        [e(o["tier"]), a(f"/cp/{q(o['checkpoint_id'])}?run={q(run_id)}", o["checkpoint_id"]), e(o["outcome"]),
         e(o["reason"]), e(f"dec-{o['decision_id']:06d}" if o["decision_id"] else "")] for o in outs])
                 + _pager(f"/runs/{q(run_id)}", page, more))
    fs = _rows(conn, "SELECT * FROM findings WHERE run_id = ? AND synthetic = 0 ORDER BY tier, kind, natural_key "
                     "LIMIT ?", (run_id, PAGE_SIZE))
    parts.append("<h2>Findings</h2>" + table(["Tier", "Finding", "Kind", "Triage", "Actions"], [
        [e(f["tier"]), a(f"/f/{q(f['natural_key'])}", f["natural_key"]), e(f["kind"]), e(f["triage_state"]),
         _action_links(f["natural_key"], writable)] for f in fs]))
    jobs = _rows(conn, "SELECT * FROM outbox WHERE run_id = ? ORDER BY job_id LIMIT ?", (run_id, PAGE_SIZE))
    parts.append("<h2>Outbox</h2>" + table(["Job", "Kind", "Destination", "State", "Attempts", "Last error", ""], [
        [e(j["job_id"]), e(j["kind"]), e(j["destination"]), e(j["state"]), e(j["attempts"]), e(j["last_error"]),
         (_button(f"/act/close_outbox_job?job={j['job_id']}", "close", writable)
          if j["state"] == "abandoned" and not j["closed_by"] else e(j["closed_by"] or ""))] for j in jobs]))
    parts.append(f'<form method="get" action="/runs/compare"><input type="hidden" name="a" value="{e(run_id)}">'
                 f'Compare with run <input name="b"> <button>compare</button></form>')
    return Page(f"Run {run_id}", "".join(parts), provenance(conn, run_id))


def compare_runs(conn: sqlite3.Connection, run_a: str, run_b: str) -> Page:
    """Two runs side by side: the outcome of every checkpoint either scoped, the differing rows first."""
    for rid in (run_a, run_b):
        if _one(conn, "SELECT 1 FROM runs WHERE run_id = ?", (rid,)) is None:
            raise NotFound(f"run {rid}")
    rows = _rows(conn, "SELECT s.checkpoint_id, (SELECT outcome FROM outcomes WHERE run_id = :a AND checkpoint_id = "
                       "s.checkpoint_id) AS oa, (SELECT outcome FROM outcomes WHERE run_id = :b AND checkpoint_id = "
                       "s.checkpoint_id) AS ob FROM (SELECT checkpoint_id FROM run_scope WHERE run_id IN (:a, :b) "
                       "GROUP BY checkpoint_id) s ORDER BY s.checkpoint_id", {"a": run_a, "b": run_b})
    rows = sorted(rows, key=lambda r: (r["oa"] == r["ob"], r["checkpoint_id"]))
    body = f"<h1>Compare {e(run_a)} and {e(run_b)}</h1>" + table(
        ["Checkpoint", run_a, run_b], [[e(r["checkpoint_id"]), e(r["oa"] or "not scoped"), e(r["ob"] or "not scoped")]
                                       for r in rows[:PAGE_SIZE]])
    return Page("Compare runs", body, provenance(conn, run_a))


# ---------------------------------------------------------------------------------------------------------------
# coverage (2.12, 9.5)

def _matrix(cells: list[Mapping[str, Any]], tier: str | None) -> str:
    cells = [c for c in cells if not tier or str(c.get("tier")) == tier]
    cols = sorted({(c["persona"], c.get("entitlement") or "") for c in cells})
    grid: dict[tuple, dict[tuple, Mapping[str, Any]]] = {}
    for c in cells:
        grid.setdefault((c.get("tier"), c["surface_id"]), {})[(c["persona"], c.get("entitlement") or "")] = c
    rows = []
    for (t, surface), by in sorted(grid.items(), key=lambda kv: (kv[0][0] or 9, kv[0][1])):
        line = [e(t), e(surface)]
        red = any(c["state"] == "not_registered" for c in by.values())
        for col in cols:
            c = by.get(col)
            if c is None:
                line.append(e("-"))
            elif c["state"] == "not_run":
                line.append(e(f"not_run ({c.get('not_run_reason')})"))
            else:
                line.append(e(c["state"] if c["state"] != "covered" else f"covered ({c.get('worst_outcome')})"))
        rows.append([f'<span class="red">{x}</span>' if red else x for x in line])
    return table(["Tier", "Surface"] + [f"{p} / {ent}" if ent else p for p, ent in cols], rows, cls="matrix")


def coverage(conn: sqlite3.Connection, run_id: str | None, params: Mapping[str, str]) -> Page:
    """Surface x persona x entitlement for one run, or the newest merge, RC and Nightly runs (9.5)."""
    tier = params.get("tier") or None
    if run_id is None:
        latest = [r[0] for r in _rows(conn, "SELECT (SELECT run_id FROM runs WHERE run_type = t ORDER BY started_at "
                                            "DESC LIMIT 1) FROM (SELECT 'merge' AS t UNION ALL SELECT 'rc' UNION ALL "
                                            "SELECT 'nightly')") if r[0]]
        body = "<h1>Coverage matrix</h1><p>" + " | ".join(a(f"/runs/{q(x)}/coverage", x) for x in latest) + "</p>"
        if latest:
            body += coverage(conn, latest[0], params).body
        return Page("Coverage matrix", body, _newest(latest, conn))
    r = _one(conn, "SELECT run_type, summary_json FROM runs WHERE run_id = ?", (run_id,))
    if r is None:
        raise NotFound(f"run {run_id}")
    cells = [dict(c) for c in _rows(conn, "SELECT * FROM coverage_cells WHERE run_id = ?", (run_id,))]
    if not cells:                                          # a PR run keeps its matrix in the summary (8.3)
        cov = (json.loads(r["summary_json"] or "{}")).get("coverage") or {}
        cells = [dict(c) for c in cov.get("cells") or () if isinstance(c, Mapping) and "surface_id" in c]
    filt = " | ".join(a(f"/runs/{q(run_id)}/coverage?tier={t}", f"tier {t}") for t in (1, 2, 3))
    body = f"<h2>Coverage of {e(run_id)}</h2><p>{filt}</p>" + _matrix(cells, tier)
    return Page(f"Coverage {run_id}", body, provenance(conn, run_id))


# ---------------------------------------------------------------------------------------------------------------
# checkpoint and finding

def _record(conn: sqlite3.Connection, run_id: str, checkpoint_id: str) -> dict:
    r = _one(conn, "SELECT record_json FROM outcome_records WHERE run_id = ? AND checkpoint_id = ?",
             (run_id, checkpoint_id))
    return json.loads(r[0]) if r else {}


def _img(conn: sqlite3.Connection, sha: str | None, alt: str) -> str:
    """An artifact image at 1:1: width equals the image's pixel width, pixelated (9.1)."""
    if not sha or not SHA256_RE.match(str(sha)):
        return f'<span class="muted">{e(alt)}: none</span>'
    w = _one(conn, "SELECT width FROM artifacts WHERE sha256 = ?", (sha,))
    width = f' width="{int(w[0])}"' if w and w[0] else ""
    return f'<img class="px" alt="{e(alt)}" src="/a/{e(sha)}"{width}>'


def _boxes(record: Mapping[str, Any], regions: list[sqlite3.Row]) -> str:
    """The connected-component boxes and the regions whose anchored bounds a component reveals (9.5)."""
    comps = ((record.get("diff") or {}).get("components") or [])
    out = []
    origin: dict[str, tuple[int, int]] = {}
    for c in comps:
        x, y, w, h = c["bbox"]
        out.append(f'<div class="box component" style="left:{int(x)}px;top:{int(y)}px;width:{int(w)}px;'
                   f'height:{int(h)}px" title="component {e(c.get("area"))} px in {e(c.get("region_id"))}"></div>')
        if c.get("region_id") and c.get("bbox_region"):
            origin[c["region_id"]] = (int(x) - int(c["bbox_region"][0]), int(y) - int(c["bbox_region"][1]))
    for g in regions:
        if g["region_id"] in origin:
            ox, oy = origin[g["region_id"]]
            out.append(f'<div class="box region {e(g["kind"])}" style="left:{ox}px;top:{oy}px;width:{g["w"]}px;'
                       f'height:{g["h"]}px" title="{e(g["region_id"])} ({e(g["kind"])})"></div>')
    return "".join(out)


def _resolve(conn: sqlite3.Connection, ref: str, run: str | None) -> tuple[str, str, sqlite3.Row | None]:
    """A /cp/ reference (a finding id, a checkpoint id, or flow/step) -> (run id, checkpoint id, finding row)."""
    f = _one(conn, "SELECT * FROM findings WHERE natural_key = ?", (ref,))
    if f is not None and f["checkpoint_id"]:
        return f["run_id"], f["checkpoint_id"], f
    cid = ref if _one(conn, "SELECT 1 FROM checkpoints WHERE checkpoint_id = ?", (ref,)) else None
    if cid is None and "/" in ref:
        flow, _, step = ref.rpartition("/")
        row = _one(conn, "SELECT checkpoint_id FROM checkpoints WHERE flow_id = ? AND step_id = ?", (flow, step))
        cid = row[0] if row else None
    if cid is None:
        raise NotFound(f"checkpoint {ref}")
    if run is None:
        row = _one(conn, "SELECT o.run_id FROM outcomes o JOIN runs r ON r.run_id = o.run_id WHERE o.checkpoint_id = ? "
                         "AND o.synthetic = 0 ORDER BY r.started_at DESC LIMIT 1", (cid,))
        if row is None:
            raise NotFound(f"no outcome of {cid}")
        run = row[0]
    return run, cid, f


def checkpoint_detail(conn: sqlite3.Connection, ref: str, params: Mapping[str, str], *, writable: bool) -> Page:
    """The checkpoint page of 9.5."""
    run_id, cid, f = _resolve(conn, ref, params.get("run"))
    o = _one(conn, "SELECT o.*, c.tier, r.config_commit FROM outcomes o JOIN checkpoints c ON c.checkpoint_id = "
                   "o.checkpoint_id JOIN runs r ON r.run_id = o.run_id WHERE o.run_id = ? AND o.checkpoint_id = ?",
             (run_id, cid))
    if o is None:
        raise NotFound(f"no outcome of {cid} in {run_id}")
    rec = _record(conn, run_id, cid)
    arts = rec.get("artifacts") or {}
    base, cand = arts.get("base") or o["base_capture_sha256"], arts.get("candidate") or o["cand_capture_sha256"]
    heat = arts.get("heatmap") or o["heatmap_sha256"]
    regions = _rows(conn, "SELECT * FROM regions WHERE checkpoint_id = ? AND config_commit = ? ORDER BY region_id",
                    (cid, o["config_commit"]))
    parts = [f"<h1>Checkpoint {e(cid)}</h1>",
             dl([("Run", run_id), ("Tier", o["tier"]), ("Outcome", o["outcome"] + (f" (reason: {o['reason']})"
                                                                                  if o["reason"] else "")),
                 ("Lower results (evidence)", "; ".join(
                     f"{x.get('outcome')} ({x.get('reason')})" for x in rec.get("lower_results") or ()) or "none"),
                 ("Settle timing", f"{o['settle_ms']} ms" if o["settle_ms"] is not None else "not recorded"),
                 ("Re-run results", f"{o['recaptures']} re-captures, {o['stability'] or 'unknown'}"
                                    + (f" ({o['stability_cause']})" if o["stability_cause"] else "")),
                 ("Landmark match score", (f["evidence_json"] and json.loads(f["evidence_json"]).get("landmark_score"))
                  if f is not None else None)])]
    parts.append('<h2>Base and candidate at 1:1</h2><div class="pair">' + _img(conn, base, "base")
                 + _img(conn, cand, "candidate") + "</div>")
    parts.append(f'<h2>Flicker</h2><div class="flicker" data-period-ms="{FLICKER_MS}" style="--flicker-ms:'
                 f'{FLICKER_MS}ms">' + _img(conn, base, "base") + _img(conn, cand, "candidate")
                 + _boxes(rec, regions) + "</div>")
    parts.append("<h2>Heatmap</h2>" + _img(conn, heat, "heatmap"))
    parts.append("<h2>Regions</h2>" + table(["Region", "Kind", "Landmark", "Rect", "Money cell"], [
        [e(g["region_id"]), e(g["kind"]), e(g["landmark_id"]), e(f"{g['dx']},{g['dy']} {g['w']}x{g['h']}"),
         e("yes" if g["money_cell"] else "")] for g in regions]))
    reviews = _rows(conn, "SELECT * FROM reviews WHERE run_id = ? AND checkpoint_id = ? ORDER BY review_id",
                    (run_id, cid))
    parts.append("<h2>Reviewer</h2>" + (table(["Model", "Prompt", "Request", "Verdict", "Confidence", "Output"], [
        [e(v["model_id"]), e(v["prompt_hash"]), _img_link(v["request_sha256"]), e(v["verdict"]), e(v["confidence"]),
         f"<code>{e(v['response_json'])}</code>"] for v in reviews]) if reviews else "<p>not reviewed</p>"))
    ex = _rows(conn, "SELECT * FROM oracle_extractions WHERE run_id = ? AND checkpoint_id = ? ORDER BY "
                     "extraction_id", (run_id, cid))
    parts.append("<h2>Oracle values</h2>" + table(["Assertion", "Field", "Side", "Source", "Text", "Value",
                                                   "Confidence"], [
        [e(x["assertion_id"]), e(x["field"]), e(x["side"]), e(x["source"]), f"<code>{e(x['text'])}</code>",
         e(x["value"]), e(x["confidence"])] for x in ex]))
    if rec.get("invariant_ids") or (f is not None and f["kind"] == "invariant_violation"):
        ev = json.loads(f["evidence_json"]) if f is not None else {}
        parts.append("<h2>Invariants</h2>" + dl([("Fired", ", ".join(rec.get("invariant_ids") or ()) or o["reason"]),
                                                 ("Bounds source", ev.get("bounds_source") or "not recorded")]))
    steps = _rows(conn, "SELECT * FROM step_events WHERE run_id = ? AND checkpoint_id = ? ORDER BY shard_id, seq "
                        "LIMIT ?", (run_id, cid, PAGE_SIZE))
    parts.append("<h2>Step events</h2>" + table(["Seq", "Step", "Action", "Rung", "Outcome", "Gate", "Reason"], [
        [e(s["seq"]), e(s["step_id"]), e(s["action"]), e(s["locator_level"]), e(s["step_outcome"]),
         e(s["gate_decision"]), e(s["reason"])] for s in steps]))
    if arts.get("log_slice"):
        parts.append("<p>" + a(f"/a/{arts['log_slice']}", "log slice") + "</p>")
    flake = _one(conn, "SELECT * FROM flake_stats WHERE checkpoint_id = ?", (cid,))
    parts.append("<h2>Flake score</h2>" + (dl([("Score", flake["flake_score"]), ("Flips", flake["flip_total"]),
                                               ("Quarantined", "yes" if flake["quarantined"] else "no")])
                                           if flake else "<p>no statistics yet</p>"))
    hist = _rows(conn, "SELECT o.run_id, o.outcome, o.reason FROM outcomes o JOIN runs r ON r.run_id = o.run_id "
                       "WHERE o.checkpoint_id = ? AND o.synthetic = 0 ORDER BY r.started_at DESC LIMIT ?",
                 (cid, HISTORY_RUNS))
    parts.append(f"<h2>History over {HISTORY_RUNS} runs</h2>" + table(["Run", "Outcome", "Reason"], [
        [a(f"/runs/{q(h['run_id'])}", h["run_id"]), e(h["outcome"]), e(h["reason"])] for h in hist]))
    if f is not None:
        parts.append(_lookup(conn, f, run_id))
        parts.append("<h2>Actions</h2>" + _action_links(f["natural_key"], writable, extra=(
            "quarantine", "unquarantine", "accept_anchor_patch")))
    return Page(f"Checkpoint {cid}", "".join(parts), provenance(conn, run_id))


def _img_link(sha: str | None) -> str:
    return a(f"/a/{sha}", "request") if sha and SHA256_RE.match(str(sha)) else e(sha or "")


def _lookup(conn: sqlite3.Connection, f: sqlite3.Row, run_id: str) -> str:
    """The live decision lookup (8.3): what a new diff with this signature would inherit now."""
    if not (f["checkpoint_id"] and f["region_id"] and f["phash"] is not None and f["category"]):
        return "<h2>Live decision lookup</h2><p>no diff signature</p>"
    look = lookup_decisions(conn, Signature(f["checkpoint_id"], f["region_id"], bytes(f["phash"]).hex(),
                                            f["category"]), run_id=run_id)
    inh = look.inherited
    lines = [("Would inherit", inh.decision_id + f" ({inh.verdict})" if inh else "nothing"),
             ("Frozen", "yes" if look.frozen else "no"),
             ("Conflict", ", ".join(f"{m.decision.decision_id} ({m.decision.verdict})" for m in look.applicable)
              if look.conflict else "no"),
             ("Related, not applied", ", ".join(f"{m.decision.decision_id} at Hamming {m.distance}"
                                                for m in look.related) or "none")]
    return "<h2>Live decision lookup</h2>" + dl(lines)


def _button(href: str, label: str, writable: bool) -> str:
    return a(href, label) if writable else f'<span class="disabled" title="read-only">{e(label)}</span>'


def _action_links(ref: str, writable: bool, extra: Sequence[str] = ()) -> str:
    return " | ".join(_button(f"/act/{x}?f={q(ref)}", RULES[x].row if x in RULES else x, writable)
                      for x in (*FINDING_ACTIONS, *extra))


def finding_page(conn: sqlite3.Connection, finding_id: str, *, writable: bool) -> Page:
    """The finding: its evidence, its review, its proposal and the live decision lookup (8.7)."""
    f = _one(conn, "SELECT * FROM findings WHERE natural_key = ?", (finding_id,))
    if f is None:
        raise NotFound(f"finding {finding_id}")
    ev = json.loads(f["evidence_json"])
    items = [("Run", f["run_id"]), ("Kind", f["kind"]), ("Tier", f["tier"]), ("Checkpoint", f["checkpoint_id"]),
             ("Region", f["region_id"]), ("Category", f["category"]), ("Triage state", f["triage_state"]),
             ("Ticket", f["ticket_ref"]), ("Decision", f"dec-{f['decision_id']:06d}" if f["decision_id"] else None),
             ("Description", f["description"])]
    parts = [f"<h1>Finding {e(finding_id)}</h1>", dl(items)]
    if ev.get("triage_note"):
        parts.append(f'<p class="warn">{e(ev["triage_note"])}</p>')
    if ev.get("proposal"):
        p = ev["proposal"]
        parts.append("<h2>Proposal</h2>" + dl([("Action", p.get("action")), ("Proposer", p.get("proposer")),
                                               ("Time", p.get("time")), ("Ticket", p.get("ticket")),
                                               ("Reason", p.get("reason")), ("State", p.get("state"))]))
    shown = {k: v for k, v in ev.items() if k not in ("proposal", "triage_note")}
    parts.append(f"<h2>Evidence</h2><pre>{e(json.dumps(shown, indent=1, sort_keys=True))}</pre>")
    if f["checkpoint_id"]:
        parts.append("<p>" + a(f"/cp/{q(finding_id)}", "checkpoint page (crops, flicker, heatmap)") + "</p>")
    parts.append(_lookup(conn, f, f["run_id"]))
    parts.append("<h2>Actions</h2>" + _action_links(finding_id, writable))
    return Page(f"Finding {finding_id}", "".join(parts), provenance(conn, f["run_id"]))


# ---------------------------------------------------------------------------------------------------------------
# queues

def _confidence(conn: sqlite3.Connection, f: sqlite3.Row) -> float:
    r = _one(conn, "SELECT MAX(confidence) FROM reviews WHERE run_id = ? AND checkpoint_id IS ?",
             (f["run_id"], f["checkpoint_id"]))
    return float(r[0]) if r and r[0] is not None else 0.0


def triage_rank(conn: sqlite3.Connection, f: sqlite3.Row) -> tuple:
    """The 9.2 order of a finding: tier, kind, reviewer confidence descending, novelty, age."""
    kind = f["kind"] if f["kind"] in KIND_RANK else (f["category"] or "")
    rank = KIND_RANK.get(kind, len(KIND_RANK) + 1)
    if kind == "needs_human" and f["detail"] == "uncalibrated":
        rank = KIND_RANK["collateral"]
    novel = 0 if (f["first_seen_run_id"] or f["run_id"]) == f["run_id"] else 1
    return (f["tier"] or 9, rank, -_confidence(conn, f), novel, f["created_at"], f["natural_key"])


def triage_queue(conn: sqlite3.Connection, params: Mapping[str, str], *, writable: bool,
                 sla: Callable[[str, str, int | None], str | None] | None = None) -> Page:
    """Every finding without a decision, ranked as in 9.2, with SLA clock, assignee and pending proposals (9.5)."""
    fs = _rows(conn, "SELECT f.*, t.assignee, t.assigned_at, t.sla_due_at FROM findings f LEFT JOIN "
                     "triage_assignments t ON t.finding_id = f.finding_id AND t.released_at IS NULL WHERE "
                     "f.triage_state = 'open' AND f.decision_id IS NULL AND f.synthetic = 0 AND f.held = 0")
    fs = sorted(fs, key=lambda f: triage_rank(conn, f))
    page = _page(params.get("page"))
    shown = fs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    rows = []
    for n, f in enumerate(shown, start=(page - 1) * PAGE_SIZE + 1):
        start = f["assigned_at"] or f["created_at"]
        due = f["sla_due_at"] or (sla("finding", start, f["tier"]) if sla else None)
        ev = json.loads(f["evidence_json"])
        rows.append([e(n), e(f["tier"]), a(f"/f/{q(f['natural_key'])}", f["natural_key"]), e(f["kind"]),
                     e(f"due {due}" if due else f"since {start}"), e(f["assignee"] or "unassigned"),
                     e(ev.get("triage_note") or ""),
                     _action_links(f["natural_key"], writable, extra=("assign",))])
    props = _rows(conn, "SELECT p.*, f.natural_key, f.tier FROM proposals p JOIN findings f ON f.finding_id = "
                        "p.finding_id WHERE p.state = 'proposed' ORDER BY p.proposed_at, p.proposal_id")
    prow = []
    for p in props:
        due = sla("proposal", p["proposed_at"], p["tier"]) if sla else None
        prow.append([e(p["proposal_id"]), a(f"/f/{q(p['natural_key'])}", p["natural_key"]), e(p["action"]),
                     e(p["proposed_by"]), e(p["ticket_ref"]), e(p["reason"]),
                     e(f"due {due}" if due else f"since {p['proposed_at']} (4 business hours)"),
                     _button(f"/act/confirm_proposal?proposal={p['proposal_id']}", "confirm", writable) + " | "
                     + _button(f"/act/decline_proposal?proposal={p['proposal_id']}", "decline", writable)])
    body = ("<h1>Triage queue</h1>" + table(["#", "Tier", "Finding", "Kind", "SLA clock", "Assignee", "Note",
                                             "Actions"], rows)
            + _pager("/triage", page, len(fs) > page * PAGE_SIZE)
            + "<h2>Proposals awaiting a second person</h2>"
            + table(["Proposal", "Finding", "Action", "Proposer", "Ticket", "Reason", "Clock", ""], prow))
    return Page("Triage queue", body, _newest((f["run_id"] for f in fs), conn))


def decisions_page(conn: sqlite3.Connection, params: Mapping[str, str], *, writable: bool, now: datetime) -> Page:
    """Search by signature, checkpoint, ticket or decider; the statuses; conflicts; decisions without tickets;
    expiring in 7 days; suspect decisions (9.5)."""
    text = params.get("q") or ""
    status = params.get("status") or None
    page = _page(params.get("page"))
    limit, offset = _limit(page)
    like = f"%{text}%"
    rows = _rows(conn, "SELECT * FROM decisions WHERE (:s IS NULL OR status = :s) AND (:q = '' OR checkpoint_id LIKE "
                       ":l OR region_id LIKE :l OR ticket_ref LIKE :l OR decided_by LIKE :l OR hex(phash) LIKE :u) "
                       "ORDER BY decision_id DESC LIMIT :n OFFSET :o",
                 {"s": status, "q": text, "l": like, "u": f"%{text.upper()}%", "n": limit, "o": offset})
    more, rows = len(rows) > PAGE_SIZE, rows[:PAGE_SIZE]

    def line(d: sqlite3.Row) -> list[str]:
        ref = f"dec-{d['decision_id']:06d}"
        acts = []
        if d["status"] in ("active", "reopened"):
            acts = [_button(f"/act/supersede?decision={ref}", "supersede", writable),
                    _button(f"/act/revoke?decision={ref}", "revoke", writable)]
            if d["action"] == "noise_signature":
                acts.append(_button(f"/act/reconfirm_noise?decision={ref}", "re-confirm", writable))
        return [e(ref), e(d["action"]), e(d["verdict"]), e(d["status"]), e(d["checkpoint_id"]), e(d["region_id"]),
                e(d["ticket_ref"]), e(d["decided_by"]), e(d["expires_at"]), " | ".join(acts)]

    head = ["Decision", "Action", "Verdict", "Status", "Checkpoint", "Region", "Ticket", "Decider", "Expires", ""]
    soon = (now + timedelta(days=EXPIRING_DAYS)).isoformat(timespec="seconds")
    conflicts = _rows(conn, "SELECT checkpoint_id, region_id, category, group_concat(DISTINCT verdict) AS v FROM "
                            "decisions WHERE status = 'active' GROUP BY checkpoint_id, region_id, category HAVING "
                            "count(DISTINCT verdict) > 1")
    sections = [
        ("Without tickets", "status = 'active' AND action = 'confirm_bug' AND ticket_ref IS NULL", ()),
        ("Expiring in 7 days", "status = 'active' AND expires_at > ? AND expires_at <= ?",
         (now.isoformat(timespec="seconds"), soon)),
        ("Suspect (two reopenings)", "reopen_count >= ?", (SUSPECT_REOPENS,)),
        ("Contested", "source_finding_id IN (SELECT finding_id FROM findings WHERE contested = 1)", ())]
    body = [f'<h1>Decisions</h1><form method="get" action="/decisions"><input name="q" value="{e(text)}" '
            f'placeholder="signature, checkpoint, ticket or decider"> <button>search</button></form><p>'
            + " | ".join(a(f"/decisions?status={s}", s) for s in ("active", "expired", "superseded", "reopened",
                                                                   "revoked")) + "</p>",
            table(head, (line(d) for d in rows)), _pager("/decisions", page, more, {"q": text, "status": status}),
            "<h2>Conflicts</h2>" + table(["Checkpoint", "Region", "Category", "Verdicts"], [
                [e(c["checkpoint_id"]), e(c["region_id"]), e(c["category"]), e(c["v"])] for c in conflicts])]
    for title, where, args in sections:
        found = _rows(conn, f"SELECT * FROM decisions WHERE {where} ORDER BY decision_id LIMIT {PAGE_SIZE}", args)
        body.append(f"<h2>{e(title)}</h2>" + table(head, (line(d) for d in found)))
    return Page("Decisions", "".join(body), _newest((d["run_id"] for d in rows), conn))


def baseline_queue(conn: sqlite3.Connection, params: Mapping[str, str], *, writable: bool) -> Page:
    """Candidates per checkpoint from merge runs (9.8) beside the approved baseline of their lineage (9.5)."""
    page = _page(params.get("page"))
    limit, offset = _limit(page)
    rows = _rows(conn, "SELECT b.*, c.tier, (SELECT image_sha256 FROM baselines x WHERE x.checkpoint_id = b.checkpoint_id "
                       "AND x.lineage = b.lineage AND x.state = 'approved') AS approved, (SELECT natural_key FROM "
                       "findings f WHERE f.run_id = b.source_run_id AND f.checkpoint_id = b.checkpoint_id AND "
                       "f.synthetic = 0 ORDER BY f.kind = 'needs_baseline' DESC LIMIT 1) AS finding, "
                       "(SELECT evidence_json FROM findings f WHERE f.run_id = b.source_run_id AND f.checkpoint_id = "
                       "b.checkpoint_id AND f.synthetic = 0 ORDER BY f.kind = 'needs_baseline' DESC LIMIT 1) AS ev "
                       "FROM baselines b JOIN checkpoints c ON c.checkpoint_id = b.checkpoint_id WHERE b.state = "
                       "'candidate' ORDER BY c.tier, b.checkpoint_id, b.baseline_id LIMIT ? OFFSET ?", (limit, offset))
    more, rows = len(rows) > PAGE_SIZE, rows[:PAGE_SIZE]
    out = []
    for b in rows:
        closest = (json.loads(b["ev"]) if b["ev"] else {}).get("closest_baseline")
        acts = []
        if b["finding"]:
            for act in ("approve_baseline", "reject_baseline"):
                acts.append(_button(f"/act/{act}?f={q(b['finding'])}&baseline_id={b['baseline_id']}",
                                    act.replace("_", " "), writable))
        out.append([e(b["tier"]), e(b["checkpoint_id"]), e(b["lineage"]), e(b["build_id"]),
                    a(f"/runs/{q(b['source_run_id'])}", b["source_run_id"]) if b["source_run_id"] else "",
                    _img(conn, b["image_sha256"], "candidate"), _img(conn, b["approved"], "approved baseline"),
                    e(closest or ""), " | ".join(acts)])
    body = ("<h1>Baseline approval queue</h1>" + table(["Tier", "Checkpoint", "Lineage", "Build", "From run",
                                                        "Candidate", "Approved", "Nearest by pHash", "Actions"], out)
            + _pager("/baselines", page, more))
    return Page("Baseline approval queue", body, _newest((b["source_run_id"] for b in rows), conn))


def canary_panel(conn: sqlite3.Connection, params: Mapping[str, str]) -> Page:
    """The newest Nightly run's results per mutant and class, the detector state and what absorbed each miss."""
    run_id = params.get("run") or (_one(conn, "SELECT run_id FROM runs WHERE run_type = 'nightly' ORDER BY "
                                              "started_at DESC LIMIT 1") or [None])[0]
    state = _one(conn, "SELECT value FROM system_state WHERE key = 'detector_state'")
    rows = _rows(conn, "SELECT * FROM canary_results WHERE run_id = ? ORDER BY kind, defect_class, mutant_id, "
                       "checkpoint_id LIMIT ?", (run_id, PAGE_SIZE)) if run_id else []
    body = (f"<h1>Canary panel</h1>{dl([('Nightly run', run_id or 'none'), ('Detector state', state[0] if state else 'ok')])}"
            + table(["Kind", "Mutant", "Class", "Checkpoint", "State", "Produced", "Absorbed by"], [
                [e(r["kind"]), e(r["mutant_id"]), e(r["defect_class"]), e(r["checkpoint_id"]), e(r["state"]),
                 e(f"{r['produced_outcome']} ({r['produced_reason']})" if r["produced_outcome"] else ""),
                 e(r["absorbed_by"])] for r in rows]))
    return Page("Canary panel", body, _newest([run_id], conn))


def metrics_rows(conn: sqlite3.Connection, now: datetime, days: int) -> list[dict]:
    """The metrics rows of the window: runs and outcomes per run type and outcome, open findings per tier."""
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    out = [{"metric": "runs", "key": f"{r[0]}:{r[1]}", "value": r[2]} for r in _rows(
        conn, "SELECT run_type, COALESCE(outcome, lifecycle), count(*) FROM runs WHERE started_at >= ? GROUP BY 1, 2 "
              "ORDER BY 1, 2", (since,))]
    out += [{"metric": "outcomes", "key": r[0], "value": r[1]} for r in _rows(
        conn, "SELECT o.outcome, count(*) FROM outcomes o JOIN runs r ON r.run_id = o.run_id WHERE r.started_at >= ? "
              "AND o.synthetic = 0 GROUP BY 1 ORDER BY 1", (since,))]
    out += [{"metric": "open_findings", "key": f"tier {r[0]}", "value": r[1]} for r in _rows(
        conn, "SELECT tier, count(*) FROM findings WHERE triage_state = 'open' AND synthetic = 0 GROUP BY 1 ORDER BY 1")]
    return out


def metrics(conn: sqlite3.Connection, params: Mapping[str, str], now: datetime) -> Page:
    """The 9.11 dashboard: the window's counts and the Nightly run's metrics summary; window and CSV export."""
    days = _page(params.get("days") or 30)
    rows = metrics_rows(conn, now, days)
    nightly = _one(conn, "SELECT run_id, summary_json FROM runs WHERE run_type = 'nightly' ORDER BY started_at DESC "
                         "LIMIT 1")
    summary = (json.loads(nightly["summary_json"] or "{}").get("metrics") if nightly else None) or {}
    body = (f"<h1>Metrics</h1><p>window {days} days: " + " | ".join(a(f"/metrics?days={d}", f"{d} d")
                                                                      for d in (7, 30, 90))
            + " | " + a(f"/metrics?days={days}&format=csv", "export CSV") + "</p>"
            + table(["Metric", "Key", "Value"], [[e(r["metric"]), e(r["key"]), e(r["value"])] for r in rows])
            + "<h2>Nightly metrics</h2>" + dl(sorted((k, json.dumps(v) if not isinstance(v, str) else v)
                                                    for k, v in summary.items())))
    return Page("Metrics", body, _newest([nightly["run_id"] if nightly else None], conn))


def metrics_csv(conn: sqlite3.Connection, params: Mapping[str, str], now: datetime) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["metric", "key", "value"])
    w.writeheader()
    w.writerows(metrics_rows(conn, now, _page(params.get("days") or 30)))
    return buf.getvalue()


def manifest_template() -> Page:
    """The change manifest template page (4.3, 2.10.2) that the PR comment links."""
    body = ("<h1>Change manifest template</h1><p>Add a manifest entry to the PR description for every UI surface "
            "the change touches (4.3):</p><pre>sentinel-manifest:\n  - id: m1\n    ticket: TOS-12345\n    "
            "summary: what changes on screen\n    surfaces: [trade.active-trader.ladder]\n</pre>")
    return Page("Change manifest template", body)


# ---------------------------------------------------------------------------------------------------------------
# actions

ACTION_FIELDS = {"ticket": "Ticket key", "reason": "Reason", "expires_at": "Expires at (ISO 8601)",
                 "assignee": "Assignee", "fixed_in_build": "Fixed in build", "pr": "Mask pull request number"}
NEEDS = {"confirm_bug": ("ticket", "reason"), "mark_intended": ("ticket", "reason"), "waive": ("ticket", "reason",
                                                                                             "expires_at"),
         "request_mask": ("reason",), "noise_signature": ("reason",), "approve_baseline": ("ticket", "reason"),
         "reject_baseline": ("ticket", "reason"), "fixed_in_build": ("fixed_in_build", "reason"),
         "revoke": ("reason",), "supersede": ("ticket", "reason"), "quarantine": ("ticket", "reason"),
         "unquarantine": ("reason",), "override_check": ("reason",), "decline_proposal": ("reason",),
         "assign": ("assignee",), "approve_mask": ("pr",), "accept_anchor_patch": ("reason",),
         "promote_flow": ("reason",)}
HIDDEN = ("f", "decision", "proposal", "run", "job", "baseline_id", "pr")
CLUSTER_ACTIONS = ("confirm_bug", "mark_intended", "noise_signature")


def action_form(conn: sqlite3.Connection, action: str, params: Mapping[str, str], *, writable: bool) -> Page:
    """The action pre-selected with its item (9.1: one click after SSO, plus a ticket or reason where required)."""
    if action not in RULES and action not in ("agent_mistake", "expected_behavior"):
        raise NotFound(f"action {action}")
    rule = RULES.get(action)
    item = params.get("f") or params.get("decision") or params.get("proposal") or params.get("run") or params.get("job")
    run_id = None
    cluster = ""
    if params.get("f"):
        f = _one(conn, "SELECT run_id, kind, tier, checkpoint_id, description, cluster_id FROM findings WHERE "
                       "natural_key = ?", (params["f"],))
        if f is None:
            raise NotFound(f"finding {params['f']}")
        run_id = f["run_id"]
        summary = dl([("Finding", params["f"]), ("Kind", f["kind"]), ("Tier", f["tier"]),
                      ("Checkpoint", f["checkpoint_id"]), ("Description", f["description"])])
        if f["cluster_id"] and action in CLUSTER_ACTIONS:         # 9.5: apply to a whole cluster
            cluster = (f'<label><input type="checkbox" name="cluster" value="1"> apply to every open member of '
                       f'cluster {e(f["cluster_id"])}</label><br>')
    else:
        summary = dl([("Item", item)])
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(params[k])}">' for k in HIDDEN if params.get(k))
    fields = "".join(f'<label>{e(ACTION_FIELDS[k])} <input name="{k}"></label><br>'
                     for k in NEEDS.get(action, ("reason",)) if k not in params)
    disabled = "" if writable else " disabled"
    title = rule.row if rule else action
    body = (f"<h1>{e(title)}</h1>{summary}<p class=\"muted\">Who may: {e(_who(rule))}</p>"
            f'<form method="post" action="/act/{e(action)}">{hidden}{fields}{cluster}<button{disabled}>{e(title)}</button>'
            "</form>")
    return Page(title, body, provenance(conn, run_id))


def _who(rule: Any) -> str:
    if rule is None:
        return "triage rotation (Explorer disposition)"
    return "; ".join(f"{'tier ' + str(k) if k else 'any tier'}: {', '.join(v)}" for k, v in rule.who.items())


def result_page(action: str, status: int, doc: Mapping[str, Any]) -> Page:
    """What the service answered: the rows written, or the refusal naming its clause (9.6)."""
    if status == 200:
        items = [("Decisions", ", ".join(doc.get("decisions") or ()) or "none"),
                 ("Proposal", doc.get("proposal") or "none"), ("Outbox jobs", ", ".join(map(str, doc.get("jobs") or ()))
                                                              or "none"), ("Note", doc.get("note") or "")]
        return Page(f"{action}: done", f"<h1>{e(action)}: done</h1>" + dl(items))
    body = f'<h1>{e(action)}: refused</h1><p class="warn">{e(doc.get("refused") or doc.get("error"))}</p>'
    if doc.get("budget") is not None:
        body += dl([("Current share", doc.get("share")), ("Budget", doc.get("budget")),
                    ("The mask adds", doc.get("mask_share")),
                    ("Advice", "declare a rule region instead, or decide by confirm_bug or mark_intended")])
    return Page(f"{action}: refused", body, status=status)


# ---------------------------------------------------------------------------------------------------------------
# artifacts (8.7)

def read_object(set_dir: Path, sha: str) -> bytes | None:
    """The verified bytes of `sha` in one set directory (8.4 `objects/<aa>/<sha256>.<ext>`), or None when the copy
    is absent, corrupt or in a quarantined set; reads only, never creates a directory."""
    if (set_dir / QUARANTINE_MARKER).exists():
        return None
    d = set_dir / "objects" / sha[:2]
    for p in sorted(d.glob(f"{sha}*")) if d.is_dir() else ():
        if p.name == sha or p.name.startswith(sha + "."):
            data = p.read_bytes()
            if hashlib.sha256(data).hexdigest() == sha:
                return data
    return None


def _missing_page(conn: sqlite3.Connection, sha: str, why: str) -> Page:
    f = _one(conn, "SELECT natural_key FROM findings f WHERE kind = 'artifact_missing' AND EXISTS (SELECT 1 FROM "
                   "json_each(f.evidence_json, '$.artifacts') WHERE value = ?) ORDER BY created_at LIMIT 1", (sha,))
    names = a(f"/f/{q(f[0])}", f[0]) if f else e("no finding records it yet")
    body = (f"<h1>Evidence missing</h1><p>{e(why)}</p><p>Object <code>{e(sha)}</code>. Recorded by: {names}</p>")
    return Page("Evidence missing", body, status=404)


def artifact(conn: sqlite3.Connection, sha: str, roots: Sequence[Path]) -> tuple[int, list[tuple[str, str]], bytes] | Page:
    """Serve `/a/<sha256>` (module docstring): the bytes with immutable headers, or the page that explains why not."""
    if not SHA256_RE.match(sha):
        raise NotFound(f"artifact {sha}")
    row = _one(conn, "SELECT * FROM artifacts WHERE sha256 = ?", (sha,))
    if row is None:
        named = _one(conn, "SELECT run_id, checkpoint_id FROM outcomes WHERE cand_capture_sha256 = ? OR "
                           "base_capture_sha256 = ? OR heatmap_sha256 = ? LIMIT 1", (sha, sha, sha))
        if named is None:
            raise NotFound(f"artifact {sha}")
        return _missing_page(conn, sha, f"the outcome of {named['checkpoint_id']} in run {named['run_id']} names "
                                        "this object, but the store has no artifacts row for it")
    copies = _rows(conn, "SELECT * FROM artifact_sets WHERE sha256 = ? ORDER BY created_at", (sha,)) or [
        {"set_path": row["set_path"], "deleted_at": row["deleted_at"], "quarantined_at": None}]
    live = [c for c in copies if c["deleted_at"] is None]
    if not live:
        when = max(c["deleted_at"] for c in copies)
        body = (f"<h1>Removed by retention</h1><p>This was a {e(row['kind'])} ({e(row['media_type'])}, "
                f"{e(row['bytes'])} bytes) of {e(row['set_path'])}, first stored by run {e(row['first_run_id'])}. "
                f"Retention removed it on {e(when)} (8.6).</p>")
        return Page("Removed by retention", body, status=410)
    if all(c["quarantined_at"] for c in live):
        return Page("Quarantined", f"<h1>Quarantined</h1><p>Object <code>{e(sha)}</code> is in a set the plaintext "
                                   "scan quarantined (6.9, 8.4); the security contact clears it.</p>", status=403)
    if row["audit_state"] != "ok":
        return _missing_page(conn, sha, f"the artifact audit found this object {row['audit_state']}")
    for c in live:
        if c["quarantined_at"]:
            continue
        for root in roots:
            data = read_object(Path(root) / c["set_path"], sha)
            if data is None:
                continue
            return 200, [("Content-Type", row["media_type"]), ("Content-Length", str(len(data))),
                         ("Cache-Control", IMMUTABLE)], data
    return _missing_page(conn, sha, "no stored copy of this object is on disk or hashes to its name")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
