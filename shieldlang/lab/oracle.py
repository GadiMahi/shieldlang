"""Independent oracles: judge an *interpreted* value, specified separately from both pipelines."""
from __future__ import annotations

import re
from typing import Any, Dict, List


def path_escapes_root(value: str, base_depth: int = 0) -> bool:
    """Segment-stack evaluation (own implementation, not posixpath): True if the relative path climbs above its base
    directory or is absolute. '.' and empty segments are ignored; backslash is NOT a separator in this model."""
    if value.startswith("/"):
        return True
    depth = 0
    for seg in value.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            depth -= 1
            if depth < -base_depth:
                return True
        else:
            depth += 1
    return False


def violated(oracle: Dict[str, Any], effective: str) -> bool:
    k = oracle["kind"]
    if k == "contains_substring":
        needle = oracle["needle"]
        return needle.lower() in effective.lower() if oracle.get("case_insensitive", True) else needle in effective
    if k == "path_escapes_root":
        return path_escapes_root(effective, int(oracle.get("base_depth", 0)))
    if k == "regex":
        return re.search(oracle["pattern"], effective, re.I if oracle.get("ignore_case") else 0) is not None
    if k == "equals_any":
        return effective in oracle["values"]
    raise ValueError(f"unknown oracle kind {k!r}")


def targets(oracle: Dict[str, Any]) -> List[str]:
    """Strings the search tries to smuggle through (derived from the oracle, or supplied for regex oracles)."""
    k = oracle["kind"]
    if k == "contains_substring":
        return [oracle["needle"]]
    if k == "path_escapes_root":
        return oracle.get("seed_targets") or ["../secret.txt"]
    if k == "equals_any":
        return list(oracle["values"])
    if k == "regex":
        if not oracle.get("seed_targets"):
            raise ValueError("regex oracle needs seed_targets")
        return list(oracle["seed_targets"])
    raise ValueError(k)
