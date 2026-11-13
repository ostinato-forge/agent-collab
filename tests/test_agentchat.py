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
                            f"p = Path({str(child_pid)!r})\n"
                            "tmp = p.with_suffix('.tmp')\n"
                            "tmp.write_text(str(os.getpid()))\n"
                            "tmp.replace(p)\n"
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


class ResponseBroker(FakeBroker):
    def __init__(self, messages=()):
        super().__init__(messages)
        self.responses = []

    def call(self, path, data=None):
        if path == '/response':
            self.responses.append(data)
            return data
        return super().call(path, data)


def test_explicit_model_receipts_are_deduplicated_and_echo_is_not_ack(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, '''
msg = json.loads(sys.stdin.readline())
print(json.dumps(msg), flush=True)
text = 'COLLAB_RESPONSE {"message_id":6,"state":"acknowledged","detail":"received"}\\nCOLLAB_RESPONSE {"message_id":6,"state":"answered","detail":"done"}'
print(json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":text}]}}), flush=True)
print(json.dumps({"type":"result","result":text}), flush=True)
time.sleep(30)
''')
    client = ResponseBroker([dict(message(6), response_required=True)])
    assert agentchat.run(args(tmp_path, turns=1), client) == 0
    assert [item['state'] for item in client.responses] == ['acknowledged', 'answered']
    assert all(item['generation'] == 'one' and item['actor'] == 'builder' for item in client.responses)


def test_unrelated_completion_and_echo_leave_request_failed_at_stop(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, '''
msg = json.loads(sys.stdin.readline())
print(json.dumps(msg), flush=True)
print(json.dumps({"type":"result","result":"unrelated output"}), flush=True)
time.sleep(30)
''')
    client = ResponseBroker([dict(message(6), response_required=True)])
    assert agentchat.run(args(tmp_path, turns=1), client) == 0
    assert [item['state'] for item in client.responses] == ['failed']


def test_child_failure_marks_known_request_failed(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, 'sys.stdin.readline()\nsys.exit(7)\n')
    client = ResponseBroker([dict(message(6), response_required=True)])
    with pytest.raises(RuntimeError):
        agentchat.run(args(tmp_path, turns=1), client)
    assert [item['state'] for item in client.responses] == ['failed']


def test_late_receipt_does_not_stop_other_request(tmp_path, monkeypatch, capsys):
    from urllib.error import HTTPError
    fake_process(tmp_path, monkeypatch, '''
sys.stdin.readline()
sys.stdin.readline()
text = 'COLLAB_RESPONSE {"message_id":6,"state":"acknowledged"}\\nCOLLAB_RESPONSE {"message_id":7,"state":"answered","detail":"done"}'
print(json.dumps({"type":"result","result":text}), flush=True)
print(json.dumps({"type":"result","result":"second"}), flush=True)
time.sleep(30)
''')
    class Late(ResponseBroker):
        def call(self, path, data=None):
            if path == '/response' and data['message_id'] == 6:
                raise HTTPError('http://localhost/response', 409, 'terminal', {}, None)
            if path.startswith('/responses?'):
                return {'generation': 'one', 'responses': [{'message_id': 6, 'state': 'timed_out'}]}
            return super().call(path, data)
    client = Late([dict(message(6), response_required=True), dict(message(7), response_required=True)])
    assert agentchat.run(args(tmp_path, turns=2), client) == 0
    assert [item['state'] for item in client.responses] == ['answered']
    assert 'response_rejected' in capsys.readouterr().out


def test_answered_request_is_not_failed_when_other_request_loses_child(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, '''
sys.stdin.readline()
sys.stdin.readline()
text = 'COLLAB_RESPONSE {"message_id":6,"state":"answered","detail":"done"}'
print(json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":text}]}}), flush=True)
sys.exit(7)
''')
    client = ResponseBroker([dict(message(6), response_required=True), dict(message(7), response_required=True)])
    with pytest.raises(RuntimeError):
        agentchat.run(args(tmp_path), client)
    assert [(item['message_id'], item['state']) for item in client.responses] == [(6, 'answered'), (7, 'failed')]


def test_nudge_for_known_answered_request_does_not_start_extra_model_input(tmp_path, monkeypatch, capsys):
    fake_process(tmp_path, monkeypatch, '''
sys.stdin.readline()
text = 'COLLAB_RESPONSE {"message_id":6,"state":"answered","detail":"done"}'
print(json.dumps({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":text}]}}), flush=True)
sys.stdin.readline()
from pathlib import Path
Path('extra-input').touch()
time.sleep(30)
''')
    class Nudged(ResponseBroker):
        def call(self, path, data=None):
            result = super().call(path, data)
            if path == '/response' and data['state'] == 'answered':
                self.messages.append(dict(message(7), response_required=True, nudge_for=6))
            return result
    client = Nudged([dict(message(6), response_required=True)])
    assert agentchat.run(args(tmp_path, timeout=1), client) == 124
    assert not (tmp_path / 'extra-input').exists()
    assert 'nudge_skipped' in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_cleanup_reaps_exited_leader_after_group_permission_error(monkeypatch):
    from types import SimpleNamespace
    events = []
    reaped = False

    def poll():
        nonlocal reaped
        events.append('reaped')
        reaped = True
        return 7

    def killpg(pid, sig):
        events.append('signal')
        if not reaped:
            raise PermissionError('unreaped group')
        raise ProcessLookupError('group is gone')

    child = SimpleNamespace(pid=123, poll=poll, wait=lambda **kwargs: 7,
                            stdin=None, stdout=None, stderr=None)
    monkeypatch.setattr(agentchat.os, 'killpg', killpg)
    agentchat.stop_child(child)
    assert events[:3] == ['signal', 'reaped', 'signal']


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.parametrize('returncode', [None, 7])
def test_cleanup_keeps_real_group_permission_errors(monkeypatch, returncode):
    from types import SimpleNamespace

    def denied(*args):
        raise PermissionError('permission denied')

    child = SimpleNamespace(pid=123, poll=lambda: returncode)
    monkeypatch.setattr(agentchat.os, 'killpg', denied)
    with pytest.raises(PermissionError, match='permission denied'):
        agentchat.stop_child(child)


def test_cleanup_error_still_reports_pending_request(tmp_path, monkeypatch):
    fake_process(tmp_path, monkeypatch, '''
json.loads(sys.stdin.readline())
print(json.dumps({"type":"result","result":"unrelated output"}), flush=True)
time.sleep(30)
''')
    cleanup = agentchat.stop_child

    def cleanup_then_error(child):
        cleanup(child)
        raise PermissionError('cleanup failed')

    monkeypatch.setattr(agentchat, 'stop_child', cleanup_then_error)
    client = ResponseBroker([dict(message(6), response_required=True)])
    with pytest.raises(PermissionError, match='cleanup failed'):
        agentchat.run(args(tmp_path, turns=1), client)
    assert client.responses[-1]['message_id'] == 6
    assert client.responses[-1]['state'] == 'failed'
