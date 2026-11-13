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
import subprocess
import sys
import threading
import time
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9111
DEFAULT_TOKEN_FILE = Path(f"/tmp/agent-collab-broker-token-{os.getuid()}")
VALID_KINDS = ("signal", "chat")
BROKER_MODULE_PATH = Path(__file__).resolve().with_name("broker.py")
ENFORCEMENT_MODES = ("off", "warn", "hard")
SESSION_BOOT_TIMEOUT_SECONDS = 5.0


class DaemonLaunchResult(NamedTuple):
    action: str
    log_path: Path
    pid: int | None = None


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

    start_session = subparsers.add_parser(
        "start-session",
        help="Bootstrap a repo session: warm sync state and ensure the collab daemon is live.",
    )
    start_session.add_argument("--role", required=True)
    start_session.add_argument("--repo", default=".")
    start_session.add_argument("--actor")
    start_session.add_argument("--peer")
    start_session.add_argument(
        "--enforce",
        default="warn",
        choices=ENFORCEMENT_MODES,
        help="Enforcement mode to use only when runtime identity needs to be created.",
    )
    start_session.add_argument(
        "--note",
        default="session start",
        help="Sync note to record for the session bootstrap checkpoint.",
    )
    start_session.add_argument(
        "--daemon-interval",
        type=int,
        default=15,
        help="Polling interval for the repo sync daemon when one must be launched.",
    )
    start_session.add_argument(
        "--daemon-log",
        help="Optional path for the detached daemon log. Defaults to <repo>/.collab/<role>-daemon.log.",
    )
    start_session.add_argument(
        "--collab-script",
        help="Optional path to the repo-local collab.py. Defaults to <repo>/scripts/collab.py.",
    )
    start_session.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip daemon startup and only warm the repo state.",
    )

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

    send.add_argument("--require-response", action="store_true",
                      help="track acknowledgement and progress deadlines for this chat")
    send.add_argument("--ack-timeout", type=int)
    send.add_argument("--progress-timeout", type=int)
    send.add_argument("--max-nudges", type=int)

    responses = subparsers.add_parser("responses", help="Show tracked response states")
    responses.add_argument("--channel")
    respond = subparsers.add_parser("respond", help="Record an explicit response to a message")
    respond.add_argument("--message-id", type=int, required=True)
    respond.add_argument("--generation", required=True)
    respond.add_argument("--actor", required=True)
    respond.add_argument("--state", required=True,
                         choices=("acknowledged", "working", "answered", "failed"))
    respond.add_argument("--detail", default="")

    chat = subparsers.add_parser("chat", help="Open a live terminal chat with a peer.")
    chat.add_argument("--role", required=True)
    chat.add_argument("--peer", required=True)
    chat.add_argument("--history", type=int, default=20)

    subparsers.add_parser("clear", help="Clear all broker messages.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "send":
        configured = any(value is not None for value in
                         (args.ack_timeout, args.progress_timeout, args.max_nudges))
        if configured and not args.require_response:
            parser.error("response deadlines require --require-response")
        if args.require_response:
            if args.kind != "chat" or not args.channel or args.to == args.from_role:
                parser.error("--require-response needs chat, a channel, and distinct roles")
            for name in ("ack_timeout", "progress_timeout"):
                value = getattr(args, name)
                if value is not None and not 1 <= value <= 3600:
                    parser.error(name.replace("_", "-") + " must be 1 to 3600 seconds")
            if args.max_nudges is not None and not 0 <= args.max_nudges <= 5:
                parser.error("max-nudges must be 0 to 5")
    if args.command == "respond" and (args.message_id < 1 or not args.generation.strip()):
        parser.error("respond requires a positive message ID and a generation")

    if args.command == "up":
        return command_up(args)
    if args.command == "onboard":
        return command_onboard(args)

    token = resolve_token(args.token, args.token_file)
    base_url = f"http://{args.host}:{args.port}"

    try:
        if args.command == "status":
            payload = _get_json(f"{base_url}/status", token=token)
        elif args.command == "start-session":
            return command_start_session(args, base_url=base_url, token=token)
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
            request: dict[str, object] = {
                "to": args.to, "from": args.from_role, "body": args.body,
                "kind": args.kind, "channel": args.channel,
            }
            if args.require_response:
                support = _get_json(f"{base_url}/responses", token=token)
                generation = support.get("generation")
                if (not isinstance(generation, str) or not generation
                        or not isinstance(support.get("responses"), list)):
                    raise RuntimeError("broker does not support response monitoring; update and restart it")
                request["generation"] = generation
                request["response"] = {
                    "ack_timeout": args.ack_timeout if args.ack_timeout is not None else 30,
                    "progress_timeout": args.progress_timeout if args.progress_timeout is not None else 120,
                    "max_nudges": args.max_nudges if args.max_nudges is not None else 2,
                }
            payload = _post_json(f"{base_url}/send", request, token=token)
            if args.require_response and payload.get("response_required") is not True:
                raise RuntimeError("broker did not confirm response tracking; delivery is uncertain, do not retry automatically")
        elif args.command == "responses":
            query = urlencode({"channel": args.channel}) if args.channel else ""
            payload = _get_json(f"{base_url}/responses?{query}", token=token)
        elif args.command == "respond":
            payload = _post_json(f"{base_url}/response", {
                "message_id": args.message_id, "generation": args.generation,
                "actor": args.actor, "state": args.state, "detail": args.detail,
            }, token=token)
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
    print("Fast path:")
    print(f"  python3 brokerctl.py start-session --role {args.role} --repo /path/to/repo")
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


def command_start_session(
    args: argparse.Namespace,
    *,
    base_url: str,
    token: str | None,
) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        raise RuntimeError(f"repo does not exist: {repo}")
    if not repo.is_dir():
        raise RuntimeError(f"repo is not a directory: {repo}")

    collab_script = resolve_collab_script(repo, explicit_path=args.collab_script)
    existing_runtime = load_runtime_config(repo, args.role)
    actor = args.actor or str((existing_runtime or {}).get("actor") or args.role)
    peer = str((existing_runtime or {}).get("peer") or default_peer_for(args.role, args.peer))
    if args.peer:
        peer = args.peer
    broker_status = _get_json(f"{base_url}/status", token=token)

    init_result = run_collab_command(repo, collab_script, ["init"])
    set_active_runtime_role(repo, args.role)
    runtime_exists = existing_runtime is not None
    enable_result: subprocess.CompletedProcess[str] | None = None
    if not runtime_exists:
        enable_result = run_collab_command(
            repo,
            collab_script,
            [
                "enable",
                "--role",
                args.role,
                "--actor",
                actor,
                "--peer",
                peer,
                "--enforce",
                args.enforce,
            ],
        )

    sync_result = run_collab_command(
        repo,
        collab_script,
        ["sync", "--role", args.role, "--actor", actor, "--note", args.note],
    )
    next_result = run_collab_command(repo, collab_script, ["next", "--role", args.role])

    daemon_result: DaemonLaunchResult | None = None
    if not args.no_daemon:
        daemon_log_path = resolve_daemon_log_path(repo, role=args.role, explicit_path=args.daemon_log)
        daemon_result = ensure_daemon_running(
            repo,
            collab_script,
            role=args.role,
            interval=args.daemon_interval,
            log_path=daemon_log_path,
        )

    doctor_result: subprocess.CompletedProcess[str] | None = None
    if not args.no_daemon:
        doctor_result = run_collab_command(
            repo,
            collab_script,
            ["doctor", "--role", args.role],
            check=False,
        )

    print("Agent-collab session started")
    print(f"role: {args.role}")
    print(f"actor: {actor}")
    print(f"peer: {peer}")
    print(f"repo: {repo}")
    print(f"collab script: {collab_script}")
    pending_messages = broker_status.get("total", broker_status.get("message_count", "?"))
    print(
        f"broker: {base_url} [ok, pending_messages={pending_messages}]"
    )
    print_command_block("init", init_result.stdout)
    if enable_result is None:
        print("runtime: already enabled")
    else:
        print_command_block("runtime", enable_result.stdout)
    print_command_block("sync", sync_result.stdout)
    print_command_block("queue", next_result.stdout)
    if daemon_result is None:
        print("daemon: skipped (--no-daemon)")
    else:
        if daemon_result.action == "already running":
            print(f"daemon: already running [{daemon_result.log_path}]")
        else:
            print(
                f"daemon: started pid={daemon_result.pid} interval={args.daemon_interval}s [{daemon_result.log_path}]"
            )
    if doctor_result is not None:
        if doctor_result.returncode == 0:
            print_command_block("doctor", doctor_result.stdout)
        else:
            print_command_block("doctor (warnings)", command_output(doctor_result))
    else:
        print("doctor: skipped (daemon bootstrap disabled)")
    print("next:")
    print(f"  python3 brokerctl.py recv --role {args.role} --wait 60")
    print(f"  python3 brokerctl.py chat --role {args.role} --peer {peer}")
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


def resolve_collab_script(repo: Path, *, explicit_path: str | None = None) -> Path:
    candidate = Path(explicit_path).expanduser() if explicit_path else repo / "scripts" / "collab.py"
    if not candidate.is_absolute():
        candidate = (repo / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        raise RuntimeError(f"collab.py not found: {candidate}")
    return candidate


def runtime_config_path(repo: Path, role: str) -> Path:
    return repo / ".collab" / "runtime" / f"{role}.json"


def active_runtime_role_path(repo: Path) -> Path:
    return repo / ".collab" / "runtime" / "active-role.txt"


def load_runtime_config(repo: Path, role: str) -> dict[str, object] | None:
    path = runtime_config_path(repo, role)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid runtime config: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid runtime config payload: {path}")
    return payload


def set_active_runtime_role(repo: Path, role: str) -> None:
    path = active_runtime_role_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{role}\n", encoding="utf-8")


def resolve_daemon_log_path(repo: Path, *, role: str, explicit_path: str | None = None) -> Path:
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if not candidate.is_absolute():
            candidate = (repo / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return candidate
    return repo / ".collab" / f"{role}-daemon.log"


def print_command_block(title: str, output: str) -> None:
    print(f"{title}:")
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if not lines:
        print("  (no output)")
        return
    for line in lines:
        print(f"  {line}")


def command_output(result: subprocess.CompletedProcess[str]) -> str:
    parts = [part.strip() for part in (result.stdout, result.stderr) if part and part.strip()]
    return "\n".join(parts)


def run_collab_command(
    repo: Path,
    collab_script: Path,
    argv: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(collab_script), *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = command_output(result)
        if detail:
            raise RuntimeError(
                f"collab command failed in {repo}: {' '.join(argv)}\n{detail}"
            )
        raise RuntimeError(
            f"collab command failed in {repo}: {' '.join(argv)} (exit {result.returncode})"
        )
    return result


def ensure_daemon_running(
    repo: Path,
    collab_script: Path,
    *,
    role: str,
    interval: int,
    log_path: Path,
) -> DaemonLaunchResult:
    status_result = run_collab_command(
        repo,
        collab_script,
        ["daemon", "--role", role, "status"],
        check=False,
    )
    if status_result.returncode == 0:
        return DaemonLaunchResult(action="already running", log_path=log_path)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(collab_script),
                "daemon",
                "--role",
                role,
                "up",
                "--interval",
                str(interval),
            ],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + SESSION_BOOT_TIMEOUT_SECONDS
    last_status = command_output(status_result)
    while time.monotonic() < deadline:
        time.sleep(0.2)
        status_result = run_collab_command(
            repo,
            collab_script,
            ["daemon", "--role", role, "status"],
            check=False,
        )
        last_status = command_output(status_result)
        if status_result.returncode == 0:
            return DaemonLaunchResult(
                action="started",
                log_path=log_path,
                pid=process.pid,
            )
        if process.poll() is not None:
            break

    log_excerpt = read_log_excerpt(log_path)
    detail = last_status or "daemon status stayed unhealthy during bootstrap"
    if log_excerpt:
        detail = f"{detail}\nlog excerpt:\n{log_excerpt}"
    raise RuntimeError(f"daemon failed to become healthy for {role}\n{detail}")


def read_log_excerpt(path: Path, *, max_chars: int = 600) -> str:
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[-max_chars:]


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
