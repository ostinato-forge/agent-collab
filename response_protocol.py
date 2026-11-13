"""Explicit model response markers. Transport output is not acknowledgement."""
from __future__ import annotations

import json
from typing import Any

PREFIX = "COLLAB_RESPONSE "
STATES = frozenset({"acknowledged", "working", "answered", "failed"})
TERMINAL = frozenset({"answered", "failed", "timed_out"})


def tracked_id(message: dict[str, Any]) -> int | None:
    if message.get("response_required") is not True:
        return None
    value = message.get("nudge_for", message.get("id"))
    if type(value) is not int or value < 1:
        raise ValueError("invalid tracked message ID")
    return value


def model_text(event: dict[str, Any]) -> list[str]:
    if event.get("type") == "result":
        result = event.get("result")
        return [result] if isinstance(result, str) and not event.get("is_error") else []
    if event.get("type") != "assistant" or event.get("parent_tool_use_id") is not None:
        return []
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    texts = [part["text"] for part in content if isinstance(part, dict)
             and part.get("type") == "text" and isinstance(part.get("text"), str)]
    return ["\n".join(texts)] if texts else []


def markers(text: str):
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not line.startswith(PREFIX):
            continue
        try:
            receipt = json.loads(line[len(PREFIX):])
        except (ValueError, RecursionError):
            continue
        if not isinstance(receipt, dict) or set(receipt) - {"message_id", "state", "detail"}:
            continue
        number, state, detail = receipt.get("message_id"), receipt.get("state"), receipt.get("detail", "")
        try:
            detail_bytes = len(detail.encode("utf-8")) if isinstance(detail, str) else 1025
        except UnicodeEncodeError:
            continue
        if (type(number) is not int or number < 1 or not isinstance(state, str) or state not in STATES
                or not isinstance(detail, str) or detail_bytes > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in detail)):
            continue
        yield {"message_id": number, "state": state, "detail": detail}


class Receipts:
    def __init__(self) -> None:
        self.states: dict[int, str] = {}
        self.seen: set[tuple[int, str, str]] = set()

    def add(self, message: dict[str, Any]) -> int | None:
        number = tracked_id(message)
        if number is not None:
            self.states.setdefault(number, "queued")
        return number

    def accept(self, receipt: dict[str, Any]) -> bool:
        number, state, detail = receipt["message_id"], receipt["state"], receipt["detail"]
        old = self.states.get(number)
        key = (number, state, detail)
        if old is None or old in TERMINAL or key in self.seen:
            return False
        if old == "working" and state == "acknowledged":
            return False
        return True

    def record(self, receipt: dict[str, Any]) -> None:
        self.states[receipt["message_id"]] = receipt["state"]
        self.seen.add((receipt["message_id"], receipt["state"], receipt["detail"]))
