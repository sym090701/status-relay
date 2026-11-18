#!/usr/bin/env python3
"""Small HMAC-authenticated event relay for public services and a NAS agent."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def _parse_map(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, secret = item.partition("=")
        if not sep or not key or not secret:
            raise ValueError("credentials must use key=value format")
        result[key] = secret
    return result


class RelayConfig:
    def __init__(self) -> None:
        self.bind = os.environ.get("RELAY_BIND", "127.0.0.1")
        self.port = int(os.environ.get("RELAY_PORT", "8780"))
        self.db_path = Path(os.environ.get("RELAY_DB", "./data/relay.sqlite3"))
        self.ingest_keys = _parse_map(os.environ.get("RELAY_INGEST_KEYS", ""))
        self.ingest_projects = _parse_map(os.environ.get("RELAY_INGEST_PROJECTS", ""))
        self.agent_tokens = _parse_map(os.environ.get("RELAY_AGENT_TOKENS", ""))
        self.max_body_bytes = int(os.environ.get("RELAY_MAX_BODY_BYTES", "65536"))
        self.timestamp_skew = int(os.environ.get("RELAY_TIMESTAMP_SKEW", "300"))
        self.event_ttl = int(os.environ.get("RELAY_EVENT_TTL", "604800"))
        self.max_events = int(os.environ.get("RELAY_MAX_EVENTS", "100000"))
        if not self.ingest_keys:
            raise ValueError("RELAY_INGEST_KEYS is required")
        if not self.agent_tokens:
            raise ValueError("RELAY_AGENT_TOKENS is required")
        if set(self.ingest_projects) != set(self.ingest_keys):
            raise ValueError("RELAY_INGEST_PROJECTS must bind every ingest key to a project")
        if self.max_events < 1:
            raise ValueError("RELAY_MAX_EVENTS must be positive")


class RelayStore:
    def __init__(self, path: Path, event_ttl: int, max_events: int) -> None:
        self.path = path
        self.event_ttl = event_ttl
        self.max_events = max_events
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    acked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS events_pending_idx
                    ON events (acked_at, seq, expires_at);
                CREATE TABLE IF NOT EXISTS nonces (
                    key_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (key_id, nonce)
                );
                """
            )

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def remember_nonce(self, key_id: str, nonce: str, expires_at: int) -> bool:
        with self.lock, self.connection() as db:
            now = int(time.time())
            db.execute("DELETE FROM nonces WHERE expires_at < ?", (now,))
            try:
                db.execute(
                    "INSERT INTO nonces (key_id, nonce, expires_at) VALUES (?, ?, ?)",
                    (key_id, nonce, expires_at),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def put_event(self, event: dict[str, Any], now: int) -> tuple[int, bool] | None:
        with self.lock, self.connection() as db:
            row = db.execute("SELECT seq FROM events WHERE event_id = ?", (event["event_id"],)).fetchone()
            if row:
                return int(row["seq"]), True
            db.execute("DELETE FROM events WHERE expires_at < ? AND acked_at IS NOT NULL", (now,))
            if db.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= self.max_events:
                return None
            try:
                cur = db.execute(
                    """
                    INSERT INTO events
                    (event_id, project, source, kind, payload, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["project"],
                        event["source"],
                        event["kind"],
                        json.dumps(event["payload"], ensure_ascii=False, separators=(",", ":")),
                        now,
                        now + self.event_ttl,
                    ),
                )
                return int(cur.lastrowid), False
            except sqlite3.IntegrityError:
                row = db.execute(
                    "SELECT seq FROM events WHERE event_id = ?", (event["event_id"],)
                ).fetchone()
                return int(row["seq"]), True

    def get_events(self, cursor: int, limit: int, now: int) -> list[dict[str, Any]]:
        with self.lock, self.connection() as db:
            db.execute("DELETE FROM events WHERE expires_at < ? AND acked_at IS NOT NULL", (now,))
            rows = db.execute(
                """
                SELECT seq, event_id, project, source, kind, payload, created_at
                FROM events
                WHERE seq > ? AND acked_at IS NULL AND expires_at >= ?
                ORDER BY seq ASC LIMIT ?
                """,
                (cursor, now, limit),
            ).fetchall()
            if rows:
                db.executemany(
                    "UPDATE events SET attempts = attempts + 1 WHERE seq = ?",
                    [(int(row["seq"]),) for row in rows],
                )
            return [
                {
                    "seq": int(row["seq"]),
                    "event_id": row["event_id"],
                    "project": row["project"],
                    "source": row["source"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "created_at": int(row["created_at"]),
                }
                for row in rows
            ]

    def ack(self, event_ids: list[str], now: int) -> int:
        if not event_ids:
            return 0
        with self.lock, self.connection() as db:
            placeholders = ",".join("?" for _ in event_ids)
            cur = db.execute(
                f"UPDATE events SET acked_at = ? WHERE event_id IN ({placeholders}) AND acked_at IS NULL",
                [now, *event_ids],
            )
            return cur.rowcount

    def pending_count(self) -> int:
        with self.lock, self.connection() as db:
            row = db.execute("SELECT COUNT(*) AS n FROM events WHERE acked_at IS NULL").fetchone()
            return int(row["n"])


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "status-relay/0.1"

    @property
    def cfg(self) -> RelayConfig:
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def store(self) -> RelayStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return None
        if length < 0 or length > self.cfg.max_body_bytes:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete body"})
            return None
        return body

    def verify_ingest(self, body: bytes) -> bool:
        key_id = self.headers.get("X-Relay-Key-Id", "")
        timestamp_text = self.headers.get("X-Relay-Timestamp", "")
        nonce = self.headers.get("X-Relay-Nonce", "")
        provided = self.headers.get("X-Relay-Signature", "")
        secret = self.cfg.ingest_keys.get(key_id)
        if not secret or not timestamp_text or not nonce or not provided:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "missing webhook credentials"})
            return False
        try:
            timestamp = int(timestamp_text)
        except ValueError:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid webhook timestamp"})
            return False
        if abs(int(time.time()) - timestamp) > self.cfg.timestamp_skew:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "stale webhook"})
            return False
        digest = hashlib.sha256(body).hexdigest()
        canonical = f"{timestamp}\n{nonce}\n{digest}".encode()
        expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided.removeprefix("sha256="), expected):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid webhook signature"})
            return False
        if not self.store.remember_nonce(key_id, nonce, timestamp + self.cfg.timestamp_skew):
            self.send_json(HTTPStatus.CONFLICT, {"error": "replayed webhook"})
            return False
        return True

    def verify_agent(self) -> bool:
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "agent bearer token required"})
            return False
        if not any(hmac.compare_digest(token, known) for known in self.cfg.agent_tokens.values()):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid agent token"})
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True, "pending": self.store.pending_count()})
            return
        if parsed.path != "/v1/events":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.verify_agent():
            return
        query = parse_qs(parsed.query)
        try:
            cursor = max(0, int(query.get("cursor", ["0"])[0]))
            limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid cursor or limit"})
            return
        events = self.store.get_events(cursor, limit, int(time.time()))
        next_cursor = events[-1]["seq"] if events else cursor
        self.send_json(HTTPStatus.OK, {"events": events, "next_cursor": next_cursor})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self.read_body()
        if body is None:
            return
        if parsed.path == "/v1/events":
            if not self.verify_ingest(body):
                return
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if not isinstance(event, dict):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "event must be an object"})
                return
            required = ("event_id", "project", "source", "kind", "payload")
            if any(not isinstance(event.get(field), str) for field in required[:4]) or not isinstance(event.get("payload"), dict):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid event fields"})
                return
            if not all(isinstance(event.get(field), str) and event[field].strip() for field in required[:4]):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "event metadata must be non-empty strings"})
                return
            if self.cfg.ingest_projects.get(self.headers.get("X-Relay-Key-Id", "")) != event["project"]:
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "key not authorized for project"})
                return
            result = self.store.put_event(event, int(time.time()))
            if result is None:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "queue capacity reached; retry later"})
                return
            seq, duplicate = result
            self.send_json(HTTPStatus.OK, {"accepted": True, "duplicate": duplicate, "seq": seq})
            return
        if parsed.path == "/v1/ack":
            if not self.verify_agent():
                return
            try:
                value = json.loads(body)
            except json.JSONDecodeError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            event_ids = value.get("event_ids") if isinstance(value, dict) else None
            if not isinstance(event_ids, list) or not all(isinstance(item, str) for item in event_ids):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "event_ids must be a string array"})
                return
            count = self.store.ack(event_ids[:100], int(time.time()))
            self.send_json(HTTPStatus.OK, {"acked": count})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


class RelayServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], cfg: RelayConfig) -> None:
        super().__init__(address, RelayHandler)
        self.cfg = cfg
        self.store = RelayStore(cfg.db_path, cfg.event_ttl, cfg.max_events)


def main() -> None:
    cfg = RelayConfig()
    server = RelayServer((cfg.bind, cfg.port), cfg)
    print(f"status-relay listening on {cfg.bind}:{cfg.port}, db={cfg.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
