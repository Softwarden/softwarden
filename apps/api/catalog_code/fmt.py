"""Catalog-as-code files: safe YAML in, stable YAML out (docs/CATALOG_AS_CODE.md).

Reading never constructs Python objects from tags. The file is composed into PyYAML's node tree by a SafeLoader that
refuses anchors and aliases (so no alias expansion, the "billion laughs", can happen however the result is used later),
and bounds nesting and the number of nodes; this module then turns the nodes into plain values itself, recording the
line of every key so an error can name it. Only the YAML 1.1 core scalars are accepted: null, booleans, integers,
floats and strings. A timestamp-looking scalar (2026-01-31) stays the text it is: dates are text in this format. Any
other tag (!!binary, !!set, !custom, the merge key <<) is an error, as are duplicate keys and keys that aren't plain
text.

Writing is deterministic, so the same records always give the same bytes: keys in the order the caller built them
(never sorted by the dumper), no anchors, block style, multi-line text as literal blocks, no line folding."""

from __future__ import annotations

import math
from typing import Any

import yaml

API_VERSION = "softwarden/v1"
MAX_FILE_BYTES = 512 * 1024
MAX_NESTING = 12
MAX_NODES = 20_000
MAX_NUMBER_CHARS = 30  # far past any value a field takes (custom integers stop at 2**53), far below Python's limit

NULL = "tag:yaml.org,2002:null"
BOOL = "tag:yaml.org,2002:bool"
INT = "tag:yaml.org,2002:int"
FLOAT = "tag:yaml.org,2002:float"
STR = "tag:yaml.org,2002:str"
TIMESTAMP = "tag:yaml.org,2002:timestamp"
MAP = "tag:yaml.org,2002:map"
SEQ = "tag:yaml.org,2002:seq"

Path = tuple[str | int, ...]


class YamlError(Exception):
    """A file that can't be read; `line` is 1-based, or None when the whole file is the problem."""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line


class _Loader(yaml.SafeLoader):
    """SafeLoader that composes nodes only: no aliases or anchors, bounded nesting and size."""

    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent, index):
        event = self.peek_event()
        line = event.start_mark.line + 1 if event.start_mark else None
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise YamlError("Anchors and aliases (& and *) aren't allowed; write each value out.", line)
        self._depth += 1
        self._nodes += 1
        if self._depth > MAX_NESTING:
            raise YamlError(f"The file nests more than {MAX_NESTING} levels.", line)
        if self._nodes > MAX_NODES:
            raise YamlError(f"The file has more than {MAX_NODES:,} values.", line)
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def load(data: bytes) -> tuple[Any, dict[Path, int]]:
    """(value, {path: line}) for one YAML document. Raises YamlError."""
    if len(data) > MAX_FILE_BYTES:
        raise YamlError(f"The file is larger than {MAX_FILE_BYTES // 1024} KB.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise YamlError("The file isn't UTF-8 text.") from None
    loader = _Loader(text.removeprefix("﻿"))
    try:
        node = loader.get_single_node()
    except YamlError:
        raise
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        problem = e.problem or e.context or "This isn't valid YAML"
        raise YamlError(f"{problem[0].upper()}{problem[1:]}.", mark.line + 1 if mark else None) from None
    except yaml.YAMLError:
        raise YamlError("This isn't valid YAML.") from None
    finally:
        loader.dispose()
    if node is None:
        raise YamlError("The file is empty.", 1)
    lines: dict[Path, int] = {}
    return _convert(loader, node, (), lines), lines


def _line(node) -> int:
    return node.start_mark.line + 1


def _convert(loader: _Loader, node, path: Path, lines: dict[Path, int]) -> Any:
    lines.setdefault(path, _line(node))
    if isinstance(node, yaml.MappingNode):
        if node.tag != MAP:
            raise YamlError(f"The tag {node.tag} isn't allowed.", _line(node))
        out: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag not in (STR, INT, BOOL, NULL, FLOAT):
                raise YamlError("Keys must be plain text (merge keys << aren't allowed).", _line(key_node))
            key = key_node.value
            if key in out:
                raise YamlError(f'"{key}" appears twice.', _line(key_node))
            child = (*path, key)
            lines[child] = _line(key_node)
            out[key] = _convert(loader, value_node, child, lines)
        return out
    if isinstance(node, yaml.SequenceNode):
        if node.tag != SEQ:
            raise YamlError(f"The tag {node.tag} isn't allowed.", _line(node))
        return [_convert(loader, item, (*path, i), lines) for i, item in enumerate(node.value)]
    tag = node.tag
    if tag == NULL:
        return None
    if tag in (STR, TIMESTAMP):
        return node.value
    if tag not in (BOOL, INT, FLOAT):
        raise YamlError(f"The tag {tag} isn't allowed; use plain text, numbers, true or false.", _line(node))
    if tag == INT and len(node.value) > MAX_NUMBER_CHARS:
        raise YamlError(f"Numbers can have at most {MAX_NUMBER_CHARS} characters; quote it if it is text.", _line(node))
    # PyYAML's constructors raise plain Python errors on malformed scalars (an explicit !!int abc, !!bool maybe, an
    # empty !!int, a number past Python's digit limit): each is this file's error, never a server error.
    try:
        if tag == BOOL:
            return loader.construct_yaml_bool(node)
        if tag == INT:
            return loader.construct_yaml_int(node)
        value = loader.construct_yaml_float(node)
    except (ValueError, KeyError, IndexError, OverflowError, TypeError):
        kind = {BOOL: "true or false", INT: "a whole number", FLOAT: "a number"}[tag]
        raise YamlError(f"{node.value[:40]!r} isn't {kind}.", _line(node)) from None
    if not math.isfinite(value):
        raise YamlError("Infinity and NaN (.inf, .nan) aren't allowed.", _line(node))
    return value


# ---- writing -----------------------------------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Lists indented under their key; never anchors (the reader refuses them)."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)

    def ignore_aliases(self, data):
        return True


def _represent_str(dumper: _Dumper, value: str):
    return dumper.represent_scalar(STR, value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _represent_str)


def dump(doc: dict[str, Any]) -> bytes:
    text = yaml.dump(
        doc,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1_000_000,
        indent=2,
    )
    return text.encode("utf-8")


def camel(name: str) -> str:
    """business_owner -> businessOwner: the YAML key of a field the API calls by its snake_case name."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def path_text(path: Path) -> str:
    """("spec", "instances", 0, "url") -> "spec.instances[0].url"."""
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out
