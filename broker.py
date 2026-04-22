#!/usr/bin/env python3
"""Lightweight local message broker for multi-agent coordination.

Start once: python3 broker.py [--port 9111]
Both agents talk to it via curl or the helper functions below.

Endpoints:
  POST /send         {"to": "architect", "from": "builder", "body": "..."}
  GET  /recv?role=architect[&since=<iso>][&wait=30]
  GET  /status
  POST /clear
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 9111


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class MessageStore:
    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._started_at = utc_now().isoformat()

    def send(self, *, to: str, from_actor: str, body: str) -> dict[str, Any]:
        with self._condition:
            msg = {
                "id": len(self._messages) + 1,
                "to": to,
                "from": from_actor,
                "body": body,
                "at": utc_now().isoformat(),
            }
            self._messages.append(msg)
            self._condition.notify_all()
        return msg

    def recv(self, role: str, since: datetime | None = None) -> list[dict[str, Any]]:
        with self._condition:
            return self._recv_locked(role, since)

    def wait_for_message(self, role: str, since: datetime | None, timeout: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                msgs = self._recv_locked(role, since)
                if msgs:
                    return msgs
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(timeout=remaining)

    def status(self) -> dict[str, Any]:
        with self._condition:
            roles: dict[str, int] = {}
            for m in self._messages:
                roles[m["to"]] = roles.get(m["to"], 0) + 1
            return {
                "total": len(self._messages),
                "by_role": roles,
                "started_at": self._started_at,
            }

    def clear(self) -> None:
        with self._condition:
            self._messages.clear()
            self._condition.notify_all()

    def _recv_locked(self, role: str, since: datetime | None) -> list[dict[str, Any]]:
        return [
            m
            for m in self._messages
            if m["to"] == role
            and (since is None or datetime.fromisoformat(m["at"]) > since)
        ]

class BrokerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], store: MessageStore) -> None:
        super().__init__(server_address, handler_class)
        self.store = store


class BrokerHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/send":
            self._handle_send()
        elif parsed.path == "/clear":
            self._store.clear()
            self._json_response({"cleared": True})
        else:
            self._json_response({"error": f"unknown path: {parsed.path}"}, status=404)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/recv":
            self._handle_recv(parsed)
        elif parsed.path == "/status":
            self._json_response(self._store.status())
        else:
            self._json_response({"error": f"unknown path: {parsed.path}"}, status=404)

    def _handle_send(self) -> None:
        body = self._read_json()
        if not body:
            return
        to = body.get("to", "")
        from_actor = body.get("from", "")
        text = body.get("body", "")
        if not to or not text:
            self._json_response({"error": "to and body are required"}, status=400)
            return
        msg = self._store.send(to=to, from_actor=from_actor, body=text)
        self._json_response(msg, status=201)

    def _handle_recv(self, parsed: Any) -> None:
        params = parse_qs(parsed.query)
        role = params.get("role", [""])[0]
        if not role:
            self._json_response({"error": "role parameter required"}, status=400)
            return
        since_raw = params.get("since", [None])[0]
        if since_raw:
            since_raw = since_raw.replace(" ", "+")
        since = datetime.fromisoformat(since_raw) if since_raw else None
        wait = float(params.get("wait", [0])[0])

        if wait > 0:
            msgs = self._store.wait_for_message(role, since, timeout=min(wait, 60))
        else:
            msgs = self._store.recv(role, since)

        self._json_response({"messages": msgs, "count": len(msgs)})

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response({"error": "empty body"}, status=400)
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            self._json_response({"error": f"invalid JSON: {exc}"}, status=400)
            return None

    def _json_response(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        pass

    @property
    def _store(self) -> MessageStore:
        return cast(BrokerHTTPServer, self.server).store


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, store: MessageStore | None = None) -> BrokerHTTPServer:
    return BrokerHTTPServer((host, port), BrokerHandler, store or MessageStore())


def main() -> None:
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    server = make_server(port=port)
    print(f"collab broker running on http://127.0.0.1:{port}", flush=True)
    print(f"  POST /send  {{\"to\": \"architect\", \"from\": \"builder\", \"body\": \"...\"}}", flush=True)
    print(f"  GET  /recv?role=architect[&since=<iso>][&wait=30]", flush=True)
    print(f"  GET  /status", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbroker stopped")
        server.server_close()


if __name__ == "__main__":
    main()
