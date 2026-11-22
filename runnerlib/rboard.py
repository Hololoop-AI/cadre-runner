"""A small client for the Rust blackboard (`blackboard serve`), stdlib only.

The board speaks HTTP on a Unix socket: POST|GET /v1/events, POST /v1/claims,
POST /v1/claims/<seq>/renew and /complete, GET|PUT /v1/cursors/<consumer>,
GET /v1/health. An event is a header (namespace, topic, key, who, optional
correlation_id and idempotency_key, open `data`) plus the fields of its
kind: an `ask` has `question` and `expects`, an `answer` has `answer` and
`ask_seq`, and so on. The board validates both; a refused write comes back
here as a BoardRefused with the board's own message.

This is not runnerlib/blackboard.py. That one is the runner's own board, in
SQLite, which the runner's heartbeats and task events still use; this one
carries what cadres hand each other (Learn's study asks and answers first).

Where the board is: $BLACKBOARD_SOCKET, else
$XDG_RUNTIME_DIR/qadre/blackboard.sock — the board's own default. While the
picatrix shims stand, the old $XDG_RUNTIME_DIR/picatrix/blackboard.sock is
used when nothing is at the qadre path but something is at the old one.
"""

import http.client
import json
import os
import socket
import urllib.parse
from pathlib import Path

TIMEOUT_S = 5.0


class BoardError(Exception):
    """The board could not be reached, or answered with something unreadable."""


class BoardRefused(BoardError):
    """The board answered and said no (a bad event, a lost lease)."""

    def __init__(self, status: int, body: dict):
        self.status, self.body = status, body
        super().__init__(f"{status}: {body.get('message') or body}")


def default_socket() -> Path:
    if os.environ.get("BLACKBOARD_SOCKET"):
        return Path(os.environ["BLACKBOARD_SOCKET"])
    run = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    new, old = run / "qadre" / "blackboard.sock", run / "picatrix" / "blackboard.sock"
    return old if not new.exists() and old.exists() else new


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("blackboard", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def who(source: str, machine: str = "", session_id: str = "") -> dict:
    """The writer of an event: `source` like "cadre/learn", this machine."""
    out = {"source": source, "machine": machine or socket.gethostname()}
    if session_id:
        out["session_id"] = session_id
    return out


class Client:
    def __init__(self, path=None, timeout: float = TIMEOUT_S):
        self.path = str(path or default_socket())
        self.timeout = timeout

    def _call(self, method: str, url: str, body=None):
        conn = _UnixConnection(self.path, self.timeout)
        try:
            conn.request(method, url, body=None if body is None else json.dumps(body),
                         headers={"Content-Type": "application/json"} if body is not None else {})
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as e:
            raise BoardError(f"no board at {self.path}: {e}") from e
        finally:
            conn.close()
        if resp.status == 204:
            return None
        try:
            data = json.loads(raw) if raw else None
        except ValueError as e:
            raise BoardError(f"unreadable answer from the board: {raw[:200]!r}") from e
        if resp.status >= 400:
            raise BoardRefused(resp.status, data if isinstance(data, dict) else {"message": str(data)})
        return data

    def health(self) -> dict:
        return self._call("GET", "/v1/health")

    def up(self) -> bool:
        try:
            self.health()
            return True
        except BoardError:
            return False

    def emit(self, namespace: str, topic: str, key: str, kind: str, fields: dict, *,
             who: dict, data=None, correlation_id=None, idempotency_key=None) -> dict:
        """Write one event; returns it as stored (with `seq` and `ts`).
        `fields` are the kind's own (an ask's question and expects)."""
        ev = {"namespace": namespace, "topic": topic, "key": key, "who": who,
              "kind": kind, **fields}
        if data:
            ev["data"] = data
        if correlation_id:
            ev["correlation_id"] = correlation_id
        if idempotency_key:
            ev["idempotency_key"] = idempotency_key
        return self._call("POST", "/v1/events", ev)

    def read(self, after: int = 0, **filt) -> list[dict]:
        """Events after seq `after`, oldest first, matching the filter
        (namespace, topic, key, kind, correlation_id)."""
        q = {"after": int(after), **{k: v for k, v in filt.items() if v}}
        return self._call("GET", "/v1/events?" + urllib.parse.urlencode(q)) or []

    def claim(self, consumer: str, lease_ms: int = 60_000, **filt) -> dict | None:
        """Take the oldest unclaimed event matching the filter, under a lease;
        None when there is none."""
        return self._call("POST", "/v1/claims", {
            "consumer": consumer, "lease_ms": int(lease_ms),
            "filter": {k: v for k, v in filt.items() if v}})

    def renew(self, consumer: str, seq: int, lease_ms: int = 60_000) -> None:
        self._call("POST", f"/v1/claims/{int(seq)}/renew",
                   {"consumer": consumer, "lease_ms": int(lease_ms)})

    def complete(self, consumer: str, seq: int) -> None:
        self._call("POST", f"/v1/claims/{int(seq)}/complete", {"consumer": consumer})

    def cursor(self, consumer: str) -> int:
        got = self._call("GET", f"/v1/cursors/{urllib.parse.quote(consumer, safe='')}")
        return int((got or {}).get("cursor") or 0)

    def set_cursor(self, consumer: str, seq: int) -> None:
        self._call("PUT", f"/v1/cursors/{urllib.parse.quote(consumer, safe='')}",
                   {"cursor": int(seq)})
