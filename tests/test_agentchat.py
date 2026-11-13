from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agentchat


class FakeBroker:
    token = "x" * 32

    def __init__(self, messages=(), epoch="one"):
        self.messages = list(messages)
        self.epoch = epoch
        self.sent = []
        self.recv_queries = []
        self.ready = threading.Event()

    def call(self, path, data=None):
        if path == "/status":
            return {"generation": self.epoch, "total": 5}
        if path.startswith("/recv?"):
            self.recv_queries.append(path)
            if self.messages:
                messages, self.messages = self.messages, []
                return {"generation": self.epoch, "messages": messages}
            self.ready.wait(0.1)
            return {"generation": self.epoch, "messages": []}
        if path == "/send":
            self.sent.append(data)
            return {"ok": True, "message": {"id": 20 + len(self.sent)}}
        raise AssertionError(path)


def message(number, body="hello"):
    return {"id": number, "to": "builder", "from": "architect", "kind": "chat",
            "channel": "dm:architect:builder", "body": body}


def args(tmp_path, turns=2, timeout=5):
    return agentchat.build_parser().parse_args([
        "claude", "--role", "builder", "--peer", "architect", "--channel",
        "dm:architect:builder", "--repo", str(tmp_path), "--max-turns", str(turns),
        "--timeout", str(timeout)])


def fake_process(tmp_path, monkeypatch, body):
    script = tmp_path / "fake_claude.py"
    script.write_text("import sys, json, time\n" + body)
    monkeypatch.setattr(agentchat, "claude_command", lambda args: [sys.executable, str(script)])


def test_streams_second_input_before_first_result_and_keeps_one_child(tmp_path, monkeypatch, capsys):
    fake_process(tmp_path, monkeypatch, '''
messages = [json.loads(sys.stdin.readline()), json.loads(sys.stdin.readline())]
for msg in messages:
    print(json.dumps(msg), flush=True)
for msg in messages:
    print(json.dumps({"type": "result", "result": msg["message"]["content"],
                      "session_id": "same-session", "is_error": False}), flush=True)
time.sleep(30)
''')
    client = FakeBroker([message(6, "first"), message(7, "second")])
    assert agentchat.run(args(tmp_path), client) == 0
    assert [data["body"] for data in client.sent] == ["first", "second"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["message_id"] for e in events if e["state"] == "cli_acknowledged"] == [6, 7]
    assert [e["message_id"] for e in events if e["state"] == "written_to_cli"] == [6, 7]
    assert "after_id=5" in client.recv_queries[0]
    assert all(data["to"] == "architect" and data["kind"] == "chat" for data in client.sent)


def test_failed_model_result_is_published_without_retry(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, '''
sys.stdin.readline()
print(json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns"}), flush=True)
time.sleep(30)
''')
    client = FakeBroker([message(6)])
    assert agentchat.run(args(tmp_path, turns=1), client) == 1
    assert len(client.sent) == 1
    assert "failed" in client.sent[0]["body"]


def test_unexpected_child_exit_fails(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, "sys.exit(7)\n")
    with pytest.raises(RuntimeError, match="closed|input failed"):
        agentchat.run(args(tmp_path), FakeBroker())


def test_runtime_limit_stops_child(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, "time.sleep(30)\n")
    started = time.monotonic()
    assert agentchat.run(args(tmp_path, timeout=1), FakeBroker()) == 124
    assert time.monotonic() - started < 4


def test_changed_generation_stops_before_forwarding(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, "time.sleep(30)\n")
    class Restarted(FakeBroker):
        def call(self, path, data=None):
            result = super().call(path, data)
            if path.startswith("/recv?"):
                result["generation"] = "new"
            return result
    with pytest.raises(RuntimeError, match="restarted or was cleared"):
        agentchat.run(args(tmp_path), Restarted([message(6)]))


def test_rejects_unsupported_broker_before_starting_child(tmp_path, monkeypatch):
    class Old(FakeBroker):
        def call(self, path, data=None):
            return {"total": 0}
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not start"))
    with pytest.raises(RuntimeError, match="generation"):
        agentchat.run(args(tmp_path), Old())


def test_wrong_route_fails_closed(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, "time.sleep(30)\n")
    wrong = message(6)
    wrong["from"] = "stranger"
    with pytest.raises(RuntimeError, match="outside this route"):
        agentchat.run(args(tmp_path), FakeBroker([wrong]))


def test_missing_token_is_refused():
    with pytest.raises(ValueError, match="authenticated"):
        agentchat.BrokerClient(9111, "")


def test_command_has_no_permission_bypass_or_ambient_mcp(tmp_path):
    command = agentchat.claude_command(args(tmp_path))
    assert command[0] == "claude"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--replay-user-messages" in command


def test_result_is_bounded_and_terminal_safe():
    value = agentchat.result_body({"result": "\x1b[31m\n" + "é" * 9000})
    assert len(value.encode()) <= 4096
    assert "\x1b" not in value and "\n" not in value
    assert "truncated" in value


def test_generation_id_required():
    with pytest.raises(RuntimeError, match="generation"):
        agentchat.generation({})


def test_redirects_are_refused():
    with pytest.raises(RuntimeError, match="redirects"):
        agentchat.NoRedirect().redirect_request(None, None, 302, "", {}, "http://example.com")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_dead_leader_descendant_cannot_hold_output_pipe(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    child.wait(timeout=5)
    reader = threading.Thread(target=child.stdout.read, daemon=True)
    reader.start()
    started = time.monotonic()
    agentchat.stop_child(child)
    reader.join(timeout=1)
    assert not reader.is_alive()
    assert time.monotonic() - started < 4


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM")
def test_sigterm_runs_owned_child_cleanup(tmp_path):
    import os
    import signal
    child_pid = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text("import os,time\nfrom pathlib import Path\n"
                            f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
                            "time.sleep(30)\n")
    runner = tmp_path / "adapter.py"
    runner.write_text(f'''import sys, time
sys.path.insert(0, {str(ROOT)!r})
import agentchat
class Client:
    token = 'x' * 32
    def __init__(self, *args): pass
    def call(self, path, data=None):
        if path == '/status': return {{'generation':'one', 'total':0}}
        time.sleep(0.1)
        return {{'generation':'one', 'messages':[]}}
agentchat.BrokerClient = Client
agentchat.read_token = lambda path: 'x' * 32
agentchat.claude_command = lambda args: [sys.executable, {str(child_script)!r}]
sys.argv = ['agentchat.py', 'claude', '--role', 'builder', '--peer', 'architect', '--channel', 'test', '--repo', {str(tmp_path)!r}]
sys.exit(agentchat.main())
''')
    process = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid.exists()
        pid = int(child_pid.read_text())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        assert process.returncode == 130
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_outbound_result_is_generation_bound(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, '''
sys.stdin.readline()
print(json.dumps({"type":"result", "result":"done"}), flush=True)
time.sleep(30)
''')
    client = FakeBroker([message(6)])
    assert agentchat.run(args(tmp_path, turns=1), client) == 0
    assert client.sent[0]["generation"] == "one"


def test_oversized_output_fails_and_stops_child(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, "print('x' * 1048577, flush=True)\ntime.sleep(30)\n")
    with pytest.raises(RuntimeError, match="size limit"):
        agentchat.run(args(tmp_path), FakeBroker())


@pytest.mark.parametrize('token', ['x' * 20 + '\rsecret', 'x' * 4097, 'x' * 20 + 'é'])
def test_invalid_token_is_rejected_without_disclosure(token):
    with pytest.raises(ValueError) as exc:
        agentchat.BrokerClient(9111, token)
    assert token not in str(exc.value)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX FIFO')
def test_token_fifo_is_rejected_without_blocking(tmp_path):
    import os
    path = tmp_path / 'token'
    os.mkfifo(path, 0o600)
    with pytest.raises(RuntimeError, match='regular file'):
        agentchat.read_token(path)
