from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SPEC = importlib.util.spec_from_file_location('response_broker', Path(__file__).resolve().parents[1] / 'broker.py')
assert SPEC and SPEC.loader
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)
TOKEN = 'response-tests-private-token'


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def tracked(store, **policy):
    return store.send(to='builder', from_actor='architect', body='Do work', kind='chat',
                      channel='dm:architect:builder', response=policy)


def respond(store, message, state, detail=''):
    return store.respond(generation=store.status()['generation'], message_id=message['id'],
                         actor='builder', state=state, detail=detail)


def record(store):
    return store.responses()['responses'][0]


def test_deadline_nudges_are_bounded_and_keep_original_response_deadline():
    clock = Clock()
    store = broker.MessageStore(clock=clock)
    message = tracked(store, ack_timeout=2, progress_timeout=4, max_nudges=2)
    assert message['response_required'] is True
    original = record(store)
    assert original['state'] == 'queued'
    clock.advance(2)
    store.check_deadlines()
    first = record(store)
    assert first['state'] == 'queued' and first['overdue']
    assert first['nudges'] == 1
    assert first['deadline'] == original['deadline']
    assert first['updated'] == original['updated']
    nudge = store.recv(role='builder')[-1]
    assert nudge['nudge_for'] == message['id'] and nudge['response_required']
    assert len(store.responses()['responses']) == 1
    store.check_deadlines()
    assert record(store)['nudges'] == 1
    clock.advance(2)
    store.check_deadlines()
    assert record(store)['nudges'] == 2
    clock.advance(2)
    store.check_deadlines()
    assert record(store)['state'] == 'timed_out'
    alert = store.recv(role='architect')[-1]
    assert alert['kind'] == 'signal' and alert['from'] == 'builder'
    assert alert['response_alert_for'] == message['id']
    assert alert['response_state'] == 'timed_out'
    before = store.status()['total']
    clock.advance(100)
    store.check_deadlines()
    assert store.status()['total'] == before


def test_ack_and_working_use_progress_deadline_without_resetting_nudge_budget():
    clock = Clock()
    store = broker.MessageStore(clock=clock)
    message = tracked(store, ack_timeout=2, progress_timeout=4, max_nudges=1)
    clock.advance(2)
    store.check_deadlines()
    assert record(store)['nudges'] == 1
    respond(store, message, 'acknowledged')
    assert record(store)['overdue'] is False
    clock.advance(3)
    store.check_deadlines()
    assert record(store)['state'] == 'acknowledged'
    respond(store, message, 'working', 'Tests running')
    clock.advance(3)
    store.check_deadlines()
    assert record(store)['state'] == 'working'
    clock.advance(1)
    store.check_deadlines()
    assert record(store)['state'] == 'timed_out'
    assert record(store)['nudges'] == 1


def test_duplicate_receipts_do_not_extend_deadline():
    for state in ('acknowledged', 'working'):
        clock = Clock()
        store = broker.MessageStore(clock=clock)
        message = tracked(store, progress_timeout=2, max_nudges=0)
        respond(store, message, state, 'same')
        clock.advance(1)
        respond(store, message, state, 'same')
        if state == 'acknowledged':
            respond(store, message, state, 'another acknowledgement')
        clock.advance(1)
        store.check_deadlines()
        assert record(store)['state'] == 'timed_out'


def test_unrelated_output_never_counts_as_response():
    clock = Clock()
    store = broker.MessageStore(clock=clock)
    tracked(store, ack_timeout=1, max_nudges=0)
    store.send(to='architect', from_actor='builder', body='I am doing something',
               kind='chat', channel='dm:architect:builder')
    assert record(store)['state'] == 'queued'
    clock.advance(1)
    store.check_deadlines()
    assert record(store)['state'] == 'timed_out'


@pytest.mark.parametrize('state', ['answered', 'failed'])
def test_terminal_responses_never_reopen_or_nudge(state):
    clock = Clock()
    store = broker.MessageStore(clock=clock)
    message = tracked(store)
    respond(store, message, state, 'Result')
    count = store.status()['total']
    clock.advance(9999)
    store.check_deadlines()
    assert store.status()['total'] == count
    assert record(store)['deadline'] is None
    with pytest.raises(ValueError, match='terminal'):
        respond(store, message, 'working')
    if state == 'failed':
        assert store.recv(role='architect')[-1]['response_state'] == 'failed'


def test_working_cannot_regress_to_acknowledged():
    store = broker.MessageStore()
    message = tracked(store)
    respond(store, message, 'working', 'Step1')
    with pytest.raises(ValueError, match='backwards'):
        respond(store, message, 'acknowledged')


def test_clear_cancels_old_deadlines_and_rejects_old_response():
    clock = Clock()
    store = broker.MessageStore(clock=clock)
    message = tracked(store, ack_timeout=1)
    generation = store.status()['generation']
    store.clear()
    tracked(store, ack_timeout=30)
    with pytest.raises(ValueError, match='generation'):
        store.respond(generation=generation, message_id=message['id'], actor='builder', state='answered')
    clock.advance(2)
    store.check_deadlines()
    assert store.status()['total'] == 1
    assert record(store)['state'] == 'queued'


def test_receive_batches_preserve_earliest_pending_messages():
    store = broker.MessageStore()
    for n in range(40):
        store.send(to='builder', from_actor='architect', body=str(n))
    batch = store.receive_snapshot(role='builder', limit=16)['messages']
    assert [m['id'] for m in batch] == list(range(1, 17))
    assert store.recv(role='builder', after_id=16, limit=16)[0]['id'] == 17
    assert store.history(role='builder', limit=16)[0]['id'] == 25
    assert store.recv(role='builder', limit=0) == []
    assert store.history(role='builder', limit=0) == []


@pytest.fixture
def server():
    instance = broker.make_server(port=0, auth_token=TOKEN)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{instance.server_address[1]}'
    yield instance, url
    instance.shutdown()
    instance.server_close()
    thread.join(2)


def request(url, path, body=None, token=TOKEN):
    headers = {'X-Agent-Collab-Token': token, 'Content-Type': 'application/json'}
    data = None if body is None else json.dumps(body).encode()
    with urlopen(Request(url + path, headers=headers, data=data), timeout=3) as result:
        return json.load(result)


def send_body(**overrides):
    return {'to': 'builder', 'from': 'architect', 'body': 'Do work', 'kind': 'chat',
            'channel': 'dm:architect:builder', 'response': {}, **overrides}


@pytest.mark.parametrize('policy', [None, [], True, {'unknown': 1}, {'ack_timeout': True},
    {'ack_timeout': 0}, {'progress_timeout': 3601}, {'max_nudges': 6}, {'max_nudges': -1},
    {'progress_timeout': 1.5}, {'ack_timeout': '30'}])
def test_http_refuses_invalid_policies(server, policy):
    _, url = server
    with pytest.raises(HTTPError) as exc:
        request(url, '/send', send_body(response=policy))
    assert exc.value.code == 400
    assert request(url, '/status')['total'] == 0


@pytest.mark.parametrize('override', [{'kind': 'signal'}, {'to': 'architect'}, {'channel': None}])
def test_http_refuses_untrackable_route(server, override):
    _, url = server
    with pytest.raises(HTTPError) as exc:
        request(url, '/send', send_body(**override))
    assert exc.value.code == 400


@pytest.mark.parametrize('override,code', [({'message_id': True}, 400), ({'message_id': 999}, 409),
    ({'actor': 'outsider'}, 409), ({'generation': 'old'}, 409), ({'state': 'queued'}, 400),
    ({'detail': '\n'}, 400), ({'detail': '\ud800'}, 400), ({'extra': 1}, 400), ({'detail': 'x' * 4097}, 400)])
def test_http_validates_correlated_responses(server, override, code):
    _, url = server
    sent = request(url, '/send', send_body())
    initial = request(url, '/responses')
    payload = {'generation': initial['generation'], 'message_id': sent['id'],
               'actor': 'builder', 'state': 'acknowledged', **override}
    with pytest.raises(HTTPError) as exc:
        request(url, '/response', payload)
    assert exc.value.code == code
    assert request(url, '/responses')['responses'][0]['state'] == 'queued'


def test_http_auth_and_channel_filter(server):
    _, url = server
    request(url, '/send', send_body())
    with pytest.raises(HTTPError) as exc:
        request(url, '/responses', token='wrong')
    assert exc.value.code == 401
    assert request(url, '/responses?channel=another')['responses'] == []
    with pytest.raises(HTTPError) as exc:
        request(url, '/responses?channel=bad!')
    assert exc.value.code == 400


def test_service_actions_times_out_without_client_polling(server):
    _, url = server
    request(url, '/send', send_body(response={'ack_timeout': 1, 'max_nudges': 0}))
    time.sleep(1.7)
    result = request(url, '/responses')['responses'][0]
    assert result['state'] == 'timed_out'
    assert result['nudges'] == 0


def test_public_record_mutation_cannot_change_policy():
    store = broker.MessageStore()
    tracked(store)
    result = record(store)
    result['policy']['max_nudges'] = 100
    assert record(store)['policy']['max_nudges'] == 2


def test_http_refuses_invalid_unicode_body(server):
    _, url = server
    with pytest.raises(HTTPError) as exc:
        request(url, '/send', send_body(body='\ud800'))
    assert exc.value.code == 400


def test_tracking_capacity_refuses_without_partial_message(monkeypatch):
    monkeypatch.setattr(broker, 'MAX_TRACKED_RESPONSES', 1)
    store = broker.MessageStore()
    tracked(store)
    with pytest.raises(ValueError, match='capacity'):
        tracked(store)
    assert store.status()['total'] == 1
    assert len(store.responses()['responses']) == 1
    store.clear()
    assert tracked(store)['id'] == 1
