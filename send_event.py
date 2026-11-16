#!/usr/bin/env python3
"""Create and send one HMAC-signed event to the relay."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
import uuid


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--payload", required=True, help="JSON object")
    args = parser.parse_args()
    payload = json.loads(args.payload)
    if not isinstance(payload, dict):
        raise SystemExit("--payload must be a JSON object")
    event = {
        "event_id": str(uuid.uuid4()),
        "project": args.project,
        "source": args.source,
        "kind": args.kind,
        "payload": payload,
    }
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    digest = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{nonce}\n{digest}".encode()
    signature = hmac.new(os.environ["RELAY_KEY_SECRET"].encode(), canonical, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        os.environ["RELAY_INGEST_URL"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Relay-Key-Id": os.environ["RELAY_KEY_ID"],
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Nonce": nonce,
            "X-Relay-Signature": f"sha256={signature}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.read().decode())


if __name__ == "__main__":
    main()
