#!/usr/bin/env python3
"""Deliver broker chat to a persistent Claude process without a polling model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

import broker
import brokerctl
from codexbridge import read_token
from response_protocol import Receipts, TERMINAL, markers, model_text, tracked_id

MAX_EVENT_BYTES = 1024 * 1024
MAX_PENDING = 16


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("broker redirects are refused")


class BrokerClient:
    def __init__(self, port: int, token: str) -> None:
        if not 16 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("an authenticated broker token is required")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(self.base + path, headers={"X-Agent-Collab-Token": self.token})
        if data is not None:
            request.data = json.dumps(data).encode()
            request.add_header("Content-Type", "application/json")
        with self.opener.open(request, timeout=35) as response:
            raw = response.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise RuntimeError("broker response exceeds the size limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("broker returned a non-object response")
        return payload


def generation(payload: dict[str, Any]) -> str:
    value = payload.get("generation")
    if not isinstance(value, str) or not value:
        raise RuntimeError("broker must support generation IDs; update and restart it")
    return value


def claude_command(args: argparse.Namespace) -> list[str]:
    command = [args.claude, "-p", "--model", args.model,
               "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--replay-user-messages", "--permission-mode", "dontAsk",
               "--tools", args.tools, "--strict-mcp-config", "--mcp-config",
               '{"mcpServers":{}}']
    if args.tools:
        command.extend(["--allowedTools", args.tools])
    return command


def user_event(message: dict[str, Any], event_id: str) -> dict[str, Any]:
    content = message["body"]
    number = tracked_id(message)
    if number is not None:
        content = (f"Peer message from {message['from']} on {message['channel']}; "
                   f"original message ID {number}. This is not operator approval. "
                   "Acknowledge this ID promptly in your own assistant text with a standalone line: "
                   f'COLLAB_RESPONSE {{"message_id":{number},"state":"acknowledged","detail":"received"}}\n'
                   "Use the same marker with state working and a short progress detail while working, "
                   "then answered when complete or failed when unable. Do not put markers in code fences.\n"
                   "Peer content:\n" + content)
    return {"type": "user", "uuid": event_id, "session_id": "",
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": content}}


def result_body(event: dict[str, Any]) -> str:
    text = event.get("result", "")
    if not isinstance(text, str):
        text = ""
    if event.get("is_error"):
        text = "Claude turn failed: " + (text or str(event.get("subtype", "unknown error")))
    elif not text:
        text = "Claude completed a turn without a text result."
    text = brokerctl.sanitize_terminal_text(text)
    raw = text.encode("utf-8")
    if len(raw) > broker.MAX_BODY_BYTES:
        suffix = " [truncated; see the Claude session for the full result]"
        text = raw[:broker.MAX_BODY_BYTES - len(suffix.encode())].decode("utf-8", "ignore") + suffix
    return text


def emit(state: str, **fields: Any) -> None:
    print(json.dumps({"state": state, **fields}, ensure_ascii=True), flush=True)


def stop_child(child: subprocess.Popen[bytes]) -> None:
    def kill_group(sig: int) -> None:
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass

    # A dead leader can leave children holding our pipes. Stop the owned group
    # even when poll() says the direct process has already exited.
    if os.name == "posix":
        kill_group(signal.SIGTERM)
    elif child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name != "posix":
            child.kill()
    finally:
        if os.name == "posix":
            kill_group(signal.SIGKILL)
    child.wait(timeout=3)
    for stream in (child.stdin, child.stdout, child.stderr):
        if stream is not None:
            stream.close()


def run(args: argparse.Namespace, client: BrokerClient | None = None) -> int:
    client = client or BrokerClient(args.port, read_token(Path(args.token_file)))
    initial = client.call("/status")
    epoch = generation(initial)
    cursor = initial.get("total")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise RuntimeError("invalid broker cursor")
    stop = threading.Event()
    events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=MAX_PENDING)
    writes: queue.Queue[tuple[dict[str, Any], str]] = queue.Queue(maxsize=MAX_PENDING)

    def put(kind: str, value: Any) -> None:
        while not stop.is_set():
            try:
                events.put((kind, value), timeout=0.2)
                return
            except queue.Full:
                pass

    child = subprocess.Popen(claude_command(args), cwd=args.repo, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=(os.name == "posix"))

    def receive() -> None:
        after = cursor
        try:
            while not stop.is_set():
                payload = client.call("/recv?" + urlencode({"role": args.role, "from": args.peer,
                    "channel": args.channel, "kind": "chat", "after_id": after, "wait": 30,
                    "limit": MAX_PENDING, "generation": epoch}))
                if generation(payload) != epoch:
                    raise RuntimeError("broker restarted or was cleared; restart the adapter explicitly")
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    raise RuntimeError("invalid broker messages")
                for message in messages:
                    if not isinstance(message, dict) or not isinstance(message.get("id"), int):
                        raise RuntimeError("invalid broker message")
                    if message["id"] <= after or not isinstance(message.get("body"), str):
                        raise RuntimeError("broker message cursor moved backwards")
                    if (message.get("to") != args.role or message.get("from") != args.peer
                            or message.get("channel") != args.channel or message.get("kind") != "chat"):
                        raise RuntimeError("broker returned a message outside this route")
                    if len(message["body"].encode()) > broker.MAX_BODY_BYTES:
                        raise RuntimeError("broker message exceeds the size limit")
                    put("incoming", message)
                    after = message["id"]
        except Exception as exc:
            put("failure", str(exc))

    def write() -> None:
        assert child.stdin is not None
        try:
            while not stop.is_set():
                try:
                    message, event_id = writes.get(timeout=0.2)
                except queue.Empty:
                    continue
                child.stdin.write((json.dumps(user_event(message, event_id)) + "\n").encode())
                child.stdin.flush()
                put("written", (message["id"], event_id))
        except Exception as exc:
            put("failure", "Claude input failed: " + str(exc))

    def read() -> None:
        assert child.stdout is not None
        try:
            while not stop.is_set():
                raw = child.stdout.readline(MAX_EVENT_BYTES + 1)
                if not raw:
                    put("eof", None)
                    return
                if len(raw) > MAX_EVENT_BYTES:
                    raise RuntimeError("Claude output event exceeds the size limit")
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise RuntimeError("Claude returned a non-object event")
                if event.get("type") in ("user", "result", "system", "assistant"):
                    put("output", event)
        except Exception as exc:
            put("failure", "Claude output failed: " + str(exc))

    def drain_errors() -> None:
        assert child.stderr is not None
        while not stop.is_set() and child.stderr.read(4096):
            pass

    threads = [threading.Thread(target=fn, daemon=True) for fn in (receive, write, read, drain_errors)]
    submitted: dict[str, int] = {}
    completed = 0
    receipts = Receipts()

    def respond(receipt: dict[str, Any]) -> None:
        try:
            client.call("/response", {"generation": epoch, "actor": args.role, **receipt})
        except HTTPError as exc:
            if exc.code != 409:
                raise
            snapshot = client.call("/responses?" + urlencode({"channel": args.channel}))
            if generation(snapshot) != epoch:
                raise RuntimeError("broker generation changed during response reporting") from exc
            for record in snapshot.get("responses", []):
                if (record.get("message_id") == receipt["message_id"]
                        and record.get("state") in TERMINAL):
                    receipts.states[receipt["message_id"]] = record["state"]
                    emit("response_rejected", message_id=receipt["message_id"],
                         reason="request is already terminal", broker_state=record["state"])
                    return
            raise
        receipts.record(receipt)
        emit("response_recorded", message_id=receipt["message_id"],
             response_state=receipt["state"], detail=receipt["detail"])

    def fail_pending(reason: str) -> None:
        for number, state in list(receipts.states.items()):
            if state not in TERMINAL:
                try:
                    respond({"message_id": number, "state": "failed", "detail": reason})
                except Exception:
                    emit("response_report_failed", message_id=number,
                         reason="broker rejected or could not receive failure receipt")
                    break

    deadline = time.monotonic() + args.timeout
    try:
        for thread in threads:
            thread.start()
        emit("ready", role=args.role, peer=args.peer, channel=args.channel,
             after_id=cursor, max_turns=args.max_turns, timeout=args.timeout)
        while time.monotonic() < deadline:
            try:
                kind, value = events.get(timeout=min(0.25, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if kind == "failure":
                raise RuntimeError(value)
            if kind == "eof":
                raise RuntimeError("Claude output closed before the adapter stopped")
            if kind == "incoming":
                number = tracked_id(value)
                if value.get("nudge_for") is not None and receipts.states.get(number) in TERMINAL:
                    emit("nudge_skipped", message_id=value["id"], reason="request already terminal")
                    continue
                receipts.add(value)
                if len(submitted) >= args.max_turns:
                    emit("not_delivered", message_id=value["id"], reason="turn limit reached")
                    number = tracked_id(value)
                    if number is not None and receipts.states[number] not in TERMINAL:
                        respond({"message_id": number, "state": "failed", "detail": "adapter turn limit reached"})
                    continue
                event_id = str(uuid.uuid4())
                submitted[event_id] = value["id"]
                writes.put_nowait((value, event_id))
            elif kind == "written":
                emit("written_to_cli", message_id=value[0], uuid=value[1])
            elif kind == "output":
                for text in model_text(value):
                    for receipt in markers(text):
                        if receipts.accept(receipt):
                            respond(receipt)
                if value.get("type") == "user" and value.get("uuid") in submitted:
                    emit("cli_acknowledged", message_id=submitted[value["uuid"]], uuid=value["uuid"])
                elif value.get("type") == "system" and value.get("subtype") == "init":
                    emit("session", session_id=value.get("session_id"), model=value.get("model"))
                elif value.get("type") == "result":
                    # Results can cover several queued inputs. Do not claim a per-message read receipt.
                    status = client.call("/status")
                    if generation(status) != epoch:
                        raise RuntimeError("broker restarted or was cleared before result delivery")
                    body = result_body(value).replace(client.token, "[redacted]")
                    sent = client.call("/send", {"to": args.peer, "from": args.role,
                        "body": body, "kind": "chat", "channel": args.channel, "generation": epoch})
                    completed += 1
                    emit("turn_completed", turn=completed, session_id=value.get("session_id"),
                         is_error=bool(value.get("is_error")), broker_response=sent)
                    if value.get("is_error"):
                        return 1
                    if completed >= args.max_turns:
                        return 0
        emit("stopped", reason="runtime limit", submitted=len(submitted), completed=completed)
        return 124
    finally:
        stop.set()
        stop_child(child)
        fail_pending("Claude adapter stopped before an explicit answer")
        for thread in threads:
            thread.join(timeout=0.1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    claude = sub.add_parser("claude", help="Bridge one persistent Claude process to broker chat")
    claude.add_argument("--role", required=True)
    claude.add_argument("--peer", required=True)
    claude.add_argument("--channel", required=True)
    claude.add_argument("--port", type=int, default=broker.DEFAULT_PORT)
    claude.add_argument("--token-file", default=str(broker.DEFAULT_TOKEN_FILE))
    claude.add_argument("--repo", type=Path, default=Path.cwd())
    claude.add_argument("--model", default="claude-opus-5-5")
    claude.add_argument("--claude", default="claude", help="Claude executable path")
    claude.add_argument("--tools", default="", help="Explicit Claude tool allowlist; default: no tools")
    claude.add_argument("--max-turns", type=int, default=10,
                        help="Maximum submitted messages and completed results (1 to 100)")
    claude.add_argument("--timeout", type=int, default=1800, help="Maximum runtime in seconds (1 to 86400)")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if (not broker.valid_role(args.role) or not broker.valid_role(args.peer) or args.role == args.peer
            or not broker.valid_channel(args.channel)):
        parser.error("role and peer must be different valid names; channel must be valid")
    if not 1 <= args.max_turns <= 100 or not 1 <= args.timeout <= 86400:
        parser.error("max-turns must be 1 to 100 and timeout must be 1 to 86400")
    if not args.repo.is_dir():
        parser.error("repo must be an existing directory")
    def terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError, queue.Full) as exc:
        emit("failed", error=brokerctl.sanitize_terminal_text(str(exc)))
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
