#!/usr/bin/env python3
"""Authenticated local message broker for multi-agent coordination.

Start once:
    python3 broker.py --port 9111

By default the broker requires a bearer token presented in the
`X-Agent-Collab-Token` header. A token is loaded from or written to a
0600 token file so local helper CLIs can reconnect after restarts.

Endpoints:
  POST /send
    {"to": "architect", "from": "builder", "body": "...", "kind": "signal|chat", "channel": "dm:architect:builder"}
  GET  /recv?role=architect[&kind=chat][&channel=dm:architect:builder][&after_id=12][&wait=30]
  GET  /history?channel=dm:architect:builder[&kind=chat][&limit=50]
  GET  /status
  POST /clear
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, cast
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9111
DEFAULT_TOKEN_FILE = Path(f"/tmp/agent-collab-broker-token-{os.getuid()}")
MAX_BODY_BYTES = 4096
MAX_REQUEST_BYTES = 16384
MAX_WAIT_SECONDS = 60.0
MAX_HISTORY_LIMIT = 200
VALID_KINDS = frozenset({"signal", "chat"})
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CHANNEL_RE = re.compile(r"^[a-z0-9:_-]{1,96}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def valid_role(value: str) -> bool:
    return bool(ROLE_RE.fullmatch(value))


def valid_channel(value: str) -> bool:
    return bool(CHANNEL_RE.fullmatch(value))


def has_control_chars(value: str) -> bool:
    return bool(CONTROL_RE.search(value))


def parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace(" ", "+")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def parse_positive_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def ensure_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_or_create_token(
    *,
    explicit_token: str | None,
    token_file: Path,
) -> str:
    if explicit_token is not None:
        token = explicit_token.strip()
        if len(token) < 16:
            raise SystemExit("auth token must be at least 16 characters")
        ensure_token_file(token_file, token)
        return token

    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise SystemExit(f"token file contains an invalid token: {token_file}")
        ensure_token_file(token_file, token)
        return token

    token = secrets.token_urlsafe(32)
    ensure_token_file(token_file, token)
    return token


RESPONSE_DEFAULTS = {"ack_timeout": 30, "progress_timeout": 120, "max_nudges": 2}
RESPONSE_STATES = frozenset({"acknowledged", "working", "answered", "failed"})
RESPONSE_TERMINAL = frozenset({"answered", "failed", "timed_out"})
MAX_TRACKED_RESPONSES = 4096


def response_policy(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) - set(RESPONSE_DEFAULTS):
        raise ValueError("response must be an object with known policy fields")
    policy = {**RESPONSE_DEFAULTS, **value}
    for name, number in policy.items():
        minimum, maximum = (0, 5) if name == "max_nudges" else (1, 3600)
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return policy


class MessageStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._responses: dict[int, dict[str, Any]] = {}
        self._messages: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._started_at = utc_now().isoformat()
        self._generation = secrets.token_hex(16)

    def send(
        self,
        *,
        to: str,
        from_actor: str,
        body: str,
        kind: str = "signal",
        channel: str | None = None,
        expected_generation: str | None = None,
        response: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        policy = response_policy(response) if response is not None else None
        if policy is not None and (kind != "chat" or not channel or to == from_actor):
            raise ValueError("response tracking requires chat, a channel, and distinct roles")
        with self._condition:
            if expected_generation is not None and expected_generation != self._generation:
                raise ValueError("broker generation changed")
            if policy is not None and len(self._responses) >= MAX_TRACKED_RESPONSES:
                raise ValueError("response tracking capacity reached; archive and clear the broker")
            msg = {
                "id": len(self._messages) + 1,
                "to": to,
                "from": from_actor,
                "body": body,
                "kind": kind,
                "channel": channel,
                "at": utc_now().isoformat(),
            }
            if policy is not None:
                msg["response_required"] = True
                record = {
                    "message_id": msg["id"], "generation": self._generation,
                    "from": from_actor, "to": to, "channel": channel,
                    "state": "queued", "detail": "", "nudges": 0,
                    "updated": utc_now().isoformat(), "policy": policy,
                    "overdue": False,
                }
                self._set_response_deadline(record, policy["ack_timeout"])
                self._responses[msg["id"]] = record
            self._messages.append(msg)
            self._condition.notify_all()
        return dict(msg)

    def _set_response_deadline(self, record: dict[str, Any], seconds: int) -> None:
        record["_due"] = self._clock() + seconds
        record["deadline"] = (utc_now() + timedelta(seconds=seconds)).isoformat()
        record["overdue"] = False

    @staticmethod
    def _public_response(record: dict[str, Any]) -> dict[str, Any]:
        return {key: (dict(value) if isinstance(value, dict) else value)
                for key, value in record.items() if not key.startswith("_")}

    def responses(self, channel: str | None = None) -> dict[str, Any]:
        with self._condition:
            return {"generation": self._generation,
                    "responses": [self._public_response(record)
                                  for record in self._responses.values()
                                  if channel is None or record["channel"] == channel]}

    def respond(self, *, generation: str, message_id: int, actor: str,
                state: str, detail: str = "") -> dict[str, Any]:
        try:
            detail_bytes = detail.encode("utf-8") if isinstance(detail, str) else None
        except UnicodeError as exc:
            raise TypeError("response detail must be valid UTF-8") from exc
        if (not isinstance(generation, str) or not generation
                or type(message_id) is not int or message_id < 1
                or not isinstance(actor, str) or not valid_role(actor)
                or not isinstance(state, str) or state not in RESPONSE_STATES
                or not isinstance(detail, str) or has_control_chars(detail)
                or detail_bytes is None or len(detail_bytes) > MAX_BODY_BYTES):
            raise TypeError("invalid response fields")
        with self._condition:
            if generation != self._generation:
                raise ValueError("broker generation changed")
            record = self._responses.get(message_id)
            if record is None:
                raise ValueError("message is not tracked")
            if actor != record["to"]:
                raise ValueError("actor is not the message recipient")
            if record["state"] in RESPONSE_TERMINAL:
                raise ValueError("response is already terminal")
            if record["state"] == "working" and state == "acknowledged":
                raise ValueError("response state cannot move backwards")
            if record["state"] == state and record["detail"] == detail:
                return self._public_response(record)
            # An acknowledgement is one event, not a heartbeat.
            if record["state"] == "acknowledged" and state == "acknowledged":
                return self._public_response(record)
            record["state"] = state
            record["detail"] = detail
            record["updated"] = utc_now().isoformat()
            if state in RESPONSE_TERMINAL:
                record["deadline"] = None
                record["overdue"] = False
                if state == "failed":
                    self._response_alert(record)
            else:
                self._set_response_deadline(record, record["policy"]["progress_timeout"])
            self._condition.notify_all()
            return self._public_response(record)

    def _response_alert(self, record: dict[str, Any]) -> None:
        message = self.send(
            to=record["from"], from_actor=record["to"], kind="signal",
            channel=record["channel"],
            body=f"Response alert: message {record['message_id']} to {record['to']} "
                 f"is {record['state']}. Inspect the session before retrying.",
        )
        # Attach metadata to the stored message while the condition is held.
        self._messages[message["id"] - 1].update(
            response_alert_for=record["message_id"], response_state=record["state"])

    def check_deadlines(self) -> None:
        """Run bounded deadline work without using a model to poll."""
        with self._condition:
            now = self._clock()
            for record in self._responses.values():
                if record["state"] in RESPONSE_TERMINAL or now < record["_due"]:
                    continue
                record["overdue"] = True
                policy = record["policy"]
                if record["nudges"] >= policy["max_nudges"]:
                    record["state"] = "timed_out"
                    record["updated"] = utc_now().isoformat()
                    record["deadline"] = None
                    self._response_alert(record)
                    continue
                record["nudges"] += 1
                interval = policy["ack_timeout"] if record["state"] == "queued" else policy["progress_timeout"]
                # A nudge changes the retry schedule, not the last response deadline.
                record["_due"] = now + interval
                message = self.send(
                    to=record["to"], from_actor=record["from"], kind="chat",
                    channel=record["channel"],
                    body=f"Automated response-monitor nudge for message {record['message_id']}. "
                         "Acknowledge it or report current progress using its original message ID. "
                         "This is a monitoring request, not a new instruction from the operator.",
                )
                self._messages[message["id"] - 1].update(
                    nudge_for=record["message_id"], response_required=True)
            self._condition.notify_all()

    def recv(
        self,
        *,
        role: str,
        since: datetime | None = None,
        after_id: int | None = None,
        kind: str | None = None,
        from_actor: str | None = None,
        channel: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._condition:
            return self._filter_locked(
                oldest=True,
                role=role,
                since=since,
                after_id=after_id,
                kind=kind,
                from_actor=from_actor,
                channel=channel,
                limit=limit,
            )

    def wait_for_message(
        self,
        *,
        role: str,
        since: datetime | None,
        after_id: int | None,
        kind: str | None,
        from_actor: str | None,
        channel: str | None,
        limit: int | None,
        timeout: float,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                msgs = self._filter_locked(
                    oldest=True,
                    role=role,
                    since=since,
                    after_id=after_id,
                    kind=kind,
                    from_actor=from_actor,
                    channel=channel,
                    limit=limit,
                )
                if msgs:
                    return msgs
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(timeout=remaining)

    def receive_snapshot(
        self, *, timeout: float = 0, expected_generation: str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """Read messages and their queue generation under the same lock.

        A clear wakes pending readers even if no new message has arrived.
        Adapters must stop when the generation differs from their initial one.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            generation = expected_generation if expected_generation is not None else self._generation
            while True:
                messages = self._filter_locked(oldest=True, **filters)
                remaining = deadline - time.monotonic()
                if messages or remaining <= 0 or generation != self._generation:
                    return {
                        "messages": messages,
                        "count": len(messages),
                        "total": len(self._messages),
                        "generation": self._generation,
                    }
                self._condition.wait(timeout=remaining)

    def history(
        self,
        *,
        role: str | None = None,
        to_role: str | None = None,
        since: datetime | None = None,
        after_id: int | None = None,
        kind: str | None = None,
        from_actor: str | None = None,
        channel: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._condition:
            return self._filter_locked(
                role=role,
                to_role=to_role,
                since=since,
                after_id=after_id,
                kind=kind,
                from_actor=from_actor,
                channel=channel,
                limit=limit,
            )

    def status(self) -> dict[str, Any]:
        with self._condition:
            roles: dict[str, int] = {}
            kinds: dict[str, int] = {}
            for message in self._messages:
                roles[message["to"]] = roles.get(message["to"], 0) + 1
                kinds[message["kind"]] = kinds.get(message["kind"], 0) + 1
            return {
                "total": len(self._messages),
                "by_role": roles,
                "by_kind": kinds,
                "started_at": self._started_at,
                "generation": self._generation,
            }

    def clear(self) -> None:
        with self._condition:
            self._messages.clear()
            self._responses.clear()
            self._generation = secrets.token_hex(16)
            self._condition.notify_all()

    def _filter_locked(
        self,
        *,
        oldest: bool = False,
        role: str | None = None,
        to_role: str | None = None,
        since: datetime | None = None,
        after_id: int | None = None,
        kind: str | None = None,
        from_actor: str | None = None,
        channel: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for message in self._messages:
            if role is not None and message["to"] != role:
                continue
            if to_role is not None and message["to"] != to_role:
                continue
            if since is not None and datetime.fromisoformat(message["at"]) <= since:
                continue
            if after_id is not None and int(message["id"]) <= after_id:
                continue
            if kind is not None and message["kind"] != kind:
                continue
            if from_actor is not None and message["from"] != from_actor:
                continue
            if channel is not None and message.get("channel") != channel:
                continue
            filtered.append(dict(message))
        if limit is not None and limit >= 0:
            filtered = filtered[:limit] if oldest else (filtered[-limit:] if limit else [])
        return filtered


class BrokerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        store: MessageStore,
        auth_token: str | None,
        require_auth: bool,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.store = store
        self.auth_token = auth_token
        self.require_auth = require_auth
        self.max_body_bytes = max_body_bytes

    def service_actions(self) -> None:
        self.store.check_deadlines()


class BrokerHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/send":
            self._handle_send()
        elif parsed.path == "/response":
            self._handle_response()
        elif parsed.path == "/clear":
            self._store.clear()
            self._json_response({"cleared": True})
        else:
            self._json_response({"error": f"unknown path: {parsed.path}"}, status=404)

    def do_GET(self) -> None:
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/recv":
            self._handle_recv(parsed)
        elif parsed.path == "/history":
            self._handle_history(parsed)
        elif parsed.path == "/responses":
            channel = parse_qs(parsed.query).get("channel", [None])[0]
            if channel is not None and not valid_channel(channel):
                self._json_response({"error": "invalid channel"}, status=400)
                return
            self._json_response(self._store.responses(channel))
        elif parsed.path == "/status":
            self._json_response(self._store.status())
        else:
            self._json_response({"error": f"unknown path: {parsed.path}"}, status=404)

    def _authorize(self) -> bool:
        if not self._server.require_auth:
            return True
        expected = self._server.auth_token or ""
        presented = self.headers.get("X-Agent-Collab-Token", "").strip()
        if presented and hmac.compare_digest(presented, expected):
            return True
        self._json_response({"error": "missing or invalid auth token"}, status=401)
        return False

    def _handle_send(self) -> None:
        body = self._read_json()
        if body is None:
            return
        to = body.get("to")
        from_actor = body.get("from")
        text = body.get("body")
        kind = body.get("kind", "signal")
        channel = body.get("channel")

        if not isinstance(to, str) or not valid_role(to):
            self._json_response({"error": "to must be a valid role name"}, status=400)
            return
        if not isinstance(from_actor, str) or not valid_role(from_actor):
            self._json_response({"error": "from must be a valid role name"}, status=400)
            return
        if not isinstance(text, str) or not text.strip():
            self._json_response({"error": "body is required"}, status=400)
            return
        if has_control_chars(text):
            self._json_response(
                {"error": "body must not contain control characters"},
                status=400,
            )
            return
        try:
            text_bytes = text.encode("utf-8")
        except UnicodeError:
            self._json_response({"error": "body must be valid UTF-8"}, status=400)
            return
        if len(text_bytes) > self._server.max_body_bytes:
            self._json_response(
                {"error": f"body exceeds {self._server.max_body_bytes} bytes"},
                status=413,
            )
            return
        if not isinstance(kind, str) or kind not in VALID_KINDS:
            self._json_response({"error": "kind must be one of: chat, signal"}, status=400)
            return
        if channel is not None and (not isinstance(channel, str) or not valid_channel(channel)):
            self._json_response({"error": "channel must match [a-z0-9:_-]{1,96}"}, status=400)
            return
        if kind == "chat" and channel is None:
            self._json_response({"error": "chat messages require a channel"}, status=400)
            return

        policy = None
        if "response" in body:
            try:
                policy = response_policy(body["response"])
                if kind != "chat" or not channel or to == from_actor:
                    raise ValueError("response tracking requires chat, a channel, and distinct roles")
            except ValueError as exc:
                self._json_response({"error": str(exc)}, status=400)
                return
        generation = body.get("generation")
        if generation is not None and not isinstance(generation, str):
            self._json_response({"error": "generation must be a string"}, status=400)
            return
        try:
            message = self._store.send(
                to=to,
                from_actor=from_actor,
                body=text,
                kind=kind,
                channel=channel,
                expected_generation=generation,
                response=policy,
            )
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=409)
            return
        self._json_response(message, status=201)

    def _handle_response(self) -> None:
        body = self._read_json()
        if body is None:
            return
        required = {"generation", "message_id", "actor", "state"}
        if not required <= set(body) or set(body) - (required | {"detail"}):
            self._json_response({"error": "invalid response fields"}, status=400)
            return
        try:
            response = self._store.respond(**body)
        except TypeError as exc:
            self._json_response({"error": str(exc)}, status=400)
            return
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=409)
            return
        self._json_response(response)

    def _handle_recv(self, parsed: Any) -> None:
        filters = self._parse_filters(parsed, require_role=True)
        if filters is None:
            return
        filters.pop("to_role")
        wait = min(filters.pop("wait"), MAX_WAIT_SECONDS)
        expected_generation = parse_qs(parsed.query).get("generation", [None])[0]
        self._json_response(self._store.receive_snapshot(
            timeout=wait, expected_generation=expected_generation, **filters,
        ))

    def _handle_history(self, parsed: Any) -> None:
        filters = self._parse_filters(parsed, require_role=False)
        if filters is None:
            return
        filters.pop("wait")
        if not any(
            filters.get(key) is not None
            for key in ("role", "to_role", "kind", "from_actor", "channel")
        ):
            self._json_response(
                {"error": "history requires at least one filter (role, to, from, kind, or channel)"},
                status=400,
            )
            return
        messages = self._store.history(**filters)
        self._json_response({"messages": messages, "count": len(messages)})

    def _parse_filters(
        self,
        parsed: Any,
        *,
        require_role: bool,
    ) -> dict[str, Any] | None:
        params = parse_qs(parsed.query)
        role = params.get("role", [None])[0]
        to_role = params.get("to", [None])[0]
        since = parse_iso_timestamp(params.get("since", [None])[0])
        after_id = parse_positive_int(params.get("after_id", [None])[0])
        wait = parse_positive_int(params.get("wait", ["0"])[0])
        kind = params.get("kind", [None])[0]
        from_actor = params.get("from", [None])[0]
        channel = params.get("channel", [None])[0]
        limit = parse_positive_int(params.get("limit", [None])[0])

        if require_role and (not isinstance(role, str) or not valid_role(role)):
            self._json_response({"error": "role parameter required"}, status=400)
            return None
        if role is not None and role != "" and not valid_role(role):
            self._json_response({"error": "role must be a valid role name"}, status=400)
            return None
        if to_role is not None and to_role != "" and not valid_role(to_role):
            self._json_response({"error": "to must be a valid role name"}, status=400)
            return None
        if params.get("since", [None])[0] and since is None:
            self._json_response({"error": "since must be a valid ISO timestamp"}, status=400)
            return None
        if params.get("after_id", [None])[0] and after_id is None:
            self._json_response({"error": "after_id must be a non-negative integer"}, status=400)
            return None
        if params.get("wait", [None])[0] and wait is None:
            self._json_response({"error": "wait must be a non-negative integer"}, status=400)
            return None
        if kind is not None and kind != "" and kind not in VALID_KINDS:
            self._json_response({"error": "kind must be one of: chat, signal"}, status=400)
            return None
        if from_actor is not None and from_actor != "" and not valid_role(from_actor):
            self._json_response({"error": "from must be a valid role name"}, status=400)
            return None
        if channel is not None and channel != "" and not valid_channel(channel):
            self._json_response({"error": "channel must match [a-z0-9:_-]{1,96}"}, status=400)
            return None
        if params.get("limit", [None])[0] and limit is None:
            self._json_response({"error": "limit must be a non-negative integer"}, status=400)
            return None

        return {
            "role": role or None,
            "to_role": to_role or None,
            "since": since,
            "after_id": after_id,
            "kind": kind or None,
            "from_actor": from_actor or None,
            "channel": channel or None,
            "limit": min(limit, MAX_HISTORY_LIMIT) if limit is not None else None,
            "wait": float(wait or 0),
        }

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response({"error": "empty body"}, status=400)
            return None
        if length > MAX_REQUEST_BYTES:
            self._json_response({"error": f"request exceeds {MAX_REQUEST_BYTES} bytes"}, status=413)
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            self._json_response({"error": f"invalid JSON: {exc}"}, status=400)
            return None
        if not isinstance(payload, dict):
            self._json_response({"error": "request body must be a JSON object"}, status=400)
            return None
        return payload

    def _json_response(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
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
    def _server(self) -> BrokerHTTPServer:
        return cast(BrokerHTTPServer, self.server)

    @property
    def _store(self) -> MessageStore:
        return self._server.store


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    store: MessageStore | None = None,
    auth_token: str | None = None,
    require_auth: bool = True,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> BrokerHTTPServer:
    return BrokerHTTPServer(
        (host, port),
        BrokerHandler,
        store=store or MessageStore(),
        auth_token=auth_token,
        require_auth=require_auth,
        max_body_bytes=max_body_bytes,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    parser.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="Disable token authentication. Not recommended.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    token_file = Path(args.token_file)
    auth_token = None
    if not args.insecure_no_auth:
        auth_token = load_or_create_token(
            explicit_token=args.token,
            token_file=token_file,
        )

    server = make_server(
        host=args.host,
        port=args.port,
        auth_token=auth_token,
        require_auth=not args.insecure_no_auth,
    )

    print(f"collab broker running on http://{args.host}:{args.port}", flush=True)
    print("  POST /send", flush=True)
    print("  GET  /recv?role=architect[&kind=chat][&channel=dm:architect:builder][&after_id=12][&wait=30]", flush=True)
    print("  GET  /history?channel=dm:architect:builder[&kind=chat][&limit=50]", flush=True)
    print("  GET  /status", flush=True)
    if args.insecure_no_auth:
        print("  auth: DISABLED (--insecure-no-auth)", flush=True)
    else:
        print(f"  auth: enabled via X-Agent-Collab-Token (token file: {token_file})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbroker stopped")
        server.server_close()


if __name__ == "__main__":
    main()
