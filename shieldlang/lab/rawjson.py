"""Raw JSON object-member tokenizer for duplicate-key experiments.

Never reconstructed from a parsed dict: members are read from the raw text in order, so duplicates and escape spellings survive.
Both Python json.loads and JavaScript JSON.parse keep the LAST duplicate; 'first' and 'reject' below are *named adapter
behaviours of a demo component*, not claims about any real parser.
"""
from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple


class RawJsonError(ValueError):
    pass


_WS = " \t\n\r"


def _skip_ws(t: str, i: int) -> int:
    while i < len(t) and t[i] in _WS:
        i += 1
    return i


def _scan_string(t: str, i: int) -> int:
    """t[i] == '"' -> index just past the closing quote."""
    if t[i] != '"':
        raise RawJsonError(f"expected string at {i}")
    i += 1
    while i < len(t):
        c = t[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise RawJsonError("unterminated string")


def _scan_value(t: str, i: int) -> int:
    i = _skip_ws(t, i)
    if i >= len(t):
        raise RawJsonError("unexpected end")
    c = t[i]
    if c == '"':
        return _scan_string(t, i)
    if c in "{[":
        depth = 0
        while i < len(t):
            c = t[i]
            if c == '"':
                i = _scan_string(t, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise RawJsonError("unterminated container")
    j = i
    while j < len(t) and t[j] not in ",}] \t\n\r":
        j += 1
    return j


def members(text: str) -> List[Tuple[str, str, str]]:
    """Top-level members of a JSON object in order: (raw_key_inner, raw_value_text, ...). Returns (key_raw_inner, value_raw, key_decoded)."""
    try:
        json.loads(text)  # validity gate; duplicates are legal for json.loads
    except (ValueError, RecursionError) as exc:
        raise RawJsonError(f"invalid JSON: {exc}") from exc
    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "{":
        raise RawJsonError("top level must be an object")
    i += 1
    out: List[Tuple[str, str, str]] = []
    i = _skip_ws(text, i)
    if text[i] == "}":
        return out
    while True:
        i = _skip_ws(text, i)
        ks = i
        i = _scan_string(text, i)
        key_raw = text[ks + 1:i - 1]
        i = _skip_ws(text, i)
        if text[i] != ":":
            raise RawJsonError("expected ':'")
        i = _skip_ws(text, i + 1)
        vs = i
        i = _scan_value(text, i)
        out.append((key_raw, text[vs:i], json.loads('"' + key_raw + '"')))
        i = _skip_ws(text, i)
        if text[i] == ",":
            i += 1
            continue
        if text[i] == "}":
            return out
        raise RawJsonError("expected ',' or '}'")


def lookup(text: str, name: str, duplicates: str = "last", unescape: bool = True) -> Tuple[Optional[Any], int]:
    """Value of top-level member `name` under an adapter. -> (value or None, number of members with that decoded key).
    duplicates: 'first' | 'last' | 'reject' (raises). unescape=False reads string values as literal inner text."""
    hits = [(k, v) for kr, v, k in members(text) if (k if unescape else kr) == name]
    if not hits:
        return None, 0
    if duplicates == "reject" and len(hits) > 1:
        raise RawJsonError(f"duplicate key {name!r}")
    _, raw = hits[0] if duplicates == "first" else hits[-1]
    if raw.startswith('"') and not unescape:
        return raw[1:-1], len(hits)
    return json.loads(raw), len(hits)
