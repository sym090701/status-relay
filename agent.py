#!/usr/bin/env python3
"""NAS-side pull agent. It stores received events in a local SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any


class AgentStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "events.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    project TEXT NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    received_at INTEGER NOT NULL
                );
                """
            )

    def cursor(self) -> int:
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
            return int(row[0]) if row else 0

    def save_events(self, events: list[dict[str, Any]]) -> int:
        with closing(sqlite3.connect(self.path)) as db, db:
            for event in events:
                db.execute(
                    """
                    INSERT OR IGNORE INTO events
                    (event_id, seq, project, source, kind, created_at, payload, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["seq"],
                        event["project"],
                        event["source"],
                        event["kind"],
                        event["created_at"],
                        json.dumps(event["payload"], ensure_ascii=False, separators=(",", ":")),
                        int(time.time()),
                    ),
                )
            return max((event["seq"] for event in events), default=self.cursor())

    def advance_cursor(self, cursor: int) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO meta(key, value) VALUES('cursor', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(cursor),),
            )


def request_json(url: str, token: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())


def poll_once(base_url: str, token: str, store: AgentStore, limit: int) -> int:
    cursor = store.cursor()
    result = request_json(f"{base_url.rstrip('/')}/v1/events?cursor={cursor}&limit={limit}", token)
    events = result.get("events", [])
    if not events:
        return 0
    new_cursor = store.save_events(events)
    request_json(
        f"{base_url.rstrip('/')}/v1/ack",
        token,
        method="POST",
        body={"event_ids": [event["event_id"] for event in events]},
    )
    store.advance_cursor(new_cursor)
    print(f"received={len(events)} cursor={new_cursor}")
    return len(events)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    base_url = os.environ["RELAY_URL"]
    token = os.environ["RELAY_AGENT_TOKEN"]
    store = AgentStore(Path(os.environ.get("AGENT_DATA_DIR", "./data/nas-agent")))
    limit = min(100, max(1, args.limit))
    while True:
        try:
            received = poll_once(base_url, token, store, limit)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            print(f"poll failed: {exc}")
            if args.once:
                sys.exit(1)
            received = 0
        if args.once:
            break
        if received < limit:
            time.sleep(int(os.environ.get("AGENT_POLL_SECONDS", "30")))


if __name__ == "__main__":
    main()
