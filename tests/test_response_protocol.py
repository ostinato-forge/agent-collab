import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import response_protocol as protocol


def marker(**changes):
    return protocol.PREFIX + json.dumps(dict(message_id=3, state='acknowledged', detail='ok', **changes))


def test_only_explicit_standalone_markers_are_accepted():
    value = marker()
    text = '\n'.join(['unrelated output', '> ' + value, '  ' + value,
                      '```', value, '```', value])
    assert list(protocol.markers(text)) == [{'message_id': 3, 'state': 'acknowledged', 'detail': 'ok'}]


def test_malformed_markers_never_raise_or_ack():
    samples = [None, [], {}, {'message_id': True, 'state': 'answered'},
               {'message_id': 3, 'state': []}, {'message_id': 3, 'state': {}},
               {'message_id': 3, 'state': 'answered', 'detail': '\ud800'},
               {'message_id': 3, 'state': 'answered', 'detail': 'x\ny'},
               {'message_id': 3, 'state': 'answered', 'detail': 'x' * 1025},
               {'message_id': 3, 'state': 'unknown'}]
    for sample in samples:
        assert list(protocol.markers(protocol.PREFIX + json.dumps(sample))) == []


def test_no_transport_or_tool_output_is_model_response():
    value = marker()
    for kind in ('user', 'system', 'tool_result'):
        assert protocol.model_text({'type': kind, 'message': {'role': 'assistant',
            'content': [{'type': 'text', 'text': value}]}}) == []
    assert protocol.model_text({'type': 'assistant', 'parent_tool_use_id': 'child',
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': value}]}}) == []
    assert protocol.model_text({'type': 'assistant', 'message': {'role': 'assistant',
        'content': [{'type': 'tool_result', 'text': value}]}}) == []


def test_known_ids_monotonic_and_duplicate_receipts():
    receipts = protocol.Receipts()
    receipts.add({'id': 3, 'response_required': True})
    receipts.add({'id': 4})
    ack = {'message_id': 3, 'state': 'acknowledged', 'detail': 'ok'}
    assert receipts.accept(ack)
    receipts.record(ack)
    assert not receipts.accept(ack)
    assert not receipts.accept(dict(ack, message_id=4))
    working = dict(ack, state='working')
    receipts.record(working)
    assert not receipts.accept(dict(ack, detail='again'))
    receipts.record(dict(ack, state='answered'))
    assert not receipts.accept(dict(ack, state='failed'))


def test_nested_json_and_split_code_fences_cannot_ack():
    assert list(protocol.markers(protocol.PREFIX + '[' * 2000 + '0' + ']' * 2000)) == []
    texts = protocol.model_text({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'text', 'text': '```'}, {'type': 'text', 'text': marker()},
        {'type': 'text', 'text': '```'},
    ]}})
    assert [receipt for text in texts for receipt in protocol.markers(text)] == []
