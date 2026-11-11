"""The proof reader: one Monitor read from grid OCR cross-checked with Copy All (6.1 Proof reader row, 6.6, 7.6).

`build_order_book(grid_rows_ocr, copy_all_text, net_liq=..., buying_power=...)` is the pure decision over what the
rig read for the post-run proof and its run-start snapshot: for each of Working, Filled and Canceled Orders and the
Position Statement, the rows the grid OCR read and the grid's Copy All text, parsed and header-checked by the
Oracle's own `ExportReader` (`oracle.sources.ExportSource` over `ExportTable`, 2.8.1 Grid copy); and the Net Liq and
buying power cells (6.6 step 5 reads both). It returns a `MonitorBook`, an `OrderBook` with the buying power, the
Working Orders screenshot the 6.6 Cancel All failure page carries, and the problems it found.

- `readable` is False when a grid's OCR could not be read, a cell of an OCR row is unreadable (None), or the Net Liq
  or buying power cell is missing or not a money value: every reader the proof depends on fails closed (6 preamble).
- `copy_all_agrees` is False when a grid's Copy All text did not arrive, its header differs from the declared one, or
  its rows differ from the OCR rows: "a disagreement is `monitor_unreadable`" (6.6), never PROVEN.
- A row's key is `row_key`: the cells of the layout's key columns joined by `|`. The status column is not part of it,
  so an order keeps its key when it moves from Working to Canceled; the flow's Oracle read of a sent order's Working
  row uses the same function, so `records.DeclaredSend.row_key` matches the proof's rows (6.7).

Choices where the spec is silent (reported):

1. The column labels are pinned in Phase 0 (6.13); `DEFAULT_LAYOUTS` holds placeholders the simulated rig and the
   tests use, and the real rig passes the observed layouts.
2. Rows are compared as multisets over every declared column, each cell with whitespace collapsed (the Oracle's
   `text` comparison form); the order of rows is not compared, since a re-sort between two reads changes nothing.
3. When the two readers disagree the rows are still reported, the union by key with the Copy All row first, so a
   row either reader saw still makes the sequence click `Cancel All` (6.6 step 5); the proof itself is UNPROVEN.
4. Quantities are integers (one share or one contract, 6.7); a quantity cell that is not an integer is unreadable.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..oracle.sources import ExportSource, ExportTable
from ..oracle.values import FormatError, normalize_text, number
from .rig import OrderBook, OrderRow

ORDER_GRIDS = ("working", "filled", "canceled")
POSITION_GRID = "position"
GRIDS = ORDER_GRIDS + (POSITION_GRID,)

Cells = Mapping[str, str | None]


@dataclass(frozen=True)
class GridLayout:
    """One Monitor grid as Copy All gives it (2.8.1 Grid copy): the declared header, the key columns, and the status,
    symbol and quantity columns an `OrderRow` takes."""

    header: tuple[str, ...]
    key: tuple[str, ...]
    status: str | None = None
    symbol: str | None = None
    qty: str | None = None

    def __post_init__(self) -> None:
        named = set(self.key) | {c for c in (self.status, self.symbol, self.qty) if c is not None}
        missing = sorted(named - set(self.header))
        if missing or not self.key:
            raise ValueError(f"grid layout names columns outside its header {missing} or has no key columns")


ORDER_LAYOUT = GridLayout(
    header=("Time Placed", "Side", "Qty", "Symbol", "Type", "Price", "TIF", "Status"),
    key=("Time Placed", "Side", "Qty", "Symbol", "Type", "Price", "TIF"), status="Status", symbol="Symbol", qty="Qty")
POSITION_LAYOUT = GridLayout(header=("Instrument", "Qty", "Trade Price", "Mark"), key=("Instrument",),
                             symbol="Instrument", qty="Qty")
DEFAULT_LAYOUTS: Mapping[str, GridLayout] = {"working": ORDER_LAYOUT, "filled": ORDER_LAYOUT,
                                             "canceled": ORDER_LAYOUT, POSITION_GRID: POSITION_LAYOUT}


@dataclass(frozen=True)
class MonitorBook(OrderBook):
    """A Monitor read built by `build_order_book`: the `OrderBook` the proof compares, plus the buying power (6.6
    step 5), the Working Orders screenshot (6.6 Cancel All failure page) and every problem the reader found."""

    buying_power: float | None = None
    screenshot_sha256: str | None = None
    problems: tuple[str, ...] = ()


def row_key(layout: GridLayout, cells: Cells) -> str | None:
    """The stable key of one grid row: its key cells, whitespace collapsed, joined by `|`; None when one is missing."""
    parts = []
    for c in layout.key:
        v = cells.get(c)
        if v is None:
            return None
        parts.append(normalize_text(v))
    return "|".join(parts)


def _canon(layout: GridLayout, cells: Cells) -> tuple[str, ...]:
    return tuple(normalize_text(cells.get(c) or "") for c in layout.header)


def _copy_rows(layout: GridLayout, text: str | None, grid: str, problems: list[str]) -> list[dict[str, str]] | None:
    """The grid's Copy All rows through the Oracle's ExportReader, or None when it cannot serve as the cross-check."""
    src = ExportSource(ExportTable.parse(text, "copy_all") if text is not None else None, layout.header,
                       kind="copy_all")
    if not src.produced:
        problems.append(f"{grid}: Copy All produced nothing")
        return None
    if not src.header_matches:
        problems.append(f"{grid}: Copy All header differs from the declared one")
        return None
    cols = {c: src.column(c) or () for c in layout.header}
    n = max((len(v) for v in cols.values()), default=0)
    return [{c: (cols[c][i] if i < len(cols[c]) else "") for c in layout.header} for i in range(n)]


def _order_row(layout: GridLayout, grid: str, cells: Cells, problems: list[str]) -> OrderRow | None:
    key = row_key(layout, cells)
    if key is None:
        return None
    qty = 0.0
    if layout.qty is not None:
        try:
            qty = float(number("int", cells.get(layout.qty)))
        except FormatError as e:
            problems.append(f"{grid} row {key}: quantity {cells.get(layout.qty)!r} unreadable ({e})")
            return None
    return OrderRow(key, grid, status=normalize_text(cells.get(layout.status) or "") if layout.status else "",
                    symbol=normalize_text(cells.get(layout.symbol) or "") if layout.symbol else "", qty=qty)


def _money(name: str, text: str | None, problems: list[str]) -> float | None:
    try:
        return float(number("money", text))
    except FormatError as e:
        problems.append(f"{name} {text!r} unreadable ({e})")
        return None


def build_order_book(grid_rows_ocr: Mapping[str, Sequence[Cells] | None], copy_all_text: Mapping[str, str | None],
                     *, net_liq: str | None, buying_power: str | None,
                     layouts: Mapping[str, GridLayout] = DEFAULT_LAYOUTS,
                     screenshot_sha256: str | None = None) -> MonitorBook:
    """One Monitor read for the proof (6.6 step 5, 7.6): each grid's OCR rows (column -> text, None for an unreadable
    cell; None for a grid the OCR could not read) cross-checked with its Copy All text, and the Net Liq and buying
    power cells. Disagreement or an unreadable read is recorded, never repaired (module docstring)."""
    problems: list[str] = []
    readable = agrees = True
    rows: dict[str, tuple[OrderRow, ...]] = {}
    position_rows = 0
    for g in GRIDS:
        layout = layouts[g]
        ocr = grid_rows_ocr.get(g)
        if ocr is None:
            problems.append(f"{g}: grid OCR unreadable")
            readable = False
        else:
            blind = [f"{g} row {i + 1}: cell {c!r} unreadable" for i, r in enumerate(ocr) for c in layout.header
                     if r.get(c) is None]
            problems += blind
            readable = readable and not blind
        copy = _copy_rows(layout, copy_all_text.get(g), g, problems)
        if copy is None:
            agrees = False
        elif ocr is not None and Counter(_canon(layout, r) for r in ocr) != Counter(_canon(layout, r) for r in copy):
            only_ocr = sorted({row_key(layout, r) or "?" for r in ocr} - {row_key(layout, r) or "?" for r in copy})
            only_copy = sorted({row_key(layout, r) or "?" for r in copy} - {row_key(layout, r) or "?" for r in ocr})
            problems.append(f"{g}: grid OCR and Copy All disagree (OCR only: {only_ocr or 'none'}; Copy All only: "
                            f"{only_copy or 'none'}; or a cell differs)")
            agrees = False
        copy_keys = {row_key(layout, c) for c in copy or ()}
        seen = list(copy or ()) + [r for r in ocr or () if row_key(layout, r) not in copy_keys]     # choice 3
        if g == POSITION_GRID:
            position_rows = len(seen)
            continue
        built = [_order_row(layout, g, r, problems) for r in seen]
        readable = readable and all(b is not None for b in built)
        rows[g] = tuple(b for b in built if b is not None)
    nl = _money("net liq", net_liq, problems)
    bp = _money("buying power", buying_power, problems)
    readable = readable and nl is not None and bp is not None
    return MonitorBook(working=rows["working"], filled=rows["filled"], canceled=rows["canceled"],
                       position_rows=position_rows, net_liq=nl, readable=readable, copy_all_agrees=agrees,
                       buying_power=bp, screenshot_sha256=screenshot_sha256, problems=tuple(problems))
