"""Request body parsers: DRF's, plus the hostile-input check of core/input.py.

A body is refused as a 400 naming the field when a string holds a NUL character or an unpaired surrogate, or when it
nests past core.input.MAX_DEPTH; a body nested deeper than Python's recursion limit is a malformed-request 400 rather
than a RecursionError. Every view reads its body through one of these (settings.REST_FRAMEWORK
DEFAULT_PARSER_CLASSES; the upload views name the multipart one), so no service ever sees such a string.
"""

from __future__ import annotations

from rest_framework import parsers
from rest_framework.exceptions import ParseError, UnsupportedMediaType

from core.input import body_errors
from core.problems import FieldErrors


def _check(data) -> None:
    errors = body_errors(data)
    if errors:
        raise FieldErrors(errors)


def _charset(media_type: str | None) -> str:
    for param in (media_type or "").split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return ""


class JSONParser(parsers.JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        # JSON exchanged between systems is UTF-8 (RFC 8259 §8.1): any other declared charset is a 415, not decoded
        # in it. No charset, or utf-8 in any spelling, is read as UTF-8.
        if _charset(media_type) not in ("", "utf-8", "utf8", "us-ascii"):  # ASCII is a subset of UTF-8
            raise UnsupportedMediaType(media_type, "Send JSON as UTF-8 (charset=utf-8 or no charset).")
        try:
            data = super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError("JSON parse error - the body nests too deeply.") from None
        _check(data)
        return data


class MultiPartParser(parsers.MultiPartParser):
    """Text fields and file names; file contents are each upload's own business (imports read them as CSV or XLSX)."""

    def parse(self, stream, media_type=None, parser_context=None):
        parsed = super().parse(stream, media_type, parser_context)
        _check({name: _one_or_list(parsed.data.getlist(name)) for name in parsed.data})
        _check({name: _one_or_list([f.name or "" for f in parsed.files.getlist(name)]) for name in parsed.files})
        return parsed


def _one_or_list(values: list):
    return values[0] if len(values) == 1 else values
