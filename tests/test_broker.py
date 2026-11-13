from __future__ import annotations

import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "broker.py"
SPEC = importlib.util.spec_from_file_location("broker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)

AUTH_TOKEN = "test-token-1234567890"


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 5.0,
    token: str | None = AUTH_TOKEN,
) -> dict[str, object]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-Agent-Collab-Token"] = token

    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _start_server() -> tuple[object, threading.Thread, str]:
    server = broker.make_server(port=0, auth_token=AUTH_TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _stop_server(server, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=1.0)


def test_auth_is_required_by_default() -> None:
    server, thread, base_url = _start_server()
    try:
        with pytest.raises(HTTPError) as excinfo:
            _request_json(f"{base_url}/status", token=None)
    finally:
        _stop_server(server, thread)

    assert excinfo.value.code == 401


def test_long_poll_does_not_block_send_or_status() -> None:
    server, thread, base_url = _start_server()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            recv_future = executor.submit(
                _request_json,
                f"{base_url}/recv?{urlencode({'role': 'builder', 'wait': 5})}",
                timeout=6.0,
            )
            time.sleep(0.2)

            start = time.monotonic()
            status_payload = _request_json(f"{base_url}/status", timeout=1.0)
            status_elapsed = time.monotonic() - start

            assert status_payload["total"] == 0
            assert status_elapsed < 0.5

            start = time.monotonic()
            send_payload = _request_json(
                f"{base_url}/send",
                method="POST",
                payload={
                    "to": "builder",
                    "from": "architect",
                    "body": "TASK-015 ready",
                },
                timeout=1.0,
            )
            send_elapsed = time.monotonic() - start

            assert send_payload["to"] == "builder"
            assert send_elapsed < 0.5

            recv_payload = recv_future.result(timeout=1.5)
    finally:
        _stop_server(server, thread)

    assert recv_payload["count"] == 1
    assert recv_payload["messages"][0]["body"] == "TASK-015 ready"


def test_recv_filters_by_kind_channel_and_after_id() -> None:
    server, thread, base_url = _start_server()

    try:
        _request_json(
            f"{base_url}/send",
            method="POST",
            payload={"to": "builder", "from": "architect", "body": "signal"},
        )
        _request_json(
            f"{base_url}/send",
            method="POST",
            payload={
                "to": "builder",
                "from": "architect",
                "body": "chat-one",
                "kind": "chat",
                "channel": "dm:architect:builder",
            },
        )
        _request_json(
            f"{base_url}/send",
            method="POST",
            payload={
                "to": "builder",
                "from": "architect",
                "body": "chat-two",
                "kind": "chat",
                "channel": "dm:architect:other",
            },
        )

        payload = _request_json(
            f"{base_url}/recv?{urlencode({'role': 'builder', 'kind': 'chat', 'channel': 'dm:architect:builder', 'after_id': 1})}"
        )
    finally:
        _stop_server(server, thread)

    assert payload["count"] == 1
    assert payload["messages"][0]["body"] == "chat-one"
    assert payload["messages"][0]["kind"] == "chat"
    assert payload["messages"][0]["channel"] == "dm:architect:builder"


def test_history_returns_bidirectional_chat_transcript() -> None:
    server, thread, base_url = _start_server()

    try:
        _request_json(
            f"{base_url}/send",
            method="POST",
            payload={
                "to": "architect",
                "from": "builder",
                "body": "Can you review #126?",
                "kind": "chat",
                "channel": "dm:architect:builder",
            },
        )
        _request_json(
            f"{base_url}/send",
            method="POST",
            payload={
                "to": "builder",
                "from": "architect",
                "body": "Reviewing it now.",
                "kind": "chat",
                "channel": "dm:architect:builder",
            },
        )

        payload = _request_json(
            f"{base_url}/history?{urlencode({'channel': 'dm:architect:builder', 'kind': 'chat', 'limit': 10})}"
        )
    finally:
        _stop_server(server, thread)

    assert payload["count"] == 2
    assert [message["body"] for message in payload["messages"]] == [
        "Can you review #126?",
        "Reviewing it now.",
    ]


def test_send_rejects_control_characters_in_body() -> None:
    server, thread, base_url = _start_server()

    try:
        with pytest.raises(HTTPError) as excinfo:
            _request_json(
                f"{base_url}/send",
                method="POST",
                payload={
                    "to": "builder",
                    "from": "architect",
                    "body": "hello\x1b[31m",
                },
            )
    finally:
        _stop_server(server, thread)

    assert excinfo.value.code == 400


def test_generation_changes_on_clear_and_restart() -> None:
    store = broker.MessageStore()
    initial = store.status()['generation']
    store.send(to='builder', from_actor='architect', body='first')
    snapshot = store.receive_snapshot(role='builder')
    assert snapshot['generation'] == initial
    assert snapshot['total'] == snapshot['count'] == 1
    store.clear()
    store.send(to='builder', from_actor='architect', body='replacement')
    replacement = store.receive_snapshot(role='builder')
    assert replacement['generation'] != initial
    assert replacement['messages'][0]['id'] == 1
    assert broker.MessageStore().status()['generation'] != replacement['generation']


def test_clear_wakes_snapshot_reader() -> None:
    store = broker.MessageStore()
    initial = store.status()['generation']
    started = threading.Event()
    original_filter = store._filter_locked

    def observed_filter(**kwargs):
        started.set()
        return original_filter(**kwargs)

    store._filter_locked = observed_filter
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.receive_snapshot, role='builder', timeout=30)
        assert started.wait(2)
        store.clear()
        snapshot = pending.result(timeout=2)
    assert snapshot['generation'] != initial
    assert snapshot['messages'] == []


def test_http_receive_includes_atomic_generation() -> None:
    server, thread, url = _start_server()
    try:
        initial = _request_json(url + '/status')['generation']
        snapshot = _request_json(url + '/recv?role=builder')
        assert snapshot == {'generation': initial, 'total': 0, 'count': 0, 'messages': []}
        _request_json(url + '/clear', method='POST', payload={})
        assert _request_json(url + '/recv?role=builder')['generation'] != initial
    finally:
        _stop_server(server, thread)


def test_send_rejects_reply_from_previous_generation() -> None:
    server, thread, url = _start_server()
    try:
        generation = _request_json(url + '/status')['generation']
        _request_json(url + '/clear', method='POST', payload={})
        with pytest.raises(HTTPError) as exc:
            _request_json(url + '/send', method='POST', payload={
                'to': 'architect', 'from': 'builder', 'body': 'old result',
                'generation': generation,
            })
        assert exc.value.code == 409
        assert _request_json(url + '/status')['total'] == 0
        current = _request_json(url + '/status')['generation']
        sent = _request_json(url + '/send', method='POST', payload={
            'to': 'architect', 'from': 'builder', 'body': 'current result',
            'generation': current,
        })
        assert sent['id'] == 1
    finally:
        _stop_server(server, thread)


def test_receive_with_old_generation_does_not_wait() -> None:
    server, thread, url = _start_server()
    try:
        generation = _request_json(url + '/status')['generation']
        _request_json(url + '/clear', method='POST', payload={})
        result = _request_json(url + '/recv?' + urlencode({
            'role': 'builder', 'generation': generation, 'wait': 30,
        }), timeout=2)
        assert result['generation'] != generation
        assert result['messages'] == []
    finally:
        _stop_server(server, thread)
