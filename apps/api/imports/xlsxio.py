"""Excel export: the same rows and filters as the CSV export, as an .xlsx workbook with a
frozen, bold header row, an auto-filter and typed cells (dates as dates, yes/no as booleans, numbers as numbers), so
the sheet sorts and filters properly when it is shared upward. Written server-side with openpyxl; nothing is read
back (imports stay CSV).

Formula safety. Every text cell gets the CSV export's escape (a leading `'` before `=`, `+`, `-`, `@`, tab, CR
or `'`, imports/csvio.escape_cell) and is stored as a string cell, never a formula, so a value such as `=HYPERLINK(...)`
can't run when the file is opened. Control characters Excel can't hold are dropped.

A second sheet. `EXTRA_SHEETS` holds functions `(org, kind, rows) -> Sheet | None`; each sheet one returns is added
after the main one. Custom fields register their sheet there."""

import datetime
import io
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .csvio import escape_cell

TEXT, DATE, DATETIME, BOOLEAN, NUMBER = "text", "date", "datetime", "boolean", "number"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_TITLE_MAX = 31  # Excel's limit
WIDTH_MIN, WIDTH_MAX = 10, 60


@dataclass(frozen=True)
class Column:
    key: str
    type: str = TEXT
    header: str = ""  # defaults to the key, the same heading the CSV uses

    @property
    def heading(self) -> str:
        return self.header or self.key


@dataclass
class Sheet:
    title: str
    columns: list[Column]
    rows: Iterable[dict[str, Any]] = field(default_factory=list)


# (org, kind, rows) -> an extra sheet or None. Rows are the exported records, in order.
EXTRA_SHEETS: list[Callable[[Any, str, list[Any]], Sheet | None]] = []


def _text(value: Any) -> str:
    text = escape_cell(value)
    return ILLEGAL_CHARACTERS_RE.sub("", text)


def typed(column: Column, value: Any) -> Any:
    """The cell value for `column`: a date, datetime, bool or number when the value is one (or is text that reads as
    one, as CSV values do), None when empty, else formula-safe text."""
    if value in (None, ""):
        return None
    if column.type == BOOLEAN:
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in ("true", "yes", "false", "no"):
            return lowered in ("true", "yes")
    elif column.type == DATE:
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.date.fromisoformat(str(value))
        except ValueError:
            pass
    elif column.type == DATETIME:
        if isinstance(value, datetime.datetime):
            return value.replace(tzinfo=None)  # Excel has no time zones; callers pass local time
    elif column.type == NUMBER:
        number: int | float | None = None
        if isinstance(value, int | float) and not isinstance(value, bool):
            number = value
        else:
            try:
                number = int(str(value)) if str(value).lstrip("-").isdigit() else float(str(value))
            except ValueError:
                number = None
        if number is not None and math.isfinite(number):  # nan and inf aren't numbers a sheet can hold
            return number
    return _text(value)


NUMBER_FORMATS = {DATE: "yyyy-mm-dd", DATETIME: "yyyy-mm-dd hh:mm"}


def _cell(ws, column: Column, value: Any) -> Any:
    value = typed(column, value)
    cell = WriteOnlyCell(ws, value=value)
    if isinstance(value, str):
        cell.data_type = "s"  # text, never a formula, whatever it starts with
    elif column.type in NUMBER_FORMATS and value is not None:
        cell.number_format = NUMBER_FORMATS[column.type]
    return cell


def write_xlsx(sheets: list[Sheet]) -> bytes:
    """A workbook with one worksheet per Sheet: bold header, frozen first row, auto-filter over the header."""
    wb = Workbook(write_only=True)
    bold = Font(bold=True)
    for sheet in sheets:
        ws = wb.create_sheet(title=_text(sheet.title)[:SHEET_TITLE_MAX] or "Sheet")
        ws.freeze_panes = "A2"
        widths = [max(WIDTH_MIN, len(c.heading) + 2) for c in sheet.columns]
        for i, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(width, WIDTH_MAX)
        header = []
        for column in sheet.columns:
            cell = WriteOnlyCell(ws, value=_text(column.heading))
            cell.data_type = "s"
            cell.font = bold
            header.append(cell)
        ws.append(header)
        count = 0
        for row in sheet.rows:
            ws.append([_cell(ws, c, row.get(c.key)) for c in sheet.columns])
            count += 1
        if sheet.columns:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(sheet.columns))}{count + 1}"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def extra_sheets(org, kind: str, rows: list[Any]) -> list[Sheet]:
    found = []
    for build in EXTRA_SHEETS:
        sheet = build(org, kind, rows)
        if sheet is not None:
            found.append(sheet)
    return found
