from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location('response_cli', Path(__file__).resolve().parents[1] / 'brokerctl.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def invoke(monkeypatch, *args):
    calls = []
    monkeypatch.setattr(sys, 'argv', ['brokerctl.py', *args])
    monkeypatch.setattr(cli, 'resolve_token', lambda *a: 'test-auth-token')
    monkeypatch.setattr(cli, '_post_json', lambda url, payload, **kwargs: calls.append((url, payload, kwargs)) or {'response_required': 'response' in payload})
    monkeypatch.setattr(cli, '_get_json', lambda url, **kwargs: calls.append((url, kwargs)) or {'generation': 'epoch', 'responses': []})
    assert cli.main() == 0
    return calls


def test_tracked_send_policy(monkeypatch):
    calls = invoke(monkeypatch, 'send', '--to', 'builder', '--from-role', 'architect',
                   '--body', 'task', '--kind', 'chat', '--channel', 'dm:architect:builder',
                   '--require-response', '--max-nudges', '0')
    assert calls[-1][1]['response'] == {'ack_timeout': 30, 'progress_timeout': 120, 'max_nudges': 0}


def test_untracked_send_unchanged(monkeypatch):
    calls = invoke(monkeypatch, 'send', '--to', 'builder', '--from-role', 'architect', '--body', 'note')
    assert 'response' not in calls[0][1]


def test_explicit_response_keeps_generation_and_id(monkeypatch):
    calls = invoke(monkeypatch, 'respond', '--message-id', '12', '--generation', 'epoch',
                   '--actor', 'builder', '--state', 'working', '--detail', 'Tests running')
    assert calls[0][1] == {'message_id': 12, 'generation': 'epoch', 'actor': 'builder',
                           'state': 'working', 'detail': 'Tests running'}


def test_responses_channel_is_encoded(monkeypatch):
    calls = invoke(monkeypatch, 'responses', '--channel', 'dm:a:b')
    assert calls[0][0].endswith('/responses?channel=dm%3Aa%3Ab')


@pytest.mark.parametrize('extra', [
    ['--ack-timeout', '2'],
    ['--require-response'],
    ['--require-response', '--kind', 'chat', '--channel', 'dm:a:b', '--ack-timeout', '0'],
    ['--require-response', '--kind', 'chat', '--channel', 'dm:a:b', '--progress-timeout', '3601'],
    ['--require-response', '--kind', 'chat', '--channel', 'dm:a:b', '--max-nudges', '6'],
])
def test_invalid_policy_fails_before_request(monkeypatch, extra):
    monkeypatch.setattr(sys, 'argv', ['brokerctl.py', 'send', '--to', 'builder',
                                     '--from-role', 'architect', '--body', 'task', *extra])
    monkeypatch.setattr(cli, 'resolve_token', lambda *a: pytest.fail('invalid input read token'))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def tracked_args(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['brokerctl.py', 'send', '--to', 'builder',
        '--from-role', 'architect', '--body', 'task', '--kind', 'chat',
        '--channel', 'dm:architect:builder', '--require-response'])
    monkeypatch.setattr(cli, 'resolve_token', lambda *a: 'test-auth-token')


def test_old_broker_cannot_silently_accept_untracked_work(monkeypatch, capsys):
    tracked_args(monkeypatch)
    monkeypatch.setattr(cli, '_get_json', lambda *a, **kw: {})
    monkeypatch.setattr(cli, '_post_json', lambda *a, **kw: pytest.fail('must not send work'))
    assert cli.main() == 1
    assert 'does not support response monitoring' in capsys.readouterr().err


def test_tracking_confirmation_is_required_after_send(monkeypatch, capsys):
    tracked_args(monkeypatch)
    monkeypatch.setattr(cli, '_get_json', lambda *a, **kw: {'generation': 'epoch', 'responses': []})
    sends = []
    monkeypatch.setattr(cli, '_post_json', lambda url, payload, **kw: sends.append(payload) or {'id': 1})
    assert cli.main() == 1
    assert len(sends) == 1 and sends[0]['generation'] == 'epoch'
    assert 'delivery is uncertain' in capsys.readouterr().err
