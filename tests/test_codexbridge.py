from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

SPEC = importlib.util.spec_from_file_location("codexbridge", Path(__file__).parents[1] / "codexbridge.py")
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def args(tmp_path, **changes):
    values = dict(thread="test thread", repo=tmp_path, role="architect", peer="builder",
                  channel="test:chat", max_runtime=60, max_messages=1, queue_timeout=5)
    values.update(changes)
    return argparse.Namespace(**values)


def message(number=4, **changes):
    result = {"id": number, "body": "hello $(touch /tmp/never) `uname` ; --resume", "to": "architect",
              "from": "builder", "channel": "test:chat", "kind": "chat"}
    result.update(changes)
    return result


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def get(self, path, *, timeout):
        self.calls.append((path, timeout))
        return next(self.replies)


def test_skips_history_scopes_long_poll_and_reports_only_queue(tmp_path):
    client = FakeClient([{"generation": "g", "total": 3},
                         {"generation": "g", "total": 4, "messages": [message()]}])
    events, delivered = [], []
    count = bridge.run_bridge(args(tmp_path), client,
                              deliver=lambda *values: delivered.append(values), report=events.append)
    assert count == 1 and delivered[0][2] == message()
    query = parse_qs(urlsplit(client.calls[1][0]).query)
    assert query == {"role": ["architect"], "from": ["builder"], "channel": ["test:chat"],
                     "after_id": ["3"], "wait": ["30"], "generation": ["g"]}
    assert events[1] == {"state": "queued_to_codex", "message_id": 4}
    assert "read" not in json.dumps(events) and "answered" not in json.dumps(events)


def test_queue_uses_literal_argv_and_does_not_resume(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(bridge.subprocess, "run", run)
    bridge.queue_message("a thread", tmp_path, message(), 3)
    command, kwargs = calls[0]
    assert command[:5] == ["codex", "queue", "--thread", "a thread", "--message"]
    assert message()["body"] in command[5]
    assert len(command) == 6 and "--resume" not in command
    assert not kwargs.get("shell") and kwargs["timeout"] == 3 and kwargs["cwd"] == tmp_path


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired("codex", 1), OSError("missing")])
def test_uncertain_enqueue_fails_without_retry_or_receipt(tmp_path, monkeypatch, failure):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(command, failure)
    monkeypatch.setattr(bridge.subprocess, "run", run)
    client = FakeClient([{"generation": "g", "total": 3},
                         {"generation": "g", "total": 4, "messages": [message()]}])
    events = []
    with pytest.raises(bridge.BridgeError, match="uncertain"):
        bridge.run_bridge(args(tmp_path), client, deliver=bridge.queue_message, report=events.append)
    assert len(calls) == 1 and len(events) == 1


@pytest.mark.parametrize("snapshot", [{"generation": "new", "total": 20},
                                      {"generation": "g", "total": 2},
                                      {"total": 20}])
def test_generation_change_clear_or_old_broker_refused(tmp_path, snapshot):
    client = FakeClient([{"generation": "g", "total": 3}, {**snapshot, "messages": []}])
    with pytest.raises(bridge.BridgeError):
        bridge.run_bridge(args(tmp_path), client, report=lambda _: None)


@pytest.mark.parametrize("change", [{"id": 3}, {"id": True}, {"id": 5}, {"body": "x\ny"},
                                    {"body": "x" * 4097}, {"to": "other"}, {"from": "other"},
                                    {"channel": "other"}, {"kind": "unknown"}])
def test_malformed_or_wrong_scope_message_never_delivered(tmp_path, change):
    client = FakeClient([{"generation": "g", "total": 3},
                         {"generation": "g", "total": 4, "messages": [message(**change)]}])
    with pytest.raises(bridge.BridgeError):
        bridge.run_bridge(args(tmp_path), client,
                          deliver=lambda *unused: pytest.fail("must not deliver"), report=lambda _: None)


def test_cursor_advances_only_after_success(tmp_path):
    client = FakeClient([{"generation": "g", "total": 3},
                         {"generation": "g", "total": 4, "messages": [message()]},
                         {"generation": "g", "total": 5, "messages": [message(5)]}])
    bridge.run_bridge(args(tmp_path, max_messages=2), client,
                      deliver=lambda *unused: None, report=lambda _: None)
    assert parse_qs(urlsplit(client.calls[2][0]).query)["after_id"] == ["4"]


def test_runtime_limit_does_not_deliver_after_deadline(tmp_path):
    client = FakeClient([{"generation": "g", "total": 3}])
    clock = iter([10, 71])
    events = []
    assert bridge.run_bridge(args(tmp_path), client, clock=lambda: next(clock), report=events.append) == 0
    assert len(client.calls) == 1 and events[-1]["reason"] == "runtime limit"


def test_private_token_required_and_symlink_refused(tmp_path):
    path = tmp_path / "token"
    with pytest.raises(bridge.BridgeError):
        bridge.read_token(path)
    path.write_text("x" * 32)
    path.chmod(0o600)
    assert bridge.read_token(path) == "x" * 32
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(bridge.BridgeError):
            bridge.read_token(path)
        path.chmod(0o600)
        link = tmp_path / "link"
        link.symlink_to(path)
        with pytest.raises(bridge.BridgeError):
            bridge.read_token(link)
    path.write_text("short")
    with pytest.raises(bridge.BridgeError):
        bridge.read_token(path)


def test_redirect_cannot_forward_token():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()
        def log_message(self, *unused):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = bridge.BrokerClient(server.server_port, "x" * 32)
        with pytest.raises(bridge.BridgeError, match="redirect"):
            client.get("/status", timeout=1)
        assert seen == ["/status"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_actual_fake_codex_executable_receives_literal_message(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX executable fixture")
    executable = tmp_path / "codex"
    output = tmp_path / "argv.json"
    executable.write_text("#!/usr/bin/env python3\nimport json, pathlib, sys\n"
                          "pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    bridge.queue_message("literal thread", tmp_path, message(), 5)
    received = json.loads(output.read_text())
    assert received[:4] == ["queue", "--thread", "literal thread", "--message"]
    assert received[4].endswith(message()["body"])


@pytest.mark.parametrize("clear", [False, True])
def test_real_broker_long_poll_delivers_new_scoped_message_or_stops_on_clear(tmp_path, clear):
    spec = importlib.util.spec_from_file_location("broker_for_codex_test", Path(__file__).parents[1] / "broker.py")
    assert spec and spec.loader
    broker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker)
    store = broker.MessageStore()
    store.send(to="architect", from_actor="builder", body="old", kind="chat", channel="test:chat")
    server = broker.make_server(port=0, store=store, auth_token="x" * 32)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    listening = threading.Event()
    events, delivered, errors = [], [], []
    def report(event):
        events.append(event)
        if event["state"] == "listening":
            listening.set()
    def run():
        try:
            bridge.run_bridge(args(tmp_path, max_runtime=3),
                              bridge.BrokerClient(server.server_port, "x" * 32),
                              deliver=lambda *values: delivered.append(values), report=report)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert listening.wait(2)
        if clear:
            store.clear()
        else:
            store.send(to="architect", from_actor="unrelated", body="wrong peer", kind="chat", channel="test:chat")
            store.send(to="architect", from_actor="builder", body="new", kind="chat", channel="test:chat")
        worker.join(5)
        assert not worker.is_alive()
        if clear:
            assert not delivered and len(errors) == 1
            assert isinstance(errors[0], bridge.BridgeError) and "cleared" in str(errors[0])
        else:
            assert not errors and [item[2]["body"] for item in delivered] == ["new"]
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
        worker.join(5)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_fifo_token_is_rejected_without_waiting_for_writer(tmp_path):
    token = tmp_path / "token-fifo"
    os.mkfifo(token, 0o600)
    result = subprocess.run([
        os.sys.executable, str(Path(__file__).parents[1] / "codexbridge.py"),
        "--thread", "test", "--repo", str(tmp_path), "--channel", "test:chat",
        "--token-file", str(token),
    ], timeout=3, capture_output=True, text=True)
    assert result.returncode == 1 and "regular file" in result.stderr


def test_signal_progress_is_skipped_but_timeout_alert_and_chat_are_delivered(tmp_path):
    client = FakeClient([{'generation': 'g', 'total': 3},
        {'generation': 'g', 'total': 6, 'messages': [message(4, kind='signal'),
         message(5, kind='signal', response_alert_for=1, response_state='timed_out'), message(6)]}])
    delivered, events = [], []
    assert bridge.run_bridge(args(tmp_path, max_messages=2), client,
        deliver=lambda *values: delivered.append(values), report=events.append) == 2
    assert [values[2]['id'] for values in delivered] == [5, 6]
    assert any(event['state'] == 'response_alert' for event in events)


def test_nudge_resume_only_when_opted_in_and_only_for_nudges(tmp_path, monkeypatch):
    commands, reports = [], []
    def run(command, **kwargs):
        commands.append(command)
        kwargs['stderr'].write(b'warning: could not resume\n')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(bridge.subprocess, 'run', run)
    for enabled, nudge in [(False, True), (True, False), (True, True)]:
        tracked = dict(message(), response_required=True)
        if nudge:
            tracked['nudge_for'] = 1
        client = FakeClient([{'generation': 'g', 'total': 3},
                            {'generation': 'g', 'total': 4, 'messages': [tracked]}])
        bridge.run_bridge(args(tmp_path, nudge_resume=enabled), client,
                          deliver=bridge.queue_message, report=reports.append)
    assert ['--resume' in command for command in commands] == [False, False, True]
    wake = [event for event in reports if event['state'] == 'wake_requested']
    assert len(wake) == 1 and wake[0]['warning_present'] is True
    assert 'not confirmed' in wake[0]['meaning']
    assert 'respond --message-id 1 --generation g --actor architect --state acknowledged' in commands[-1][5]


def test_tracked_enqueue_failure_is_reported_without_ack(tmp_path):
    class Client(FakeClient):
        def __init__(self, replies):
            super().__init__(replies)
            self.receipts = []
        def post(self, path, data, *, timeout):
            self.receipts.append((path, data))
    client = Client([{'generation': 'g', 'total': 3},
        {'generation': 'g', 'total': 4, 'messages': [dict(message(), response_required=True)]}])
    def fail(*args):
        raise bridge.BridgeError('failed')
    with pytest.raises(bridge.BridgeError):
        bridge.run_bridge(args(tmp_path), client, deliver=fail, report=lambda event: None)
    assert len(client.receipts) == 1
    assert client.receipts[0][1]['state'] == 'failed'
    assert client.receipts[0][1]['message_id'] == 4


def test_successful_tracked_queue_does_not_claim_acknowledgement(tmp_path):
    client = FakeClient([{'generation': 'g', 'total': 3},
        {'generation': 'g', 'total': 4, 'messages': [dict(message(), response_required=True)]}])
    delivered = []
    bridge.run_bridge(args(tmp_path), client, deliver=lambda *values: delivered.append(values),
                      report=lambda event: None)
    assert '--state acknowledged' in delivered[0][2]['_response_command']
    assert not hasattr(client, 'post')
