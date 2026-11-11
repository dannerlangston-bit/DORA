"""The 7.3 build-id read: title regex, the one About fallback, version string to build id over builds/*.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

from sentinel.orchestrator.budgets import ShardBudgets
from sentinel.orchestrator.gates import check_launched
from sentinel.orchestrator.rig import LaunchObservation
from sentinel.orchestrator.version_read import resolve_build_id

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
BUILDS = [yaml.safe_load(p.read_text()) for p in sorted((EXAMPLE / "builds").glob("*.yaml"))]
BRANCH = "branch-1998.3.0-5d3e812"


def test_title_maps_to_the_single_build_with_that_version_string():
    r = resolve_build_id("Main@thinkorswim [build 1998.3.0]", None, BUILDS)
    assert (r.title_build_id, r.about_build_id, r.source, r.version_string) == (BRANCH, None, "title", "1998.3.0")


def test_about_is_read_once_and_only_when_the_title_misses():
    calls: list[int] = []

    def about() -> str:
        calls.append(1)
        return "thinkorswim build 1998.2.0 (Oct 2026)"

    hit = resolve_build_id("Main@thinkorswim [build 1998.3.0]", about, BUILDS)
    assert hit.source == "title" and not calls
    miss = resolve_build_id("thinkorswim", about, BUILDS)
    assert (miss.title_build_id, miss.about_build_id, miss.source) == (None, "main-1998.2.0-4f1c9a2", "about")
    assert len(calls) == 1


def test_two_builds_with_one_version_string_are_unreadable():
    twin = {"build": {"build_id": "main-1998.3.0-0000000", "version_string": "1998.3.0"}}
    r = resolve_build_id("Main@thinkorswim [build 1998.3.0]", "build 1998.3.0", BUILDS + [twin])
    assert r.build_id is None and r.source is None and "cannot tell" in r.detail
    gate = check_launched(LaunchObservation(True, r.title_build_id, about_build_id=r.about_build_id),
                          expected_build=BRANCH, first_launch=True, budgets=ShardBudgets())
    assert gate.unreached.reason == "fingerprint_unreadable:build_id"


def test_an_unlisted_version_is_a_mismatch_not_unreadable():
    r = resolve_build_id("Main@thinkorswim [build 1999.0.1]", None, BUILDS)
    assert r.title_build_id == "unlisted:1999.0.1"
    gate = check_launched(LaunchObservation(True, r.title_build_id), expected_build=BRANCH, first_launch=True,
                          budgets=ShardBudgets())
    assert gate.unreached.reason == "version_mismatch" and gate.has("disable_rig")


def test_phase0_regexes_override_the_community_pattern():
    rx = {"title_regex": r"thinkorswim \((?P<version>[\d.]+)\)", "about_regex": None}
    assert resolve_build_id("thinkorswim (1998.2.0)", None, BUILDS, rx).build_id == "main-1998.2.0-4f1c9a2"
    assert resolve_build_id("Main@thinkorswim [build 1998.2.0]", None, BUILDS, rx).build_id is None
    by_id = {b["build"]["build_id"]: b for b in BUILDS}                  # SentinelConfig.builds shape
    assert resolve_build_id("Main@thinkorswim [build 1998.2.0]", None, by_id).build_id == "main-1998.2.0-4f1c9a2"


def test_neither_read_matches():
    r = resolve_build_id(None, lambda: None, BUILDS)
    assert r.build_id is None and r.detail == "title: no match; about: no match"
