"""Bounded, independent language comparison for scalar string field blocks.

This checks field acceptance, NOT whole-program equivalence, diagnostics,
backtracking complexity, or correctness of generated Python/JavaScript.
The checker traverses synchronized DFA states directly; it does not call
the optimizer's product construction or reuse its length-subsumption logic.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .ir.ir import Block, Module
from .lang import ast as A
from .lang.lexer import UNITS
from .regex.charset import CharSet, Partition
from .regex.dfa import DFA


class Unsupported(ValueError):
    pass


def _constant(value, consts):
    if isinstance(value, A.Num):
        return value.value * UNITS[value.unit] if value.unit else value.value
    if isinstance(value, A.Ref):
        if value.name not in consts:
            raise Unsupported(f"Unresolved constant {value.name}")
        return consts[value.name]
    if isinstance(value, A.Unary) and value.op == "-":
        return -_constant(value.operand, consts)
    if isinstance(value, A.Binary):
        a, b = _constant(value.left, consts), _constant(value.right, consts)
        if value.op == "+": return a + b
        if value.op == "-": return a - b
        if value.op == "*": return a * b
        if value.op == "/" and b:
            return a // b if isinstance(a, int) and isinstance(b, int) else a / b
        raise Unsupported("Unsupported constant arithmetic")
    if isinstance(value, A.Node):
        raise Unsupported("Unevaluated constraint expression")
    return value


def _literal_language(words):
    """Exact finite language; unlike '$', an enum never admits a final LF."""
    children, accepts = [{}], set()
    for word in words:
        node = 0
        for ch in word:
            if ch not in children[node]:
                children[node][ch] = len(children)
                children.append({})
            node = children[node][ch]
        accepts.add(node)
    part = Partition(CharSet.of(ch) for row in children for ch in row)
    dead = len(children)
    delta = [[dead] * len(part) for _ in range(dead + 1)]
    for node, row in enumerate(children):
        for ch, dst in row.items():
            delta[node][part.classify(ord(ch))] = dst
    tags = [frozenset([0]) if node in accepts else frozenset() for node in range(dead + 1)]
    return DFA(part, delta, 0, tags, dead)


@dataclass
class FieldLanguage:
    source: str
    name: str
    required: bool
    dfas: list = field(default_factory=list)
    minimum: int = 0
    maximum: Optional[int] = None

    def accepts(self, states, length):
        return (length >= self.minimum and (self.maximum is None or length <= self.maximum)
                and all(dfa.accepting(s) for dfa, s in zip(self.dfas, states)))


def field_language(block: Block, module: Module, consts=None) -> FieldLanguage:
    consts = consts or {}
    loads = [i for i in block.instrs if i.op == "LOAD"]
    if block.kind != "field" or len(loads) != 1:
        raise Unsupported("Expected one field load")
    load = loads[0]
    language = FieldLanguage(load.args[0], load.args[1], False)
    types = [i for i in block.instrs if i.op == "CHECK_TYPE"]
    if len(types) != 1 or types[0].args != [load.dst, "string"] or types[0].each:
        raise Unsupported("Only scalar string and string-enum fields are supported")
    optional = False
    for ins in block.instrs:
        if ins.each:
            raise Unsupported("List element checks are not supported")
        if ins.op in ("LOAD", "LABEL", "CHECK_TYPE"):
            continue
        if ins.op == "REQUIRE":
            language.required = True
        elif ins.op == "JMP_IF_MISSING":
            optional = True
        elif ins.op == "CHECK_LEN":
            lo, hi = (_constant(x, consts) for x in ins.args[1:])
            if any(x is not None and (not isinstance(x, int) or isinstance(x, bool) or x < 0) for x in (lo, hi)):
                raise Unsupported("Length limits must be nonnegative integers")
            language.minimum = max(language.minimum, lo or 0)
            if hi is not None:
                language.maximum = hi if language.maximum is None else min(language.maximum, hi)
        elif ins.op == "CHECK_ENUM":
            language.dfas.append(_literal_language(ins.args[1]))
        elif ins.op == "MATCH":
            language.dfas.append(module.dfas[ins.args[1]].dfa)
        else:
            raise Unsupported(f"Unsupported field instruction {ins.op}")
        if ins.op in ("REQUIRE", "JMP_IF_MISSING", "CHECK_LEN", "CHECK_ENUM", "MATCH") and ins.args[0] != load.dst:
            raise Unsupported("Constraint uses a different register")
    if language.required == optional:
        raise Unsupported("Expected exactly one required/optional presence guard")
    # The checker relies on structured field-block failure edges.
    for ins in block.instrs:
        if ins.target is not None and ins.target != block.end_label:
            raise Unsupported("Non-structured control flow in field block")
    return language


def compare_languages(before: FieldLanguage, after: FieldLanguage, budget=20000, both_directions=False):
    """BFS with length saturation. Exhaustion proves equality for this subset.

    A counterexample proves a difference even if the remaining search is cut
    short. Bounds and unsupported cases never become an equality verdict.
    """
    examples = []
    if before.required != after.required:
        examples.append({"value": None, "missing": True, "accepted_before": not before.required,
                         "accepted_after": not after.required})
        if not both_directions:
            return {"status": "different", "complete": False, "states": 0, "examples": examples}
    dfas = before.dfas + after.dfas
    part = Partition(cs for dfa in dfas for cs in dfa.partition.classes)
    representatives = sorted((cs.sample() for cs in part.classes), key=lambda ch: (not ch.isprintable(), ch))
    split = len(before.dfas)
    cap = max(before.minimum, after.minimum, before.maximum or 0, after.maximum or 0) + 1
    start = (tuple(d.start for d in dfas), 0)
    parents = {start: None}
    todo = deque([start])
    directions = {(e["accepted_before"], e["accepted_after"]) for e in examples}
    edges = 0

    def result(status, complete, reason=None):
        out = {"status": status, "complete": complete, "states": len(parents), "examples": examples,
               "alphabet_classes": len(part), "length_saturation": cap}
        if reason: out["reason"] = reason
        return out

    while todo:
        node = todo.popleft()
        states, length = node
        old = before.accepts(states[:split], length)
        new = after.accepts(states[split:], length)
        if old != new and (old, new) not in directions:
            chars, cursor = [], node
            while parents[cursor] is not None:
                previous, ch = parents[cursor]
                chars.append(ch)
                cursor = previous
            examples.append({"value": "".join(reversed(chars)), "missing": False,
                             "accepted_before": old, "accepted_after": new})
            directions.add((old, new))
            if not both_directions or len(directions) == 2:
                return result("different", False)
        for ch in representatives:
            edges += 1
            if edges > budget * 32:
                return result("different" if examples else "inconclusive", False, "Transition budget exceeded")
            nxt = (tuple(d.delta[s][d.partition.classify(ord(ch))] for d, s in zip(dfas, states)),
                   min(length + 1, cap))
            if nxt not in parents:
                if len(parents) >= budget:
                    return result("different" if examples else "inconclusive", False, "State budget exceeded")
                parents[nxt] = (node, ch)
                todo.append(nxt)
    return result("different" if examples else "equivalent", True)


def _fields(module):
    return {(fn.method, fn.path, b.attrs["source"], b.attrs["field"]): b
            for fn in module.functions for b in fn.blocks if b.kind == "field"}


def verify_optimizations(before: Module, after: Module, consts=None, budget=20000):
    old, new = _fields(before), _fields(after)
    checks = []
    for key in sorted(old.keys() | new.keys()):
        base = {"method": key[0], "path": key[1], "source": key[2], "field": key[3]}
        if key not in old or key not in new:
            checks.append(dict(base, status="unsupported", reason="Field added or removed"))
            continue
        try:
            a = field_language(old[key], before, consts)
            b = field_language(new[key], after, consts)
            checks.append(dict(base, **compare_languages(a, b, budget)))
        except Unsupported as exc:
            checks.append(dict(base, status="unsupported", reason=str(exc)))
    counts = {state: sum(c["status"] == state for c in checks)
              for state in ("equivalent", "different", "inconclusive", "unsupported")}
    return {"scope": "Scalar string-field acceptance before and after optimization",
            "exclusions": ["Whole-program behavior", "Conditional rules and scans", "Error codes and ordering",
                           "Generated backend correctness", "Numeric and list fields"],
            "checks": checks, "counts": counts,
            "status": "failed" if counts["different"] else "partial" if counts["unsupported"] or counts["inconclusive"] else "verified" if checks else "not_applicable"}


def compare_policies(before_source, after_source, budget=20000):
    """Compare field languages, then replay candidates against BOTH full VMs.

    Full-program witnesses are concrete evidence. Absence of one does NOT
    certify whole-program equality: guards/scans may mask field differences.
    """
    from .compiler import compile_source
    before, after = compile_source(before_source, "before.shl"), compile_source(after_source, "after.shl")
    if not before.ok or not after.ok:
        return {"status": "compile_error", "before": before.to_json()["diagnostics"],
                "after": after.to_json()["diagnostics"]}
    old, new = _fields(before.opt_ir), _fields(after.opt_ir)
    checks = []
    for key in sorted(old.keys() | new.keys()):
        entry = {"method": key[0], "path": key[1], "source": key[2], "field": key[3]}
        if key not in old or key not in new:
            checks.append(dict(entry, status="unsupported", reason="Field or route added/removed; comparison requires a shared field"))
            continue
        try:
            a = field_language(old[key], before.opt_ir, before.model.consts)
            b = field_language(new[key], after.opt_ir, after.model.consts)
            comparison = compare_languages(a, b, budget, both_directions=True)
            checks.append(dict(entry, **comparison))
        except Unsupported as exc:
            checks.append(dict(entry, status="unsupported", reason=str(exc)))
    witnesses = []
    seen = set()
    for check in checks:
        for example in check.get("examples", []):
            for result in (before, after):
                request = _seed_request(result, check, budget, after if result is before else before)
                if request is None:
                    continue
                _set_value(request, check["source"], check["field"], example["value"], example["missing"])
                request.pop("_template", None)
                request.pop("_params", None)
                import json
                fingerprint = json.dumps(request, sort_keys=True)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                a = before.vm().run(request, trace=False)
                b = after.vm().run(request, trace=False)
                if a["allowed"] != b["allowed"]:
                    witnesses.append({"request": request, "allowed_before": a["allowed"],
                                      "allowed_after": b["allowed"], "field": check["field"],
                                      "errors_before": a["errors"], "errors_after": b["errors"],
                                      "replayed": True})
    return {"status": "behavior_change_found" if witnesses else "field_differences" if any(c["status"] == "different" for c in checks) else "no_field_difference_found",
            "scope": "Shared scalar string fields; field witnesses are not whole-request witnesses",
            "whole_program_equivalence": "not_checked",
            "checks": checks, "witnesses": witnesses,
            "note": "Routes, guards, body limits, scans, numeric fields and error behavior require separate analysis."
            }


def _set_value(request, source, name, value, missing=False):
    if source == "param":
        values = request.setdefault("_params", {})
        values[name] = "sample" if missing else str(value)
        template = request["_template"]
        for key, val in values.items():
            template = template.replace("{" + key + "}", val)
        request["path"] = template
        return
    target = request.setdefault({"field": "body", "header": "headers", "query": "query"}[source], {})
    parts = name.split(".") if source == "field" else [name.lower() if source == "header" else name]
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    if missing:
        target.pop(parts[-1], None)
    else:
        target[parts[-1]] = value


def _seed_request(result, check, budget, other=None):
    rule = next((r for r in result.model.rules if r.method == check["method"] and r.path == check["path"]), None)
    if rule is None:
        return None
    request = {"method": "GET" if rule.method == "ANY" else rule.method,
               "path": rule.path, "body": {}, "query": {}, "headers": {}, "_template": rule.path, "_params": {}}
    fields = _fields(result.opt_ir)
    other_fields = _fields(other.opt_ir) if other is not None else {}
    for f in rule.fields:
        if f.optional and f.source != "param":
            continue
        if f.type.name in ("string", "enum"):
            key = (rule.method, rule.path, f.source, f.name)
            try:
                language = field_language(fields[key], result.opt_ir, result.model.consts)
                # Prefer values accepted by both versions so other fields do
                # not unnecessarily mask the candidate field's difference.
                if key in other_fields and (f.source, f.name) != (check["source"], check["field"]):
                    peer = field_language(other_fields[key], other.opt_ir, other.model.consts)
                    common = FieldLanguage(f.source, f.name, True, language.dfas + peer.dfas,
                                           max(language.minimum, peer.minimum),
                                           min(x for x in (language.maximum, peer.maximum) if x is not None)
                                           if language.maximum is not None or peer.maximum is not None else None)
                    language = common
                empty = FieldLanguage(f.source, f.name, True, minimum=1, maximum=0)
                witness = compare_languages(empty, language, budget)
                example = next((e for e in witness["examples"] if not e["missing"] and e["accepted_after"]), None)
                if example is None:
                    return None
                value = example["value"]
            except Unsupported:
                return None
        elif f.type.name in ("int", "number"):
            value = f.lo if f.lo is not None else 0
        elif f.type.name == "bool":
            value = False
        else:
            return None
        _set_value(request, f.source, f.name, value)
    return request
