#!/usr/bin/env python3
"""Small CLI wrapper for the local agent-collab broker."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Talk to the local agent-collab broker.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9111)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show broker queue status.")

    recv = subparsers.add_parser("recv", help="Receive messages for a role.")
    recv.add_argument("--role", required=True)
    recv.add_argument("--since")
    recv.add_argument("--wait", type=int, default=0)

    send = subparsers.add_parser("send", help="Send a message to a role.")
    send.add_argument("--to", required=True)
    send.add_argument("--from-role", required=True)
    send.add_argument("--body", required=True)

    subparsers.add_parser("clear", help="Clear all broker messages.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    base_url = f"http://{args.host}:{args.port}"

    try:
        if args.command == "status":
            payload = _get_json(f"{base_url}/status")
        elif args.command == "recv":
            params: dict[str, str | int] = {"role": args.role}
            if args.since:
                params["since"] = args.since
            if args.wait > 0:
                params["wait"] = args.wait
            payload = _get_json(f"{base_url}/recv?{urlencode(params)}")
        elif args.command == "send":
            payload = _post_json(
                f"{base_url}/send",
                {
                    "to": args.to,
                    "from": args.from_role,
                    "body": args.body,
                },
            )
        else:
            payload = _post_json(f"{base_url}/clear", {})
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2))
    return 0


def _get_json(url: str) -> dict[str, object]:
    request = Request(url, method="GET")
    return _read_json(request)


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _read_json(request)


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
