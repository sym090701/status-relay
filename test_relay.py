from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

from relay import RelayConfig, RelayServer
from agent import AgentStore, poll_once


def signed_request(url: str, body: bytes, secret: str, key_id: str = "demo", nonce: str | None = None):
    timestamp = str(int(time.time()))
    nonce = nonce or secrets_token()
    digest = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{nonce}\n{digest}".encode()
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Relay-Key-Id": key_id,
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Nonce": nonce,
            "X-Relay-Signature": f"sha256={signature}",
        },
        method="POST",
    )


def secrets_token() -> str:
    return uuid.uuid4().hex


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = object.__new__(RelayConfig)
        cfg.bind = "127.0.0.1"
        cfg.port = 0
        cfg.db_path = Path(self.temp.name) / "relay.sqlite3"
        cfg.ingest_keys = {"demo": "webhook-secret"}
        cfg.ingest_projects = {"demo": "p"}
        cfg.agent_tokens = {"nas158": "agent-secret"}
        cfg.max_body_bytes = 65536
        cfg.timestamp_skew = 300
        cfg.event_ttl = 3600
        cfg.max_events = 100000
        self.server = RelayServer((cfg.bind, cfg.port), cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def json_request(self, request):
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read())

    def test_ingest_pull_ack(self):
        event = {
            "event_id": "evt-1",
            "project": "p",
            "source": "quota",
            "kind": "runtime_status",
            "payload": {"healthy": True},
        }
        body = json.dumps(event, separators=(",", ":")).encode()
        self.assertEqual(self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret"))["accepted"], True)

        fetch = urllib.request.Request(
            f"{self.base}/v1/events?cursor=0",
            headers={"Authorization": "Bearer agent-secret"},
        )
        result = self.json_request(fetch)
        self.assertEqual(result["events"][0]["event_id"], "evt-1")
        self.assertEqual(result["next_cursor"], 1)

        ack = urllib.request.Request(
            f"{self.base}/v1/ack",
            data=json.dumps({"event_ids": ["evt-1"]}).encode(),
            headers={
                "Authorization": "Bearer agent-secret",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        self.assertEqual(self.json_request(ack)["acked"], 1)
        result = self.json_request(fetch)
        self.assertEqual(result["events"], [])

    def test_replay_is_rejected(self):
        event = {
            "event_id": "evt-replay",
            "project": "p",
            "source": "s",
            "kind": "k",
            "payload": {},
        }
        body = json.dumps(event).encode()
        nonce = "same-nonce"
        request = signed_request(f"{self.base}/v1/events", body, "webhook-secret", nonce=nonce)
        self.json_request(request)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret", nonce=nonce))
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

    def test_duplicate_event_is_idempotent(self):
        event = {
            "event_id": "evt-duplicate",
            "project": "p",
            "source": "s",
            "kind": "k",
            "payload": {"n": 1},
        }
        body = json.dumps(event).encode()
        first = self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret"))
        second = self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret"))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["seq"], second["seq"])

    def test_agent_persists_and_advances_cursor_after_ack(self):
        event = {
            "event_id": "evt-agent",
            "project": "p",
            "source": "s",
            "kind": "status",
            "payload": {"ok": True},
        }
        body = json.dumps(event).encode()
        self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret"))
        store = AgentStore(Path(self.temp.name) / "agent")
        self.assertEqual(poll_once(self.base, "agent-secret", store, 10), 1)
        self.assertEqual(store.cursor(), 1)
        self.assertEqual(poll_once(self.base, "agent-secret", store, 10), 0)
        with closing(__import__("sqlite3").connect(store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_cross_project_ingest_is_forbidden(self):
        event = {"event_id": "other", "project": "other", "source": "s", "kind": "k", "payload": {}}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.json_request(signed_request(f"{self.base}/v1/events", json.dumps(event).encode(), "webhook-secret"))
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertEqual(self.server.store.pending_count(), 0)

    def test_capacity_rejects_new_events_but_keeps_duplicates_and_pending(self):
        self.server.store.max_events = 1
        event = {"event_id": "first", "project": "p", "source": "s", "kind": "k", "payload": {}}
        body = json.dumps(event).encode()
        self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret"))
        self.assertTrue(self.json_request(signed_request(f"{self.base}/v1/events", body, "webhook-secret"))["duplicate"])
        event["event_id"] = "second"
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.json_request(signed_request(f"{self.base}/v1/events", json.dumps(event).encode(), "webhook-secret"))
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        self.assertEqual(self.server.store.pending_count(), 1)


if __name__ == "__main__":
    unittest.main()
