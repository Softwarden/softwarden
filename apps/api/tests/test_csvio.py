"""CSV basics: reading limits, encodings and the reversible formula escape."""

import pytest

from catalog.models import Kind
from imports import csvio


@pytest.mark.parametrize(
    "text",
    ["=SUM(A1)", "+1", "-Internal", "@cmd", "\tlead", "\rlead", "'-Internal", "''x", "plain", "", "a=b"],
)
def test_escape_round_trips(text):
    data = csvio.write_csv(["id", "name"], [{"id": "1", "name": text}])
    _, rows = csvio.read_csv(data)
    assert rows[0]["name"] == text  # an escaped leading tab or CR survives; the escape protects it from stripping


def test_escape_examples():
    assert csvio.escape_cell("-Internal") == "'-Internal"
    assert csvio.escape_cell("'-Internal") == "''-Internal"
    assert csvio.escape_cell("Internal") == "Internal"
    assert csvio.unescape_cell("''-Internal") == "'-Internal"


@pytest.mark.parametrize("text", [" =1+1", "  @cmd", " =HYPERLINK(1)", " \t-1", "\n=1"])
def test_whitespace_before_a_formula_character_is_escaped(text):
    # Spreadsheets skip leading whitespace, so " =1+1" is a formula too.
    assert csvio.escape_cell(text) == "'" + text
    assert csvio.escape_cell(" plain") == " plain"


def test_a_row_with_more_cells_than_headings_is_refused():
    # Extra cells were dropped without a word; trailing empty cells (a spreadsheet's export) still pass.
    with pytest.raises(csvio.CsvRejected, match="Row 3 has more cells than there are headings"):
        csvio.read_csv(b"name,lifecycle\nZoom,active\nSlack,active,extra\n")
    assert csvio.read_csv(b"name,lifecycle\nZoom,active,,\n")[1] == [{"name": "Zoom", "lifecycle": "active"}]


def test_the_column_cap_is_on_headings_and_on_every_line():
    cap = csvio.MAX_COLUMNS
    ok = ",".join(f"c{i}" for i in range(cap)).encode() + b"\n" + b"x," * (cap - 1) + b"x\n"
    headers, rows = csvio.read_csv(ok)
    assert len(headers) == cap and len(rows[0]) == cap
    wide = ",".join(f"c{i}" for i in range(cap + 1)).encode() + b"\nx\n"
    with pytest.raises(csvio.CsvRejected, match="more than 300 columns"):
        csvio.read_csv(wide)
    with pytest.raises(csvio.CsvRejected, match="more than 300 cells"):
        csvio.read_csv(b"name\n" + b"x," * (cap + len(csvio.REPORT_COLUMNS)) + b"x\n")  # 304 non-empty cells
    with pytest.raises(csvio.CsvRejected, match="more than 300 columns"):
        csvio.read_csv(b"import_row," + wide)  # 301 own headings plus a report column: under the line cap, over 300
    # Trailing empty cells (an Excel used range) don't count: 400 of them after the heading and the row.
    assert csvio.read_csv(b"name" + b"," * 400 + b"\nZoom" + b"," * 400 + b"\n")[1] == [{"name": "Zoom"}]
    # The error report's three leading columns on a full-width file still read back.
    report = (
        ",".join([*csvio.REPORT_COLUMNS, *(f"c{i}" for i in range(cap))]).encode() + b"\n" + b"x," * (cap + 2) + b"x\n"
    )
    assert len(csvio.read_csv(report)[0]) == cap + 3
    # Blank headings are left out of the row dicts.
    assert csvio.read_csv(b"name,,,\nZoom,,,\n")[1] == [{"name": "Zoom"}]


def test_bom_and_utf8():
    data = csvio.write_csv(["name"], [{"name": "Café"}])
    assert data.startswith(b"\xef\xbb\xbf")
    assert csvio.read_csv(data)[1] == [{"name": "Café"}]
    assert csvio.read_csv("name\nCafé\n".encode())[1] == [{"name": "Café"}]  # BOM optional


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"name\n", "no rows"),
        ("name\nCaf\xe9\n".encode("latin-1"), "isn't UTF-8"),
        (b"name,Name\nx,y\n", "same heading"),
        (b"name\n" + b"x\n" * 5001, "limit is 5,000"),
        (b"name\n" + b"x" * (5 * 1024 * 1024), "larger than 5 MB"),
    ],
)
def test_rejections(data, message):
    with pytest.raises(csvio.CsvRejected) as e:
        csvio.read_csv(data)
    assert message in str(e.value)


def test_template_detection_and_tags():
    assert csvio.is_template(["name", "lifecycle", "tags"], Kind.APPLICATION)
    assert not csvio.is_template(["App name", "Status"], Kind.APPLICATION)
    assert not csvio.is_template(["name", "component_type"], Kind.APPLICATION)
    assert csvio.split_tags("Finance; finance ;HR;;") == ["Finance", "HR"]
    assert csvio.tags_text(["hr", "Finance"]) == "Finance; hr"
