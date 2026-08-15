#!/usr/bin/env python3
"""Receives Zalo OA webhook deliveries and stores them.

Zalo has no history endpoint for a group, so anything not captured as it arrives is
gone. This does the least it can: verify, store verbatim, answer 200. Parsing and
extraction happen later, from the stored rows, so a bad parse never loses a message.

    python scripts/lavabo_webhook.py --port 8770
    python scripts/lavabo_webhook.py --port 8770 --insecure   # local testing only

Zalo needs a public HTTPS URL, so in practice this sits behind a tunnel:

    cloudflared tunnel --url http://localhost:8770
    # or: ngrok http 8770

then register  https://<tunnel>/webhook  in the OA app's webhook settings.

SIGNATURE NOT VERIFIED AGAINST THE DOCS. developers.zalo.me was unreachable while this
was written. Zalo's documented scheme is a SHA-256 of appId + data + timestamp + OA
secret, sent as X-ZEvent-Signature; that is implemented below and MUST be confirmed
before this faces the internet. Until then it fails closed: without ZALO_OA_SECRET set
the server refuses to start unless --insecure is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lavabo.config import Config  # noqa: E402
from lavabo.connectors.zalo_oa import is_message_event, parse_event  # noqa: E402
from lavabo.store import Store  # noqa: E402

SIGNATURE_HEADER = "X-ZEvent-Signature"


def expected_signature(app_id: str, body: bytes, timestamp: str, secret: str) -> str:
    """SHA-256 of appId + data + timestamp + OA secret, per Zalo's documented scheme."""
    material = app_id.encode() + body + timestamp.encode() + secret.encode()
    return "mac=" + hashlib.sha256(material).hexdigest()


class Handler(BaseHTTPRequestHandler):
    db_path: Path
    secret: str
    app_id: str
    insecure: bool
    seen: int = 0
    stored: int = 0

    def log_message(self, *args) -> None:
        pass

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # Zalo probes the URL before saving it, and a human will too.
        if self.path.startswith("/webhook"):
            self._reply(200, {"ok": True, "service": "lavabo-webhook",
                              "seen": Handler.seen, "stored": Handler.stored})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/webhook"):
            self._reply(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)

        if not self.insecure:
            provided = self.headers.get(SIGNATURE_HEADER, "")
            timestamp = self.headers.get("X-ZEvent-Timestamp", "")
            if not timestamp:
                try:
                    timestamp = str(json.loads(body).get("timestamp", ""))
                except (json.JSONDecodeError, AttributeError):
                    timestamp = ""
            wanted = expected_signature(self.app_id, body, timestamp, self.secret)
            if not hmac.compare_digest(provided, wanted):
                # 200 on purpose: a rejected delivery should not make Zalo retry it
                # forever, and the reason is logged here rather than advertised.
                print(f"  [{_now()}] REJECTED: bad signature")
                self._reply(200, {"ok": False})
                return

        Handler.seen += 1
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            print(f"  [{_now()}] REJECTED: body is not JSON")
            self._reply(200, {"ok": False})
            return

        # Answer first-class citizens only, but store everything: an event shape we do
        # not recognise today is still evidence, and it cannot be re-fetched.
        event = parse_event(payload)
        if event is None:
            print(f"  [{_now()}] skipped: no event id in payload")
            self._reply(200, {"ok": True})
            return

        with Store(self.db_path) as store:
            fresh = store.save_oa_event(event)

        if fresh:
            Handler.stored += 1
            kind = "order" if is_message_event(payload) else "event"
            who = event.get("sender_name") or event.get("sender_id") or "?"
            head = (event.get("text") or "").strip().splitlines()
            preview = head[0][:60] if head else "(no text)"
            print(f"  [{_now()}] {kind} from {who}: {preview}")
        else:
            print(f"  [{_now()}] duplicate delivery {event['event_id']}, ignored")

        self._reply(200, {"ok": True})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--insecure", action="store_true",
                    help="skip signature checking — local testing only, never public")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    secret = os.environ.get("ZALO_OA_SECRET", "")
    app_id = os.environ.get("ZALO_APP_ID", "")

    if not args.insecure and not secret:
        print("ZALO_OA_SECRET is not set, so deliveries cannot be verified.\n"
              "Put it in .env, or pass --insecure for local testing on a port the\n"
              "internet cannot reach. Refusing to start unverified.", file=sys.stderr)
        return 2

    cfg = Config.load()
    Handler.db_path = cfg.db_path
    Handler.secret = secret
    Handler.app_id = app_id
    Handler.insecure = args.insecure

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  Lavabo webhook on http://{args.host}:{args.port}/webhook")
    print(f"  signature check: {'OFF (insecure)' if args.insecure else 'on'}")
    print(f"  storing to: {cfg.db_path}")
    print("\n  Expose it with a tunnel, then register the https URL in the OA app:")
    print(f"    cloudflared tunnel --url http://localhost:{args.port}")
    print("\n  Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  stopped — {Handler.stored} stored / {Handler.seen} received")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
