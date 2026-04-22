from __future__ import annotations

import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen


MODULE_PATH = Path(__file__).resolve().parents[1] / "broker.py"
SPEC = importlib.util.spec_from_file_location("broker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def test_long_poll_does_not_block_send_or_status() -> None:
    server = broker.make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            recv_future = executor.submit(
                _request_json,
                f"{base_url}/recv?role=builder&wait=5",
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
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)

    assert recv_payload["count"] == 1
    assert recv_payload["messages"][0]["body"] == "TASK-015 ready"
