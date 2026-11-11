"""The store package's exports (8.2 import and replay) and one definition of the releasing triage states (SD-83)."""
from __future__ import annotations

import sentinel.store as store_pkg
from sentinel.store import retention
from sentinel.store import store as store_mod


def test_the_import_and_replay_records_are_exported_and_the_sweep_alias_is_gone():
    for name in ("StoreIntegrityError", "ReplayResult", "ExportResult", "ShardRows"):
        assert getattr(store_pkg, name) is getattr(store_mod, name) and name in store_pkg.__all__
    assert not hasattr(store_pkg, "SweepResult") and not hasattr(store_mod, "SweepResult")


def test_retention_releases_the_triage_states_the_store_names():
    assert retention.RELEASE_STATES is store_mod.RELEASING_TRIAGE
