#!/usr/bin/env python3
"""Unified CLI for the local agent-collab broker.

Simple onboarding:
  python3 brokerctl.py up
  python3 brokerctl.py status
  python3 brokerctl.py chat --role builder --peer architect
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9111
DEFAULT_TOKEN_FILE = Path(f"/tmp/agent-collab-broker-token-{os.getuid()}")
VALID_KINDS = ("signal", "chat")
BROKER_MODULE_PATH = Path(__file__).resolve().with_name("broker.py")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))

    subparsers = parser.add_subparsers(dest="command", required=True)

    up = subparsers.add_parser("up", help="Start the secure broker in the foreground.")
    up.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="Disable token auth. Not recommended.",
    )

    onboard = subparsers.add_parser(
        "onboard",
        help="Print a copy/paste quickstart for one agent role.",
    )
    onboard.add_argument("--role", required=True)
    onboard.add_argument("--peer")

    subparsers.add_parser("status", help="Show broker queue status.")

    recv = subparsers.add_parser("recv", help="Receive messages for a role.")
    recv.add_argument("--role", required=True)
    recv.add_argument("--since")
    recv.add_argument("--after-id", type=int)
    recv.add_argument("--wait", type=int, default=0)
    recv.add_argument("--kind", choices=VALID_KINDS)
    recv.add_argument("--from-role")
    recv.add_argument("--channel")
    recv.add_argument("--limit", type=int)

    history = subparsers.add_parser("history", help="Show filtered broker history.")
    history.add_argument("--role")
    history.add_argument("--to-role")
    history.add_argument("--since")
    history.add_argument("--after-id", type=int)
    history.add_argument("--kind", choices=VALID_KINDS)
    history.add_argument("--from-role")
    history.add_argument("--channel")
    history.add_argument("--limit", type=int, default=50)

    send = subparsers.add_parser("send", help="Send a message to a role.")
    send.add_argument("--to", required=True)
    send.add_argument("--from-role", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--kind", choices=VALID_KINDS, default="signal")
    send.add_argument("--channel")

    chat = subparsers.add_parser("chat", help="Open a live terminal chat with a peer.")
    chat.add_argument("--role", required=True)
    chat.add_argument("--peer", required=True)
    chat.add_argument("--history", type=int, default=20)

    subparsers.add_parser("clear", help="Clear all broker messages.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "up":
        return command_up(args)
    if args.command == "onboard":
        return command_onboard(args)

    token = resolve_token(args.token, args.token_file)
    base_url = f"http://{args.host}:{args.port}"

    try:
        if args.command == "status":
            payload = _get_json(f"{base_url}/status", token=token)
        elif args.command == "recv":
            payload = _get_json(
                f"{base_url}/recv?{urlencode(recv_query_params(args))}",
                token=token,
            )
        elif args.command == "history":
            payload = _get_json(
                f"{base_url}/history?{urlencode(history_query_params(args))}",
                token=token,
            )
        elif args.command == "send":
            payload = _post_json(
                f"{base_url}/send",
                {
                    "to": args.to,
                    "from": args.from_role,
                    "body": args.body,
                    "kind": args.kind,
                    "channel": args.channel,
                },
                token=token,
            )
        elif args.command == "chat":
            return command_chat(args, base_url=base_url, token=token)
        else:
            payload = _post_json(f"{base_url}/clear", {}, token=token)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2))
    return 0


def recv_query_params(args: argparse.Namespace) -> dict[str, str | int]:
    params: dict[str, str | int] = {"role": args.role}
    if args.since:
        params["since"] = args.since
    if args.after_id is not None:
        params["after_id"] = args.after_id
    if args.wait > 0:
        params["wait"] = args.wait
    if args.kind:
        params["kind"] = args.kind
    if args.from_role:
        params["from"] = args.from_role
    if args.channel:
        params["channel"] = args.channel
    if args.limit is not None:
        params["limit"] = args.limit
    return params


def history_query_params(args: argparse.Namespace) -> dict[str, str | int]:
    params: dict[str, str | int] = {}
    if args.role:
        params["role"] = args.role
    if args.to_role:
        params["to"] = args.to_role
    if args.since:
        params["since"] = args.since
    if args.after_id is not None:
        params["after_id"] = args.after_id
    if args.kind:
        params["kind"] = args.kind
    if args.from_role:
        params["from"] = args.from_role
    if args.channel:
        params["channel"] = args.channel
    if args.limit is not None:
        params["limit"] = args.limit
    return params


def command_up(args: argparse.Namespace) -> int:
    broker = load_broker_module()
    token_file = Path(args.token_file)
    auth_token = None
    if not args.insecure_no_auth:
        auth_token = broker.load_or_create_token(
            explicit_token=args.token,
            token_file=token_file,
        )

    server = broker.make_server(
        host=args.host,
        port=args.port,
        auth_token=auth_token,
        require_auth=not args.insecure_no_auth,
    )

    print(f"collab broker running on http://{args.host}:{args.port}", flush=True)
    print("  next: python3 brokerctl.py status", flush=True)
    print("  next: python3 brokerctl.py recv --role builder --wait 60", flush=True)
    print(
        "  next: python3 brokerctl.py chat --role builder --peer architect",
        flush=True,
    )
    if args.insecure_no_auth:
        print("  auth: DISABLED (--insecure-no-auth)", flush=True)
    else:
        print(f"  auth: token file {token_file}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbroker stopped")
        server.server_close()
    return 0


def command_onboard(args: argparse.Namespace) -> int:
    peer = default_peer_for(args.role, args.peer)
    channel = chat_channel(args.role, peer)
    token_file = Path(args.token_file)
    token_state = "present" if token_file.exists() else "missing (run `python3 brokerctl.py up` first)"

    print("Agent-collab onboarding")
    print(f"role: {args.role}")
    print(f"peer: {peer}")
    print(f"broker: http://{args.host}:{args.port}")
    print(f"auth token file: {token_file} [{token_state}]")
    print(f"chat channel: {channel}")
    print("source of truth: .collab/tasks/*.json")
    print("")
    print("Start here:")
    print("  python3 brokerctl.py status")
    print(f"  python3 brokerctl.py recv --role {args.role} --wait 60")
    print(f"  python3 brokerctl.py chat --role {args.role} --peer {peer}")
    print("")
    print("Task state:")
    print(f"  python3 scripts/collab.py next --role {args.role}")
    print(
        f"  python3 scripts/collab.py sync --role {args.role} --actor {args.role} --note \"session start\""
    )
    print("")
    print("Rules:")
    print("  - use chat for short coordination only")
    print("  - record real handoffs/reviews in scripts/collab.py")
    print("  - keep broker messages single-line and actionable")
    return 0


def command_chat(
    args: argparse.Namespace,
    *,
    base_url: str,
    token: str | None,
) -> int:
    channel = chat_channel(args.role, args.peer)
    history_payload = _get_json(
        f"{base_url}/history?{urlencode({'channel': channel, 'kind': 'chat', 'limit': args.history})}",
        token=token,
    )

    print(
        f"chat ready: {args.role} <-> {args.peer} on {channel}\n"
        "type /quit to exit",
        flush=True,
    )

    print_lock = threading.Lock()
    last_seen_id = 0
    for message in history_payload.get("messages", []):
        if isinstance(message, dict):
            print_message(message, print_lock=print_lock)
            last_seen_id = max(last_seen_id, int(message.get("id", 0)))

    stop_event = threading.Event()

    def recv_loop() -> None:
        nonlocal last_seen_id
        while not stop_event.is_set():
            try:
                payload = _get_json(
                    f"{base_url}/recv?{urlencode({'role': args.role, 'kind': 'chat', 'channel': channel, 'from': args.peer, 'after_id': last_seen_id, 'wait': 30})}",
                    token=token,
                )
            except RuntimeError as exc:
                with print_lock:
                    print(f"[system] receive error: {exc}", file=sys.stderr, flush=True)
                stop_event.wait(1.0)
                continue

            for message in payload.get("messages", []):
                if not isinstance(message, dict):
                    continue
                last_seen_id = max(last_seen_id, int(message.get("id", 0)))
                print_message(message, print_lock=print_lock)

    receiver = threading.Thread(target=recv_loop, daemon=True)
    receiver.start()

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() in {"/quit", "/exit"}:
                break
            if not line.strip():
                continue
            message = _post_json(
                f"{base_url}/send",
                {
                    "to": args.peer,
                    "from": args.role,
                    "body": line,
                    "kind": "chat",
                    "channel": channel,
                },
                token=token,
            )
            last_seen_id = max(last_seen_id, int(message.get("id", 0)))
            print_message(message, print_lock=print_lock)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        receiver.join(timeout=1.0)
    return 0


def chat_channel(role: str, peer: str) -> str:
    left, right = sorted((role, peer))
    return f"dm:{left}:{right}"


def default_peer_for(role: str, explicit_peer: str | None) -> str:
    if explicit_peer:
        return explicit_peer
    if role == "architect":
        return "builder"
    if role == "builder":
        return "architect"
    raise RuntimeError("peer is required for non-standard roles")


def sanitize_terminal_text(value: object) -> str:
    raw = str(value)
    escaped: list[str] = []
    for char in raw:
        code = ord(char)
        if char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif code < 32 or code == 127:
            escaped.append(f"\\x{code:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def print_message(message: dict[str, object], *, print_lock: threading.Lock) -> None:
    timestamp = sanitize_terminal_text(message.get("at", "?"))
    sender = sanitize_terminal_text(message.get("from", "?"))
    body = sanitize_terminal_text(message.get("body", ""))
    kind = sanitize_terminal_text(message.get("kind", "signal"))
    with print_lock:
        prefix = f"[{timestamp}] {sender}"
        if kind != "chat":
            prefix += f" ({kind})"
        print(f"{prefix}: {body}", flush=True)


def load_broker_module():
    spec = importlib.util.spec_from_file_location("agent_collab_broker", BROKER_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load broker module from {BROKER_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_token(explicit: str | None, token_file: str | None) -> str | None:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    env_token = os.getenv("AGENT_COLLAB_TOKEN")
    if env_token:
        return env_token.strip()
    if token_file:
        path = Path(token_file)
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            return token or None
    return None


def _get_json(url: str, *, token: str | None) -> dict[str, object]:
    request = Request(url, headers=auth_headers(token), method="GET")
    return _read_json(request)


def _post_json(url: str, payload: dict[str, object], *, token: str | None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **auth_headers(token)},
        method="POST",
    )
    return _read_json(request)


def auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"X-Agent-Collab-Token": token}


def _read_json(request: Request) -> dict[str, object]:
    try:
        with urlopen(request, timeout=65) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"broker request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"broker request failed: {exc.reason}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"broker returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("broker returned a non-object JSON payload")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
