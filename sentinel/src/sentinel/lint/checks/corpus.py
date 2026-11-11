"""The Workspace Corpus shape of 2.3 (L-WSP-08, L-WSP-09) and the odd-input corpus (4.1 `corpus/odd-inputs.yaml`, 4.7).

- L-WSP-08 (error): a tier A or tier C entry lists a surface with `streaming: true` (news, video and chat gadgets) in
  `gadgets_present`.
- L-WSP-09 (warn until Phase 3, then error): exactly one tier C entry; four to eight tier B entries; at least two of
  them saved under the previous release, i.e. `saved_under_build` is the latest `released` build of builds/ by
  `released_at` (the RC run's base).
- The odd-input corpus: `corpus/odd-inputs.yaml` reads and validates against the shape of sentinel.oddinput
  (L-WSP-00, the corpus family's `-00` id, as for corpus/measured_sizes.yaml), and every `type_fixture` step's
  `value_ref: odd_inputs.<id>` names an entry of it (L-FLW-00, as for a fixture `value_ref` that does not resolve,
  rules.py choice 5).

Choices (reported): the corpus shape is reported once, on `corpus/workspaces`; with no released build in builds/, no
tier B entry can be saved under the previous release, so that half reports the missing release. The odd-input
corpus is read from the repository's `odd_inputs` documents when the 4.1 layout lists the file (lint/repo.py
LAYOUT), else from `corpus/odd-inputs.yaml` under the repository root.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

import yaml

from ... import oddinput
from ..repo import Doc, Repo, load_yaml_text

RULE_IDS = ("L-WSP-08", "L-WSP-09", "L-WSP-00", "L-FLW-00")
TIER_B_COUNT = (4, 8)                    # 2.3 tier B: 4 to 8
TIER_B_PREVIOUS_RELEASE = 2              # 2.3: at least two saved under the previous release
TIER_C_COUNT = 1
ERROR_FROM_PHASE = 3                     # L-WSP-09: warn until Phase 3, then error
CORPUS_DIR = "corpus/workspaces"


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def previous_release(repo: Repo) -> str | None:
    """The latest `released` build of builds/ by `released_at`: the previous release, the RC run's base (2.3, 4.6)."""
    released = []
    for doc in repo.of("build"):
        b = _d(_d(doc.data).get("build"))
        t = _ts(b.get("released_at"))
        if b.get("channel") == "released" and isinstance(b.get("build_id"), str) and t is not None:
            released.append((t, b["build_id"]))
    return max(released)[1] if released else None


def odd_input_doc(repo: Repo) -> Doc | None:
    """corpus/odd-inputs.yaml as a document: the repository's own when its layout reads the file, else from disk."""
    docs = repo.of("odd_inputs")
    if docs:
        return docs[0]
    if not repo.exists(oddinput.PATH):
        return None
    try:
        return Doc(oddinput.PATH, "odd_inputs", load_yaml_text(repo.path(oddinput.PATH).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return Doc(oddinput.PATH, "odd_inputs", None, f"cannot read: {exc}".splitlines()[0])


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    _streaming(repo, add)
    _shape(repo, add, phase)
    _odd_inputs(repo, add)


def _streaming(repo: Repo, add: Callable[..., None]) -> None:
    streaming = {_d(d.data).get("id") for d in repo.of("surface") if _d(d.data).get("streaming") is True}
    for doc in repo.of("workspace"):
        w = _d(doc.data)
        if w.get("tier") not in ("A", "C"):
            continue
        for g in _l(w.get("gadgets_present")):
            if g in streaming:
                add("L-WSP-08", doc.rel, f"tier {w.get('tier')} lists streaming surface {g}; no news, video or chat "
                                         "gadgets in tiers A and C (2.3)", "gadgets_present")


def _shape(repo: Repo, add: Callable[..., None], phase: int) -> None:
    entries = [_d(d.data) for d in repo.of("workspace")]
    if not entries:
        return
    sev = "error" if phase >= ERROR_FROM_PHASE else "warn"
    tier_c = [w for w in entries if w.get("tier") == "C"]
    tier_b = [w for w in entries if w.get("tier") == "B"]
    if len(tier_c) != TIER_C_COUNT:
        add("L-WSP-09", CORPUS_DIR, f"{len(tier_c)} tier C entries; the corpus has exactly one (2.3)", severity=sev)
    lo, hi = TIER_B_COUNT
    if not lo <= len(tier_b) <= hi:
        add("L-WSP-09", CORPUS_DIR, f"{len(tier_b)} tier B entries; the corpus has {lo} to {hi} (2.3)", severity=sev)
    prev = previous_release(repo)
    on_prev = sum(1 for w in tier_b if prev is not None and w.get("saved_under_build") == prev)
    if on_prev < TIER_B_PREVIOUS_RELEASE:
        where = f"the previous release {prev}" if prev else "a previous release (builds/ holds no released build)"
        add("L-WSP-09", CORPUS_DIR, f"{on_prev} tier B entries saved under {where}; at least "
                                    f"{TIER_B_PREVIOUS_RELEASE} (2.3)", severity=sev)


def _odd_inputs(repo: Repo, add: Callable[..., None]) -> None:
    doc = odd_input_doc(repo)
    ids: frozenset[str] = frozenset()
    if doc is not None:
        if doc.error:
            add("L-WSP-00", doc.rel, doc.error)
        else:
            problems = oddinput.validate(doc.data)
            for loc, msg in problems:
                add("L-WSP-00", doc.rel, msg, loc)
            ids = frozenset(str(_d(e).get("id")) for e in _l(_d(doc.data).get("entries")))
    for fdoc in repo.of("flow"):
        f = _d(fdoc.data)
        for sec in ("normalize", "preconditions", "steps", "teardown"):
            for st in map(_d, _l(f.get(sec))):
                ref = st.get("value_ref")
                if st.get("type") != "type_fixture" or not (isinstance(ref, str) and ref.startswith(oddinput.REF_PREFIX)):
                    continue
                at = f"{sec}[{st.get('id')}].value_ref"
                if doc is None:
                    add("L-FLW-00", fdoc.rel, f"{ref}: {oddinput.PATH} is missing", at)
                elif ref[len(oddinput.REF_PREFIX):] not in ids:
                    add("L-FLW-00", fdoc.rel, f"{ref} is not an entry of {oddinput.PATH}", at)
