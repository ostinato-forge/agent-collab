from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import threading


MODULE_PATH = Path(__file__).resolve().parents[1] / "brokerctl.py"
SPEC = importlib.util.spec_from_file_location("brokerctl", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
brokerctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(brokerctl)


def test_chat_channel_is_stable_and_sorted() -> None:
    assert brokerctl.chat_channel("builder", "architect") == "dm:architect:builder"
    assert brokerctl.chat_channel("architect", "builder") == "dm:architect:builder"


def test_resolve_token_prefers_explicit_then_env_then_file(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")

    monkeypatch.setenv("AGENT_COLLAB_TOKEN", "env-token")
    assert brokerctl.resolve_token("explicit-token", str(token_file)) == "explicit-token"
    assert brokerctl.resolve_token(None, str(token_file)) == "env-token"

    monkeypatch.delenv("AGENT_COLLAB_TOKEN")
    assert brokerctl.resolve_token(None, str(token_file)) == "file-token"


def test_auth_headers_omits_empty_token() -> None:
    assert brokerctl.auth_headers(None) == {}
    assert brokerctl.auth_headers("token-123") == {"X-Agent-Collab-Token": "token-123"}


def test_default_peer_for_standard_roles() -> None:
    assert brokerctl.default_peer_for("builder", None) == "architect"
    assert brokerctl.default_peer_for("architect", None) == "builder"
    assert brokerctl.default_peer_for("builder", "reviewer") == "reviewer"


def test_sanitize_terminal_text_escapes_control_characters() -> None:
    assert brokerctl.sanitize_terminal_text("line\nnext\t\x1b") == "line\\nnext\\t\\x1b"


def test_print_message_sanitizes_output(capsys) -> None:
    brokerctl.print_message(
        {
            "at": "2026-04-22T12:00:00+00:00",
            "from": "architect",
            "body": "hello\n\x1b[31mworld",
            "kind": "chat",
        },
        print_lock=threading.Lock(),
    )
    output = capsys.readouterr().out
    assert "\\n" in output
    assert "\\x1b" in output
