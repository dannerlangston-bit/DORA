"""The store CHECKs generated from taxonomy.yaml (8.3, L-OUT-01) and the sweep and money-cell CHECKs of SD-04, SD-05
and SD-29."""
import copy
import sqlite3

import pytest

from sentinel.store.store import Store, render_schema
from sentinel.taxonomy import Taxonomy, load

COMMIT = "c0ffee"


@pytest.fixture
def conn(tmp_path):
    s = Store(tmp_path / "run.db")
    yield s.conn
    s.close()


@pytest.mark.parametrize("key,table", [("money_roles", "regions"), ("quarantine_reasons", "flake_stats"),
                                       ("quarantine_reasons", "safety_quarantines"), ("canary_kinds", "canary_results"),
                                       ("mutant_classes", "canary_results"), ("decision_statuses", "decisions"),
                                       ("decision_sources", "decisions")])
def test_lists_render_from_the_taxonomy(key, table):
    """A value added to taxonomy.yaml appears in the rendered CHECK of its table; schema.sql holds no copy."""
    data = copy.deepcopy(load().data)
    data[key] = [*data[key], "test_only_value"]
    ddl = render_schema(Taxonomy(data))
    body = ddl[ddl.index(f"CREATE TABLE {table} "):]
    assert "'test_only_value'" in body[:body.index(");\n")]
    assert "'test_only_value'" not in render_schema(load())


def _region(conn, region_id, kind, money_role, money_cell=1):
    conn.execute("INSERT INTO regions (checkpoint_id, region_id, config_commit, kind, money_cell, landmark_id, dx, dy, w, h, "
                 "money_role) VALUES ('census/trade.active-trader.ladder/ws.tier-a.active-trader', ?, ?, ?, ?, 'lm', 0, 0, "
                 "10, 10, ?)", (region_id, COMMIT, kind, money_cell, money_role))


def test_money_cell_regions_are_never_masked_but_may_be_rules(conn):
    """SD-29 as 3.8 and L-RGN-04 scope it: a census or sweep money cell is a rule region; a mask never is one."""
    _region(conn, "rgn.qty", "rule", "qty")
    _region(conn, "rgn.price", "diff", "price")
    _region(conn, "rgn.send", "freeze", "send")
    for kind in ("mask", "redaction"):
        with pytest.raises(sqlite3.IntegrityError):
            _region(conn, f"rgn.{kind}", kind, "max_loss")
    with pytest.raises(sqlite3.IntegrityError):
        _region(conn, "rgn.notional", "diff", "notional")           # not a money role of 4.8
    with pytest.raises(sqlite3.IntegrityError):
        _region(conn, "rgn.unflagged", "diff", "cancel", money_cell=0)


def _sweep(conn, cid, host, w, h):
    conn.execute("INSERT INTO checkpoints (checkpoint_id, kind, surface_id, workspace_id, host, size_w, size_h, tier, "
                 "data_mode, first_seen_commit) VALUES (?, 'sweep', 'trade.active-trader.ladder', "
                 "'ws.tier-c.sweep-harness', ?, ?, ?, 1, 'frozen', ?)", (cid, host, w, h, COMMIT))


def test_sweep_checkpoints_take_the_w_x_h_hosts_and_the_ascending_suffix(conn):
    _sweep(conn, "sweep/trade.active-trader.ladder/flexgrid/640x480", "flexgrid", 640, 480)
    _sweep(conn, "sweep/trade.active-trader.ladder/flexgrid/640x480-asc", "flexgrid", 640, 480)     # SD-04
    _sweep(conn, "sweep/dialog.order-confirmation/dialog/480x360", "dialog", 480, 360)
    with pytest.raises(sqlite3.IntegrityError):
        _sweep(conn, "sweep/trade.active-trader.ladder/tab/640x480", "tab", 640, 480)              # SD-05
    with pytest.raises(sqlite3.IntegrityError):
        _sweep(conn, "sweep/trade.active-trader.ladder/flexgrid/800x600", "flexgrid", 640, 480)    # id and columns differ
    with pytest.raises(sqlite3.IntegrityError):
        _sweep(conn, "sweep/trade.active-trader.ladder/flexgrid/320x240", "detached", 320, 240)
