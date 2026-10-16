"""Bounded, oracle-guided candidate discovery for Bypass Lab.

The search does not seed itself from fixture ``candidates`` (which are manual regression
examples). Instead it synthesizes starting points from the fixture's declared oracle,
scope, pipeline metadata, and loopback app's declared virtual resources, then applies a
generic mutation grammar. Every reported finding still requires the independent oracle
and a successful local replay. This is a test-harness experiment, not a real-world scanner.
"""
from __future__ import annotations

import copy
import json
import random
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .bypass import run_fixture
from .bypass_fixtures import BY_ID
from .labapp import LabApp

MAX_BUDGET = 1024
MAX_DEPTH = 3
MAX_FINDINGS = 5
MAX_REDUCTION_BUDGET = 500
MAX_TIME_BUDGET_MS = 10000
MAX_SEEDS = 8
MAX_CANDIDATE_CHARS = 2048
MAX_BYTES = 2048
MAX_QUEUE_FACTOR = 4


def fixture_version(fixture_id: str) -> str:
    """Stable SHA-256 identifier for the declared fixture used in an experiment."""
    fx = BY_ID.get(fixture_id)
    if fx is None:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    from .artifacts import content_hash
    return content_hash({k: fx[k] for k in ("scope", "policy_stages", "app_stages", "oracle", "replay")})


def _validate_limits(budget: int, max_depth: int, max_findings: int,
                     reduction_budget: int, time_budget_ms: int) -> None:
    checks = (("budget", budget, 1, MAX_BUDGET),
              ("max_depth", max_depth, 0, MAX_DEPTH),
              ("max_findings", max_findings, 1, MAX_FINDINGS),
              ("reduction_budget", reduction_budget, 1, MAX_REDUCTION_BUDGET),
              ("time_budget_ms", time_budget_ms, 100, MAX_TIME_BUDGET_MS))
    for name, value, lower, upper in checks:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if value < lower:
            qualifier = "non-negative" if lower == 0 else "positive"
            raise ValueError(f"{name} must be a {qualifier} integer")
        if value > upper:
            raise ValueError(f"{name} exceeds the maximum of {upper}")


def _validate_text(raw: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be text")
    if not allow_empty and not raw:
        raise ValueError(f"{name} must be a non-empty string")
    if len(raw) > MAX_CANDIDATE_CHARS:
        raise ValueError(f"{name} exceeds {MAX_CANDIDATE_CHARS} characters")
    try:
        encoded = raw.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} contains an unpaired Unicode surrogate") from exc
    if len(encoded) > MAX_BYTES:
        raise ValueError(f"{name} exceeds {MAX_BYTES} UTF-8 bytes")
    return raw


@contextmanager
def _replay_session(fixture_id: str):
    """Start one loopback server for an entire search or minimization experiment."""
    fx = BY_ID.get(fixture_id)
    if fx is None:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    started = time.monotonic()
    app = LabApp(fx["app_stages"]).start()
    try:
        yield app, started
    finally:
        app.stop()


def _oracle_seeds(fx: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Synthesize starting inputs from the property specification, not preset candidates."""
    scope = fx.get("scope", {})
    oracle = fx.get("oracle", {})
    stages = set(fx.get("policy_stages", []) + fx.get("app_stages", []))
    kind = oracle.get("kind")
    out: List[Tuple[str, str]] = []

    if kind == "contains_substring":
        needle = oracle.get("needle")
        if isinstance(needle, str) and needle:
            out.append((needle, "oracle-derived substring seed"))
            out.append(("prefix " + needle + " suffix", "padded oracle-derived substring seed"))
    elif kind == "path_escapes_root":
        # Choose a path outside the app's public root from its declared virtual file tree.
        # The traversal string itself is synthesized from the oracle's root-escape property.
        try:
            from .labapp import VIRTUAL_FILES
            outside = [name for name in VIRTUAL_FILES if not name.startswith("public/")]
        except (ImportError, AttributeError):
            outside = []
        if outside:
            base_depth = oracle.get("base_depth", 0)
            if not isinstance(base_depth, int) or isinstance(base_depth, bool) or base_depth < 0:
                base_depth = 0
            for name in outside[:4]:
                out.append(("../" * (base_depth + 1) + name,
                            "oracle-derived root-escape seed using declared virtual resource"))
        out.append(("x", "generic path control seed"))
    elif kind == "equals_any":
        field = scope.get("name")
        values = oracle.get("values", [])
        target = next((v for v in values if isinstance(v, str)), None)
        if isinstance(field, str) and field and target is not None:
            out.append((json.dumps({field: target}, separators=(",", ":")),
                        "oracle-derived single-field JSON seed"))
            if any(s.startswith("dup:") for s in stages):
                # Generated from the field name, oracle target, and declared duplicate-key
                # semantics; this is not copied from fx['candidates'].
                benign = "__bypass_lab_control__"
                out.append(("{" + json.dumps(field) + ":" + json.dumps(benign) + "," +
                            json.dumps(field) + ":" + json.dumps(target) + "}",
                            "schema-generated duplicate-key seed"))
    if not out:
        out.append(("x", "generic fallback seed"))
    return out


def _candidate_mutations(fx: Dict[str, Any], raw: str) -> Iterable[Tuple[str, str]]:
    """Apply generic, bounded mutation operators to a candidate."""
    seen = set()

    def emit(value: str, why: str):
        if value != raw and value not in seen and len(value) <= MAX_CANDIDATE_CHARS:
            seen.add(value)
            return value, why
        return None

    variants = [
        (raw.replace("../", "%2e%2e%2f"), "percent-encode traversal components"),
        (raw.replace("%", "%25"), "percent-encode percent signs"),
        (raw.replace("<", "＜").replace(">", "＞"), "replace ASCII delimiters with compatibility forms"),
        (raw.replace("<", "&lt;").replace(">", "&gt;"), "HTML-entity encode delimiters"),
        (raw.replace("\x00", ""), "delete NUL characters"),
        (raw.replace("<", "<\x00"), "insert NUL after opening delimiter"),
        (raw.replace("script", "scr\x00ipt"), "insert NUL within token"),
        (raw.lower(), "lowercase candidate"),
        (raw.upper(), "uppercase candidate"),
        (raw.replace("/", "%2f"), "percent-encode path separators"),
        (raw.replace(".", "%2e"), "percent-encode dots"),
        (raw.replace("\\u", "\\\\u"), "escape Unicode escape introducers"),
        (raw.replace(":", " : ").replace(",", " , "), "vary JSON punctuation whitespace"),
    ]
    for value, why in variants:
        item = emit(value, why)
        if item:
            yield item

    # JSON structure mutations are generated from the declared field and oracle values.
    scope = fx.get("scope", {})
    stages = set(fx.get("policy_stages", []) + fx.get("app_stages", []))
    oracle = fx.get("oracle", {})
    field = scope.get("name")
    if (scope.get("kind") == "body_field" and isinstance(field, str) and field
            and any(s.startswith("dup:") for s in stages) and oracle.get("kind") == "equals_any"):
        values = [v for v in oracle.get("values", []) if isinstance(v, str)]
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                parsed = {}
        except (ValueError, TypeError):
            parsed = {}
        current = parsed.get(field, "__bypass_lab_control__")
        for target in values[:8]:
            for order, first, second in (
                ("control then target", current, target),
                ("target then control", target, current),
            ):
                value = "{" + json.dumps(field) + ":" + json.dumps(first) + "," + json.dumps(field) + ":" + json.dumps(second) + "}"
                item = emit(value, f"generated duplicate-key order: {order}")
                if item:
                    yield item


def search_fixture(fixture_id: str, *, seeds: Optional[List[str]] = None, budget: int = 128,
                   max_depth: int = 2, seed: int = 0, max_findings: int = 3,
                   minimize: bool = True, reduction_budget: int = 128,
                   time_budget_ms: int = 5000) -> Dict[str, Any]:
    """Run one bounded search with one reusable loopback server and one deadline."""
    _validate_limits(budget, max_depth, max_findings, reduction_budget, time_budget_ms)
    if fixture_id not in BY_ID:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer")
    if not isinstance(minimize, bool):
        raise ValueError("minimize must be a boolean")
    if seeds is not None:
        if not isinstance(seeds, list):
            raise ValueError("seeds must be a list of strings")
        if not 1 <= len(seeds) <= MAX_SEEDS:
            raise ValueError(f"seeds may contain at most {MAX_SEEDS} items")
        for index, raw in enumerate(seeds):
            _validate_text(raw, f"seed {index}")
    generated = _oracle_seeds(BY_ID[fixture_id]) if seeds is None else []
    if len(generated) > MAX_SEEDS:
        raise ValueError(f"generated seed corpus exceeds {MAX_SEEDS} items")
    if len(generated) + len(seeds or []) > MAX_SEEDS:
        raise ValueError(f"combined initial seed corpus may contain at most {MAX_SEEDS} items")
    for raw, _source in generated:
        _validate_text(raw, "generated seed")
    with _replay_session(fixture_id) as (app, session_started):
        return _search_fixture_with_app(
            fixture_id, seeds=seeds, budget=budget, max_depth=max_depth, seed=seed,
            max_findings=max_findings, minimize=minimize, reduction_budget=reduction_budget,
            time_budget_ms=time_budget_ms, _app=app, _session_started=session_started,
        )


def _search_fixture_with_app(fixture_id: str, *, seeds: Optional[List[str]] = None, budget: int = 128,
                             max_depth: int = 2, seed: int = 0, max_findings: int = 3,
                             minimize: bool = True, reduction_budget: int = 128,
                             time_budget_ms: int = 5000, _app: LabApp,
                             _session_started: float) -> Dict[str, Any]:
    """Run oracle-guided mutation search, requiring loopback replay for each finding."""
    _validate_limits(budget, max_depth, max_findings, reduction_budget, time_budget_ms)
    if fixture_id not in BY_ID:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer")
    if not isinstance(minimize, bool):
        raise ValueError("minimize must be a boolean")
    if seeds is not None:
        if not isinstance(seeds, list):
            raise ValueError("seeds must be a list of strings")
        if not 1 <= len(seeds) <= MAX_SEEDS:
            raise ValueError(f"seeds may contain at most {MAX_SEEDS} items")
        for index, raw in enumerate(seeds):
            _validate_text(raw, f"seed {index}", allow_empty=False)
    fx = BY_ID[fixture_id]
    generated_seeds = _oracle_seeds(fx) if seeds is None else []
    if len(generated_seeds) > MAX_SEEDS:
        raise ValueError(f"generated seed corpus exceeds {MAX_SEEDS} items")
    if len(generated_seeds) + len(seeds or []) > MAX_SEEDS:
        raise ValueError(f"combined initial seed corpus may contain at most {MAX_SEEDS} items")
    for raw, _source in generated_seeds:
        if len(raw) > MAX_CANDIDATE_CHARS:
            raise ValueError(f"generated seed exceeds {MAX_CANDIDATE_CHARS} characters")
    rng = random.Random(seed)
    started = _session_started
    deadline = started + time_budget_ms / 1000.0

    queue: List[Tuple[str, List[str], int]] = []
    for raw, provenance in generated_seeds:
        queue.append((raw, [provenance], 0))
    for raw in seeds or []:
        queue.append((raw, ["caller-provided seed"], 0))
    dedup = set()
    queue = [item for item in queue if not (item[0] in dedup or dedup.add(item[0]))]

    visited = set()
    findings: List[Dict[str, Any]] = []
    evaluated = 0
    replay_count = 0
    reduction_replay_count = 0
    reduction_evaluations = 0
    stop_reason = "candidate queue exhausted"

    def _count_reduction_replay() -> None:
        nonlocal replay_count, reduction_replay_count
        replay_count += 1
        reduction_replay_count += 1

    def evaluate(raw: str, *, deadline_at: Optional[float] = None) -> Optional[Dict[str, Any]]:
        nonlocal replay_count, reduction_replay_count
        if time.monotonic() >= deadline:
            return None
        if deadline_at is not None and time.monotonic() >= deadline_at:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        preview = run_fixture(fixture_id, raw, replay=False)
        if not preview["policy_would_accept"] or not preview["oracle_violation_on_app_view"]:
            return preview
        replay_count += 1
        if deadline_at is not None:
            reduction_replay_count += 1
        try:
            return run_fixture(fixture_id, raw, replay=True, app=_app, replay_deadline=deadline)
        except (ValueError, UnicodeError, OSError, TimeoutError) as exc:
            return {"fixture_id": fixture_id, "raw": raw, "confirmed_bypass": False,
                    "evaluation_error": f"{type(exc).__name__}: {exc}"}

    while queue and evaluated < budget and time.monotonic() < deadline and len(findings) < max_findings:
        raw, provenance, depth = queue.pop(0)
        if raw in visited:
            continue
        visited.add(raw)
        if len(raw) > MAX_CANDIDATE_CHARS or len(raw.encode("utf-8")) > MAX_BYTES:
            continue
        result = evaluate(raw)
        if result is None:
            break
        evaluated += 1
        if result.get("confirmed_bypass"):
            finding: Dict[str, Any] = {"raw": raw, "depth": depth, "result": result, "provenance": provenance}
            if minimize and time.monotonic() < deadline:
                before = reduction_replay_count
                try:
                    reduction = _minimize_fixture_with_app(
                        fixture_id, raw, budget=reduction_budget,
                        time_budget_ms=max(1, int((deadline - time.monotonic()) * 1000)),
                        _deadline=deadline, _initial_result=result,
                        _on_replay=lambda: _count_reduction_replay(), _app=_app,
                        _session_started=time.monotonic(),
                    )
                    finding["reduction"] = reduction
                except ValueError as exc:
                    finding["reduction_error"] = str(exc)
                reduction_evaluations += max(0, reduction_replay_count - before)
            elif minimize:
                finding["reduction_skipped"] = "shared search deadline expired"
            findings.append(finding)
        if depth < max_depth:
            generated = list(_candidate_mutations(fx, raw))
            rng.shuffle(generated)
            for candidate, why in generated:
                if candidate not in visited and len(candidate.encode("utf-8")) <= MAX_BYTES and len(queue) < budget * MAX_QUEUE_FACTOR:
                    queue.append((candidate, provenance + [why], depth + 1))

    if len(findings) >= max_findings:
        stop_reason = "max_findings reached"
    elif evaluated >= budget:
        stop_reason = "evaluation budget reached"
    elif time.monotonic() >= deadline:
        stop_reason = "shared time budget reached"

    stop_reason = {"max_findings reached": "finding_limit", "evaluation budget reached": "candidate_budget",
                   "shared time budget reached": "time_budget", "candidate queue exhausted": "space_exhausted"}[stop_reason]
    if not queue and evaluated >= budget and max_depth == 0:
        stop_reason = "space_exhausted"
    return {
        "operation": "search", "engine_version": "bounded-mutation/2",
        "experiment": {k: copy.deepcopy(fx[k]) for k in ("scope", "policy_stages", "app_stages", "oracle", "replay")},
        "candidate_budget": budget, "summary": {"confirmed": len(findings), "evaluated": evaluated},
        "fixture_id": fixture_id, "fixture_hash": fixture_version(fixture_id),
        "scope_note": ("Oracle-guided candidate synthesis plus generic mutation operators; preset fixture candidates "
                       "are excluded from the search seed corpus. Findings require independent-oracle agreement and "
                       "local loopback replay. No finding is not a safety proof."),
        "seeds": [raw for raw, _ in generated_seeds] + list(seeds or []),
        "seed_sources": [source for _, source in generated_seeds] + ["caller-provided seed"] * len(seeds or []),
        "budget": budget, "max_depth": max_depth, "seed": seed, "max_findings": max_findings,
        "minimize": minimize, "reduction_budget": reduction_budget, "time_budget_ms": time_budget_ms,
        "evaluated": evaluated, "replay_count": replay_count,
        "reduction_replay_count": reduction_replay_count, "reduction_evaluations": reduction_evaluations,
        "findings": findings, "stop_reason": stop_reason,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def minimize_fixture(fixture_id: str, raw: Optional[str], *, budget: int = 100,
                     time_budget_ms: int = 5000) -> Dict[str, Any]:
    """Minimize a confirmed finding, reusing one local server for the reduction."""
    if fixture_id not in BY_ID:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    raw = _validate_text(raw, "raw")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise ValueError("budget must be a positive integer")
    if budget > MAX_REDUCTION_BUDGET:
        raise ValueError(f"budget exceeds the maximum of {MAX_REDUCTION_BUDGET}")
    if not isinstance(time_budget_ms, int) or isinstance(time_budget_ms, bool) or time_budget_ms < 1:
        raise ValueError("time_budget_ms must be a positive integer")
    if time_budget_ms < 100:
        raise ValueError("time_budget_ms must be at least 100")
    if time_budget_ms > MAX_TIME_BUDGET_MS:
        raise ValueError(f"time_budget_ms exceeds the maximum of {MAX_TIME_BUDGET_MS}")
    with _replay_session(fixture_id) as (app, session_started):
        out = _minimize_fixture_with_app(
            fixture_id, raw, budget=budget, time_budget_ms=time_budget_ms,
            _app=app, _session_started=session_started,
        )
        out["engine_version"] = "bounded-mutation/2"
        out["stop_reason"] = {"1-minimal under character deletion": "deletion_fixed_point",
                              "reduction budget reached": "evaluation_budget",
                              "reduction time budget reached": "time_budget"}.get(out["stop_reason"], out["stop_reason"])
        return out


def _minimize_fixture_with_app(fixture_id: str, raw: Optional[str], *, budget: int = 100,
                               time_budget_ms: int = 5000, _deadline: Optional[float] = None,
                               _initial_result: Optional[Dict[str, Any]] = None,
                               _on_replay: Optional[Callable[[], None]] = None,
                               _app: LabApp, _session_started: float) -> Dict[str, Any]:
    """Greedy deletion reducer; accepted deletions preserve replay-confirmed status.

    Private keyword arguments allow search to pass its absolute deadline and account for
    every replay without extending the original search's wall-clock budget.
    """
    if fixture_id not in BY_ID:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    raw = _validate_text(raw, "raw")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise ValueError("budget must be a positive integer")
    if budget > MAX_REDUCTION_BUDGET:
        raise ValueError(f"budget exceeds the maximum of {MAX_REDUCTION_BUDGET}")
    if not isinstance(time_budget_ms, int) or isinstance(time_budget_ms, bool) or time_budget_ms < 1:
        raise ValueError("time_budget_ms must be a positive integer")
    if time_budget_ms > MAX_TIME_BUDGET_MS:
        raise ValueError(f"time_budget_ms exceeds the maximum of {MAX_TIME_BUDGET_MS}")
    started = _session_started
    local_deadline = started + time_budget_ms / 1000.0
    deadline = min(local_deadline, _deadline) if _deadline is not None else local_deadline
    original = raw
    evaluations = 0
    accepted_deletions = 0

    def eval_candidate(candidate: str) -> Optional[Dict[str, Any]]:
        if time.monotonic() >= deadline:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if _on_replay:
            _on_replay()
        try:
            return run_fixture(fixture_id, candidate, replay=True, app=_app, replay_deadline=deadline)
        except (ValueError, UnicodeError, OSError, TimeoutError) as exc:
            return {"fixture_id": fixture_id, "raw": candidate, "confirmed_bypass": False,
                    "evaluation_error": f"{type(exc).__name__}: {exc}"}

    current = raw
    current_result = _initial_result
    if current_result is None:
        current_result = eval_candidate(raw)
        if current_result is None:
            return _reduction_result(fixture_id, original, original, evaluations,
                                     "shared time budget reached before initial replay", None, started)
    if current_result.get("evaluation_error"):
        return _reduction_result(fixture_id, original, original, evaluations,
                                 "inconclusive replay error during initial evaluation", current_result, started)
    if not current_result.get("confirmed_bypass"):
        raise ValueError("input is not a replay-confirmed finding")

    granularity = 2
    stop_reason = "1-minimal under character deletion"
    inconclusive_error = False
    trial_error: Optional[Dict[str, Any]] = None
    while len(current) >= 2 and evaluations < budget and time.monotonic() < deadline:
        chunk = max(1, (len(current) + granularity - 1) // granularity)
        reduced = False
        for start_idx in range(0, len(current), chunk):
            if evaluations >= budget or time.monotonic() >= deadline:
                break
            candidate = current[:start_idx] + current[start_idx + chunk:]
            if not candidate or len(candidate) > MAX_CANDIDATE_CHARS:
                continue
            result = eval_candidate(candidate)
            if result is None:
                break
            evaluations += 1
            if result.get("evaluation_error"):
                stop_reason = "inconclusive replay error"
                inconclusive_error = True
                # Preserve evidence for `current`, which is still the last confirmed input.
                # A failed deletion trial is diagnostic data, not replacement evidence.
                trial_error = {
                    "candidate": candidate,
                    "evaluation_error": result.get("evaluation_error"),
                }
                break
            if result.get("confirmed_bypass"):
                current, current_result = candidate, result
                accepted_deletions += 1
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if inconclusive_error:
            break
        if time.monotonic() >= deadline:
            break
        if not reduced:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)

    if inconclusive_error:
        stop_reason = "inconclusive replay error"
    elif evaluations >= budget:
        stop_reason = "reduction budget reached"
    elif time.monotonic() >= deadline:
        stop_reason = "shared time budget reached" if _deadline is not None else "reduction time budget reached"
    original_bytes = len(original.encode("utf-8"))
    reduced_bytes = len(current.encode("utf-8"))
    if stop_reason == "1-minimal under character deletion":
        minimality = "Every accepted deletion retained a replay-confirmed finding; single-character deletion fixed point only, not a globally shortest witness."
    elif stop_reason.startswith("inconclusive replay error"):
        minimality = "No minimality claim: a replay error made at least one deletion inconclusive."
    else:
        minimality = "Reduction incomplete: minimality is not established because a time or evaluation budget stopped reduction."
    return {
        "operation": "minimize", "fixture_id": fixture_id, "fixture_hash": fixture_version(fixture_id),
        "original_raw": original, "raw": current, "original_bytes": original_bytes,
        "reduced_bytes": reduced_bytes, "evaluations": evaluations, "accepted_deletions": accepted_deletions, "stop_reason": stop_reason,
        "minimality": minimality, "result": current_result, "trial_error": trial_error,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _reduction_result(fixture_id: str, original: str, reduced: str, evaluations: int,
                      stop_reason: str, result: Optional[Dict[str, Any]], started: float) -> Dict[str, Any]:
    original_bytes = len(original.encode("utf-8"))
    reduced_bytes = len(reduced.encode("utf-8"))
    if result is None:
        result = {"fixture_id": fixture_id, "raw": original, "confirmed_bypass": False,
                  "replay": {"skipped": True},
                  "note": "No replay was started because the shared time budget had expired."}
    return {
        "operation": "minimize", "fixture_id": fixture_id, "fixture_hash": fixture_version(fixture_id),
        "original_raw": original, "raw": reduced, "original_bytes": original_bytes,
        "reduced_bytes": reduced_bytes, "evaluations": evaluations, "stop_reason": stop_reason,
        "minimality": "No minimality claim: the initial candidate was not confirmed or the deadline expired.",
        "result": result, "trial_error": None, "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }
