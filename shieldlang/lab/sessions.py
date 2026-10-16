"""In-memory registry of live Protocol Studio sessions (one gateway+backend+engine per session).

A session is server state, not browser state: the client only ever holds a session_id. Oldest sessions are evicted
past MAX_SESSIONS so a demo server does not leak loopback ports. Each session remembers the protocol hash it was
built from, so the API can tell the client when the displayed source has since changed (stale evidence).
"""
from __future__ import annotations

import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .traffic import LabEnv

MAX_SESSIONS = 16


class Session:
    def __init__(self, source: str, protocol: str, mode: str):
        self.id = uuid.uuid4().hex[:12]
        self.tmp = tempfile.TemporaryDirectory(prefix="shieldlang-studio-")
        self.env = LabEnv(source, mode=mode, protocol=protocol, workdir=self.tmp.name)
        self.source = source
        self.protocol_name = protocol
        self.mode = mode
        self.created = time.time()
        self.touched = self.created
        ir = next(p for p in self.env.compiled.protocols if p.name == protocol)
        self.protocol_hash = ir.hash
        self.policy_hash = __import__("hashlib").sha256(source.encode()).hexdigest()[:16]

    def close(self):
        try:
            self.env.close()
        finally:
            self.tmp.cleanup()

    def touch(self):
        self.touched = time.time()


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}

    def create(self, source: str, protocol: str, mode: str) -> Session:
        s = Session(source, protocol, mode)
        with self._lock:
            self._sessions[s.id] = s
            while len(self._sessions) > MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda x: x.touched)
                if oldest.id == s.id:
                    break
                self._sessions.pop(oldest.id).close()
        return s

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            s = self._sessions.get(sid)
            if s:
                s.touch()
            return s

    def close(self, sid: str) -> bool:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s:
            s.close()
        return s is not None

    def restart(self, sid: str) -> Optional[Session]:
        """Close the gateway/engine but keep the on-disk store, then reopen it under the SAME session id.
        Reopening calls ProtocolEngine.recover(): reserved-without-send-intent -> released, sent-without-outcome ->
        unknown. This is the recovery pass from docs/protocol_semantics.md, not a process kill; tests/test_protocol_runtime.py
        exercises the real subprocess crash (os._exit) version of the same logic."""
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            s.env.server.stop()
            try:
                s.env.backend.stop()
            except Exception:
                pass
            from .demo_backend import DemoBackend
            from .gateway import Gateway, GatewayServer
            from pathlib import Path
            s.env.backend = DemoBackend(str(Path(s.tmp.name) / "backend.db")).start()
            from ..protocol.engine import ProtocolEngine, ProtocolStore
            ir = next(p for p in s.env.compiled.protocols if p.name == s.protocol_name)
            recovered = {}
            store = ProtocolStore(s.env.store_path)
            eng = ProtocolEngine(ir, store, shadow=(s.mode == "observe"), recover=False)
            recovered = eng.recover() if s.mode != "observe" else {"released": 0, "unknown": 0}
            s.env.engine = eng
            s.env.gateway = Gateway(s.env.compiled, s.env.backend.port, mode=s.mode, engine=eng)
            s.env.server = GatewayServer(s.env.gateway).start()
            s.touch()
            return s, recovered
