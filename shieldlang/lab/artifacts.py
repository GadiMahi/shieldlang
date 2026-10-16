"""Content-addressed artifacts and bounded run records for the lab pages.

Everything derived (compile results, runs, findings) names the exact content hashes it was produced from.
Editing a source produces a new hash; stored records are immutable and never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
MAX_RUNS = 100
MAX_SOURCES = 500
MAX_RECORDINGS = 50
MAX_RUN_BYTES = 2 * 1024 * 1024


def data_dir() -> Path:
    p = Path(os.environ.get("SHIELDLANG_DATA") or (ROOT / ".shieldlang_data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(obj: Any) -> str:
    text = obj if isinstance(obj, str) else canonical(obj)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ArtifactStore:
    """SQLite-backed store: sources, recordings, runs. Retention is bounded; deletion is explicit."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else data_dir() / "lab.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sources(hash TEXT PRIMARY KEY, kind TEXT, name TEXT, text TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS recordings(hash TEXT PRIMARY KEY, name TEXT, doc TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, kind TEXT, created REAL, doc TEXT, truncated INTEGER);
            """)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            # sqlite3's own context manager commits/rolls back but does not
            # close. Leaked handles prevent deleting run databases on Windows.
            c.close()

    # ---- sources ----
    def put_source(self, text: str, kind: str = "policy", name: str = "") -> str:
        h = content_hash(text)
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?)", (h, kind, name, text, time.time()))
            c.execute("DELETE FROM sources WHERE hash NOT IN (SELECT hash FROM sources ORDER BY created DESC LIMIT ?)",
                      (MAX_SOURCES,))
        return h

    def get_source(self, h: str) -> Optional[str]:
        with self._conn() as c:
            r = c.execute("SELECT text FROM sources WHERE hash=?", (h,)).fetchone()
        return r["text"] if r else None

    def list_sources(self, kind: str = "policy", limit: int = 30) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT hash,name,created FROM sources WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, limit)).fetchall()
        return [{"hash": r["hash"], "name": r["name"], "created": r["created"]} for r in rows]

    # ---- recordings ----
    def put_recording(self, doc: Dict[str, Any]) -> str:
        h = content_hash(doc.get("entries", []))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO recordings VALUES (?,?,?,?)",
                      (h, doc.get("name", "recording"), canonical(doc), time.time()))
            c.execute("DELETE FROM recordings WHERE hash NOT IN (SELECT hash FROM recordings ORDER BY created DESC LIMIT ?)",
                      (MAX_RECORDINGS,))
        return h

    def get_recording(self, h: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT doc FROM recordings WHERE hash=?", (h,)).fetchone()
        return json.loads(r["doc"]) if r else None

    def list_recordings(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT hash,name,doc,created FROM recordings ORDER BY created DESC").fetchall()
        out = []
        for r in rows:
            d = json.loads(r["doc"])
            out.append({"hash": r["hash"], "name": r["name"], "created": r["created"],
                        "entries": len(d.get("entries", [])), "description": d.get("description", "")})
        return out

    def delete_recording(self, h: str) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM recordings WHERE hash=?", (h,)).rowcount > 0

    # ---- runs ----
    def put_run(self, kind: str, doc: Dict[str, Any]) -> str:
        created = time.time()
        rid = f"run-{kind}-{int(created * 1000):x}-{content_hash(doc)[7:13]}"
        doc = dict(doc, run_id=rid, kind=kind, created=created)
        text = canonical(doc)
        truncated = 0
        if len(text.encode("utf-8")) > MAX_RUN_BYTES:
            doc = {k: v for k, v in doc.items() if k not in ("entries", "candidates", "timeline", "ledger")}
            doc["truncated"] = "run record exceeded the retention size limit; detail dropped"
            text, truncated = canonical(doc), 1
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (rid, kind, created, text, truncated))
            c.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY created DESC LIMIT ?)", (MAX_RUNS,))
        return rid

    def get_run(self, rid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT doc FROM runs WHERE id=?", (rid,)).fetchone()
        return json.loads(r["doc"]) if r else None

    def list_runs(self, kind: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        q, args = "SELECT id,kind,created,doc FROM runs", []
        if kind:
            q += " WHERE kind=?"
            args.append(kind)
        q += " ORDER BY created DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["doc"])
            out.append({"run_id": r["id"], "kind": r["kind"], "created": r["created"], "mode": d.get("mode"),
                        "artifacts": d.get("artifacts"), "summary": d.get("summary"), "truncated": d.get("truncated")})
        return out

    def delete_run(self, rid: str) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM runs WHERE id=?", (rid,)).rowcount > 0

    def delete_all(self, kind: Optional[str] = None) -> int:
        with self._lock, self._conn() as c:
            if kind:
                return c.execute("DELETE FROM runs WHERE kind=?", (kind,)).rowcount
            return c.execute("DELETE FROM runs").rowcount

    def export(self, kind: Optional[str] = None) -> Dict[str, Any]:
        runs = [self.get_run(r["run_id"]) for r in self.list_runs(kind, MAX_RUNS)]
        return {"format": "shieldlang-lab-export/1", "exported": time.time(), "runs": runs}


_default: Optional[ArtifactStore] = None
_default_lock = threading.Lock()


def default_store() -> ArtifactStore:
    global _default
    with _default_lock:
        if _default is None or _default.path.parent != data_dir():
            _default = ArtifactStore()
        return _default
