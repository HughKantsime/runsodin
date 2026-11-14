"""live_state reads the stored printer report whether the database returns text or decoded JSON.

SQLite returns the stored JSON as text; PostgreSQL's JSON column returns it already decoded.
Before 2.2.1 the decoded form was refused, so on PostgreSQL every reader saw no live state.
"""
import json
import time

import pytest

from core.monitor_observations import MAX_REPORT_AGE, cache_key, live_state


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Conn:
    def __init__(self, value):
        self._value = value
        self.keys = []

    def execute(self, _statement, params):
        self.keys.append(params['key'])
        return _Result(self._value)


def _report(state='RUNNING', age=0):
    return {'state': state, 'at': time.time() - age}


_FORMS = [json.dumps, lambda value: value, lambda value: json.dumps(value).encode(),
          lambda value: bytearray(json.dumps(value).encode()), lambda value: memoryview(json.dumps(value).encode())]
_FORM_IDS = ['text', 'decoded', 'bytes', 'bytearray', 'memoryview']


@pytest.mark.parametrize('encode', _FORMS, ids=_FORM_IDS)
def test_a_fresh_report_is_read_in_every_stored_form(encode):
    conn = _Conn(encode(_report()))
    assert live_state(conn, 7, 0) == 'RUNNING'
    assert conn.keys == [cache_key(7)]


@pytest.mark.parametrize('encode', _FORMS, ids=_FORM_IDS)
def test_a_stale_report_is_refused_in_every_stored_form(encode):
    assert live_state(_Conn(encode(_report(age=MAX_REPORT_AGE + 5))), 7, 0) is None


@pytest.mark.parametrize('value', [None, '', 'not json', 5, [], {'state': 'RUNNING'}, {'at': 'soon', 'state': 'RUNNING'}, b'\xff\xfe', b'not json'])
def test_bad_stored_values_fail_closed(value):
    assert live_state(_Conn(value), 7, 0) is None


@pytest.mark.parametrize('encode', [json.dumps, lambda value: value], ids=['text', 'decoded'])
@pytest.mark.parametrize('report', [
    {'state': ['RUNNING']}, {'state': None}, {'state': 5}, {'state': {'a': 1}},
    {'at': 10 ** 400}, {'at': -(10 ** 400)}, {'at': True}, {'at': [1]}, {'at': None},
], ids=['state-list', 'state-null', 'state-number', 'state-object',
        'at-huge', 'at-huge-negative', 'at-boolean', 'at-list', 'at-null'])
def test_a_report_with_a_wrong_typed_field_fails_closed_without_raising(encode, report):
    assert live_state(_Conn(encode({**_report(), **report})), 7, 0) is None


@pytest.mark.parametrize('at', ['NaN', 'Infinity', '-Infinity'])
def test_a_non_finite_time_fails_closed(at):
    assert live_state(_Conn('{"state": "RUNNING", "at": %s}' % at), 7, 0) is None
