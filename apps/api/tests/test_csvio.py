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
