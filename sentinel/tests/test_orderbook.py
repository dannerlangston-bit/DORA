"""The proof reader: grid OCR cross-checked with Copy All, Net Liq and buying power (6.1 Proof reader, 6.6, 7.6)."""
from __future__ import annotations

import pytest

from sentinel.orchestrator.killswitch import PROVEN_NONE, UNPROVEN, ProofInputs, post_run_proof
from sentinel.orchestrator.orderbook import (DEFAULT_LAYOUTS, ORDER_LAYOUT, POSITION_LAYOUT, GridLayout, MonitorBook,
                                             build_order_book, row_key)

H = ORDER_LAYOUT.header
P = POSITION_LAYOUT.header
ORDER = {"Time Placed": "11/10/26 14:02:11", "Side": "BUY", "Qty": "+1", "Symbol": "SPY", "Type": "LMT",
         "Price": "400.00", "TIF": "DAY", "Status": "WORKING"}
POS = {"Instrument": "AAPL", "Qty": "100", "Trade Price": "150.00", "Mark": "151.20"}


def copy_all(header: tuple[str, ...], rows: list[dict]) -> str:
    return "\n".join(["\t".join(header)] + ["\t".join(r[c] for c in header) for r in rows])


def read(working=(), filled=(), canceled=(), positions=(POS,), *, ocr_over=None, copy_over=None, net_liq="25,000.00",
         buying_power="$50,000.00", shot=None) -> MonitorBook:
    grids = {"working": list(working), "filled": list(filled), "canceled": list(canceled), "position": list(positions)}
    ocr = {**grids, **(ocr_over or {})}
    text = {g: copy_all(P if g == "position" else H, rows) for g, rows in grids.items()}
    text.update(copy_over or {})
    return build_order_book(ocr, text, net_liq=net_liq, buying_power=buying_power, screenshot_sha256=shot)


def test_agreeing_reads_give_the_rows_position_count_net_liq_and_buying_power():
    b = read(working=[ORDER], shot="sha-wo")
    assert b.readable and b.copy_all_agrees and b.problems == ()
    (w,) = b.working
    assert w.key == "11/10/26 14:02:11|BUY|+1|SPY|LMT|400.00|DAY" and (w.grid, w.status, w.symbol, w.qty) == (
        "working", "WORKING", "SPY", 1.0)
    assert b.position_rows == 1 and b.net_liq == 25000.0 and b.buying_power == 50000.0
    assert b.screenshot_sha256 == "sha-wo" and b.filled == b.canceled == ()


def test_an_ocr_row_missing_from_copy_all_disagrees_and_the_proof_is_monitor_unreadable():
    start = read()
    end = read(working=[ORDER], copy_over={"working": copy_all(H, [])})
    assert end.readable and not end.copy_all_agrees
    assert any("OCR only" in p and "SPY" in p for p in end.problems)
    assert [r.key for r in end.working] == [row_key(ORDER_LAYOUT, ORDER)]       # still seen, so Cancel All clicks
    p = post_run_proof(ProofInputs(start, end))
    assert (p.verdict, p.reason) == (UNPROVEN, "monitor_unreadable")
    assert post_run_proof(ProofInputs(start, read())).verdict == PROVEN_NONE


def test_a_misread_cell_disagrees_and_whitespace_and_row_order_do_not():
    other = {**ORDER, "Time Placed": "11/10/26 14:05:00", "Symbol": "QQQ"}
    misread = {**ORDER, "Price": "460.00"}
    assert not read(working=[ORDER], ocr_over={"working": [misread]}).copy_all_agrees
    spaced = {**ORDER, "Symbol": " SPY ", "Time Placed": "11/10/26  14:02:11"}
    b = read(working=[ORDER, other], ocr_over={"working": [other, spaced]})
    assert b.copy_all_agrees and b.readable and len(b.working) == 2


@pytest.mark.parametrize("text,why", [(None, "produced nothing"), ("Symbol\tQty\nSPY\t1", "header differs")])
def test_copy_all_missing_or_with_another_header_is_no_cross_check(text, why):
    b = read(copy_over={"filled": text})
    assert not b.copy_all_agrees and any(why in p for p in b.problems)


def test_unreadable_ocr_grid_cell_money_or_quantity_fails_closed():
    assert not read(ocr_over={"canceled": None}).readable
    blind = {**ORDER, "Symbol": None}
    b = read(working=[ORDER], ocr_over={"working": [blind]})
    assert not b.readable and any("'Symbol' unreadable" in p for p in b.problems)
    assert not read(net_liq=None).readable and read(net_liq=None).net_liq is None
    nan = read(buying_power="NaN")
    assert not nan.readable and nan.buying_power is None and any("buying power" in p for p in nan.problems)
    half = {**ORDER, "Qty": "0.5"}
    assert not read(working=[half]).readable


def test_a_row_keeps_its_key_when_it_moves_from_working_to_canceled():
    canceled = {**ORDER, "Status": "CANCELED"}
    assert row_key(ORDER_LAYOUT, ORDER) == row_key(ORDER_LAYOUT, canceled)
    b = read(canceled=[canceled])
    assert b.canceled[0].key == row_key(ORDER_LAYOUT, ORDER) and b.canceled[0].status == "CANCELED"
    assert row_key(ORDER_LAYOUT, {**ORDER, "Side": None}) is None


def test_layouts_name_only_columns_of_their_header():
    assert set(DEFAULT_LAYOUTS) == {"working", "filled", "canceled", "position"}
    with pytest.raises(ValueError):
        GridLayout(header=("Symbol",), key=("Order ID",))
    with pytest.raises(ValueError):
        GridLayout(header=("Symbol",), key=())
