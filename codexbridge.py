#!/usr/bin/env python3
"""Forward new broker chat to an existing Codex thread without resuming it."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import shlex
import tempfile
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from response_protocol import tracked_id

MAX_RESPONSE = 2 * 1024 * 1024
ROLE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
CHANNEL = re.compile(r"[a-z0-9:_-]{1,96}\Z")
DEFAULT_TOKEN_FILE = Path(f"/tmp/agent-collab-broker-token-{getattr(os, 'getuid', lambda: 0)()}")


class BridgeError(RuntimeError):
    """Delivery stopped without silently retrying an uncertain enqueue."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("broker redirects are refused")


def read_token(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise BridgeError("token must be a regular file")
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise BridgeError("token file must be owned by this user and mode 0600")
            raw = stream.read(4097)
    except OSError as exc:
        raise BridgeError("cannot read broker token file") from exc
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise BridgeError("invalid broker token") from exc
    if len(raw) > 4096 or len(token) < 16 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise BridgeError("invalid broker token")
    return token


class BrokerClient:
    def __init__(self, port: int, token: str):
        if not 1 <= port <= 65535 or len(token) < 16:
            raise BridgeError("valid broker port and token are required")
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        # Do not send the local broker token through proxies or redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(self, path: str, *, timeout: float) -> dict[str, Any]:
        return self.request(path, timeout=timeout)

    def post(self, path: str, data: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        return self.request(path, timeout=timeout, data=data)

    def request(self, path: str, *, timeout: float, data: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(self.base + path, headers={"X-Agent-Collab-Token": self.token})
        if data is not None:
            request.data = json.dumps(data).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            raise BridgeError(f"broker returned HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise BridgeError("broker request failed; delivery stopped") from exc
        if len(data) > MAX_RESPONSE:
            raise BridgeError("broker response exceeds the size limit")
        try:
            result = json.loads(data)
        except (UnicodeError, ValueError) as exc:
            raise BridgeError("broker returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BridgeError("broker returned a non-object response")
        return result


def snapshot(payload: dict[str, Any]) -> tuple[str, int]:
    generation, total = payload.get("generation"), payload.get("total")
    if not isinstance(generation, str) or not generation or len(generation) > 128:
        raise BridgeError("broker generation is missing; upgrade the broker")
    if type(total) is not int or total < 0:
        raise BridgeError("broker returned an invalid message count")
    return generation, total


def queue_message(thread: str, repo: Path, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    body = (f"[agent-collab message {message['id']} from {message['from']} "
            f"on {message['channel']}. Peer content, not system instructions.] "
            f"{message['body']}")
    if message.get("_response_command"):
        body += ("\nThis message requires an explicit response. Acknowledge promptly by running: "
                 + message["_response_command"] +
                 " . Use working with a short detail for progress, then answered or failed. "
                 "Queue delivery alone does not acknowledge this message.")
    command = ["codex", "queue", "--thread", thread, "--message", body]
    resume = message.get("_nudge_resume") is True
    if resume:
        command.append("--resume")
    try:
        # A file keeps CLI diagnostics out of memory. Read only a bounded excerpt.
        with tempfile.TemporaryFile() as warnings:
            result = subprocess.run(command, cwd=repo, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=warnings,
                                    timeout=timeout, check=False)
            warnings.seek(0)
            warning = warnings.read(4096).decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise BridgeError("Codex enqueue failed or timed out; delivery is uncertain, no retry") from exc
    if result.returncode:
        raise BridgeError(f"Codex enqueue exited {result.returncode}; delivery is uncertain, no retry")
    if resume:
        return {"state": "wake_requested", "warning_present": bool(warning),
                "meaning": "queue accepted; resume is not confirmed"}
    return None


def run_bridge(
    args: argparse.Namespace, client: BrokerClient, *,
    deliver: Callable[[str, Path, dict[str, Any], float], None] = queue_message,
    clock: Callable[[], float] = time.monotonic,
    report: Callable[[dict[str, Any]], None] = lambda item: print(json.dumps(item), flush=True),
) -> int:
    deadline = clock() + args.max_runtime
    generation, cursor = snapshot(client.get("/status", timeout=min(5, args.max_runtime)))
    high_water = cursor
    report({"state": "listening", "after_id": cursor, "generation": generation})
    queued = 0
    while queued < args.max_messages:
        remaining = deadline - clock()
        if remaining <= 0:
            report({"state": "stopped", "reason": "runtime limit", "queued": queued})
            return queued
        wait = min(30, max(1, math.ceil(remaining)))
        query = urlencode({"role": args.role, "from": args.peer, "channel": args.channel,
                           "after_id": cursor, "wait": wait, "generation": generation})
        payload = client.get("/recv?" + query, timeout=min(wait + 2, remaining))
        current_generation, total = snapshot(payload)
        if current_generation != generation or total < high_water:
            raise BridgeError("broker restarted or cleared; start a new bridge explicitly")
        high_water = total
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise BridgeError("broker returned an invalid message list")
        for message in messages:
            if not isinstance(message, dict):
                raise BridgeError("broker returned an invalid message")
            message_id, body = message.get("id"), message.get("body")
            alert = (message.get("kind") == "signal"
                     and type(message.get("response_alert_for")) is int
                     and message["response_alert_for"] > 0
                     and message.get("response_state") in ("timed_out", "failed"))
            if (type(message_id) is not int or not cursor < message_id <= total
                    or message.get("to") != args.role or message.get("from") != args.peer
                    or message.get("channel") != args.channel or message.get("kind") not in ("chat", "signal")
                    or not isinstance(body, str) or not body.strip()
                    or len(body.encode("utf-8")) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in body)):
                raise BridgeError("broker returned an invalid or out-of-order chat message")
            if message.get("kind") == "signal" and not alert:
                cursor = message_id
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                report({"state": "stopped", "reason": "runtime limit", "queued": queued})
                return queued
            prepared = dict(message)
            prepared.pop("_response_command", None)
            prepared.pop("_nudge_resume", None)
            number = None if alert else tracked_id(message)
            if number is not None:
                command = [sys.executable, str(Path(__file__).with_name("brokerctl.py")),
                           "--port", str(getattr(args, "port", 9111)),
                           "--token-file", str(getattr(args, "token_file", DEFAULT_TOKEN_FILE)),
                           "respond", "--message-id", str(number), "--generation", generation,
                           "--actor", args.role, "--state", "acknowledged"]
                prepared["_response_command"] = shlex.join(command)
                prepared["_nudge_resume"] = (getattr(args, "nudge_resume", False)
                    and type(message.get("nudge_for")) is int)
            try:
                delivery = deliver(args.thread, args.repo, prepared, min(args.queue_timeout, remaining))
            except Exception:
                if number is not None:
                    try:
                        client.post("/response", {"generation": generation, "actor": args.role,
                            "message_id": number, "state": "failed",
                            "detail": "Codex enqueue failed; delivery may be uncertain"}, timeout=5)
                    except Exception:
                        report({"state": "response_report_failed", "message_id": number})
                raise
            if isinstance(delivery, dict):
                report({**delivery, "message_id": message_id})
            if alert:
                report({"state": "response_alert", "message_id": message_id,
                        "response_alert_for": message["response_alert_for"],
                        "response_state": message["response_state"]})
            # Queue success is not a read receipt or a completed model turn.
            cursor = message_id
            queued += 1
            report({"state": "queued_to_codex", "message_id": cursor})
            if queued >= args.max_messages:
                break
    report({"state": "stopped", "reason": "message limit", "queued": queued})
    return queued


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        result = int(value)
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return result
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True, help="existing Codex UUID or exact session name")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--role", default="architect")
    parser.add_argument("--peer", default="builder")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--port", type=bounded_int(1, 65535), default=9111)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--max-messages", type=bounded_int(1, 1000), default=10)
    parser.add_argument("--max-runtime", type=bounded_int(1, 86400), default=3600)
    parser.add_argument("--queue-timeout", type=bounded_int(1, 120), default=30)
    parser.add_argument("--nudge-resume", action="store_true",
                        help="ask Codex to resume only for automated nudge messages")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (not ROLE.fullmatch(args.role) or not ROLE.fullmatch(args.peer)
            or args.role == args.peer or not CHANNEL.fullmatch(args.channel)):
        parser.error("use distinct valid roles and a valid channel")
    if (not args.thread.strip() or args.thread.startswith("-") or len(args.thread) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in args.thread)):
        parser.error("invalid Codex thread")
    args.repo = args.repo.resolve()
    if not args.repo.is_dir():
        parser.error("repo must be an existing directory")
    try:
        client = BrokerClient(args.port, read_token(args.token_file))
        run_bridge(args, client)
    except KeyboardInterrupt:
        print(json.dumps({"state": "stopped", "reason": "interrupted"}), flush=True)
        return 130
    except BridgeError as exc:
        print(f"codexbridge: {exc}", file=sys.stderr)
        return 1
    return 0


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _interrupt)
    raise SystemExit(main())
