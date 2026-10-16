"""Transformation library for Bypass Lab: decoding stages (modeled pipelines) and encoders (candidate generation).

Stages are pure str->str functions. A pipeline is a *declared* list of stage names; nothing here claims how any real
product behaves. Python's html/unicodedata/urllib are used as the reference implementations of each named stage.
"""
from __future__ import annotations

import html
import json
import posixpath
import random
import re
import unicodedata
import urllib.parse
from typing import Callable, Dict, List, Optional, Tuple

_HEX = "0123456789abcdefABCDEF"


def percent_decode(s: str) -> str:
    return urllib.parse.unquote(s, encoding="utf-8", errors="replace")


def strip_nul(s: str) -> str:
    return s.replace("\x00", "")


def json_unescape(s: str) -> str:
    """Interpret JSON string escapes inside already-extracted string content."""
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s


def path_normalize(s: str) -> str:
    return posixpath.normpath(s) if s else s


STAGES: Dict[str, Callable[[str], str]] = {
    "percent_decode": percent_decode,
    "nfkc": lambda s: unicodedata.normalize("NFKC", s),
    "nfc": lambda s: unicodedata.normalize("NFC", s),
    "strip_nul": strip_nul,
    "html_unescape": html.unescape,
    "lowercase": str.lower,
    "casefold": str.casefold,
    "path_normalize": path_normalize,
    "json_unescape": json_unescape,
}


def apply_stages(value: str, stages: List[str]) -> Tuple[str, List[Dict[str, str]]]:
    trace = []
    for name in stages:
        if name not in STAGES:
            raise ValueError(f"unknown stage {name!r}")
        out = STAGES[name](value)
        trace.append({"stage": name, "input": value, "output": out})
        value = out
    return value, trace


# ---------------------------------------------------------------- encoders (candidate generation)
_FULLWIDTH: Dict[str, List[str]] = {}


def compat_variants(ch: str) -> List[str]:
    """Characters (BMP) whose NFKC form is exactly `ch`, excluding `ch` itself. Built once from the Unicode database."""
    global _FULLWIDTH
    if not _FULLWIDTH:
        complete = {}
        for cp in range(0x80, 0x10000):
            c = chr(cp)
            if 0xD800 <= cp <= 0xDFFF:
                continue
            n = unicodedata.normalize("NFKC", c)
            if len(n) == 1 and n != c:
                complete.setdefault(n, []).append(c)
        # Publish the complete index at once; concurrent searches must not see a
        # partially built Unicode table and generate a different mutation space.
        _FULLWIDTH = complete
    return _FULLWIDTH.get(ch, [])


def _pct(ch: str, upper=True) -> str:
    out = "".join("%%%02X" % b for b in ch.encode("utf-8"))
    return out if upper else out.lower()


ALT: Dict[str, Callable[[str], List[str]]] = {
    "percent": lambda c: [_pct(c, True), _pct(c, False)],
    "fullwidth": lambda c: compat_variants(c)[:2],
    "html": lambda c: ["&#x%x;" % ord(c), "&#%d;" % ord(c)],
    "json_u": lambda c: ["\\u%04x" % ord(c)] if ord(c) < 0x10000 else [],
    "nul": lambda c: [c + "\x00"],
    "case": lambda c: [c.swapcase()] if c.swapcase() != c else [],
}


def select_positions(s: str, strategy: str, rng: random.Random) -> List[int]:
    idx = list(range(len(s)))
    if strategy == "all":
        return idx
    if strategy == "specials":
        return [i for i in idx if not s[i].isalnum()]
    if strategy == "alnum":
        return [i for i in idx if s[i].isalnum()]
    if strategy.startswith("single:"):
        k = int(strategy.split(":")[1])
        return [k] if k < len(s) else []
    if strategy == "random":
        return [i for i in idx if rng.random() < 0.5]
    raise ValueError(strategy)


def encode(s: str, encoder: str, strategy: str, alt: int, rng: random.Random) -> Optional[str]:
    """Replace the selected characters by the encoder's `alt`-th alternative; None if nothing changed."""
    pos = set(select_positions(s, strategy, rng))
    out, changed = [], False
    for i, c in enumerate(s):
        if i in pos:
            alts = ALT[encoder](c)
            if alts:
                out.append(alts[min(alt, len(alts) - 1)])
                changed = True
                continue
        out.append(c)
    return "".join(out) if changed else None


_UNRESERVED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")


def transport_encode(s: str, kind: str) -> str:
    """Make a payload legal inside the raw location without altering already-present %XX / \\uXXXX atoms."""
    if kind in ("query", "path_param"):
        out, i = [], 0
        while i < len(s):
            c = s[i]
            if c == "%" and i + 2 < len(s) + 0 and s[i + 1] in _HEX and s[i + 2] in _HEX:
                out.append(s[i:i + 3]); i += 3; continue
            if c in _UNRESERVED or (c in "!$'()*,;:@" ):
                out.append(c)
            else:
                out.append(_pct(c))
            i += 1
        return "".join(out)
    if kind == "body_field":
        out, i = [], 0
        while i < len(s):
            c = s[i]
            if c == "\\" and re.match(r"\\u[0-9a-fA-F]{4}", s[i:i + 6]):
                out.append(s[i:i + 6]); i += 6; continue
            if c == '"' or c == "\\":
                out.append("\\" + c)
            elif ord(c) < 0x20:
                out.append("\\u%04x" % ord(c))
            else:
                out.append(c)
            i += 1
        return "".join(out)
    raise ValueError(kind)


def atoms(s: str) -> List[str]:
    """Split a raw payload into reducible atoms: %XX, \\uXXXX, &#..; entities, else single characters."""
    pat = re.compile(r"%[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|&#x?[0-9a-fA-F]+;")
    out, i = [], 0
    while i < len(s):
        m = pat.match(s, i)
        if m:
            out.append(m.group(0)); i = m.end()
        else:
            out.append(s[i]); i += 1
    return out
