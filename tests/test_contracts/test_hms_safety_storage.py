"""Raw safety survives display filtering, persistence and unattended HTTP resume."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_control_audit_small_fixes import (
    audited, control_client, token_client, review_db, rows,
)
from tests.test_contracts.test_hms_observation_boundaries import hms_boundary, monitor_db

HIDDEN = {'attr': 0x05000300, 'code': 0x00020018}
NOTICE = {'attr': 0x05010400, 'code': 0x00030002}


def test_hidden_print_error_lookup_remains_empty(monkeypatch):
    from modules.printers import hms_codes
    monkeypatch.setattr(hms_codes, 'load_bundle', lambda _: {'device_error': {'03004002': ''}})
    assert hms_codes.lookup_print_error(0x03004002, '00M') == ''


@pytest.fixture
def api_defaults(hms_boundary):
    db, _, _, _ = hms_boundary
    # The monitor fixture inserts SQL rows without ORM-side API defaults.
    db.execute(text("UPDATE printers SET slot_count=4,tags='[]',timelapse_enabled=0 WHERE id=9"))
    db.commit()
    return hms_boundary


@pytest.fixture
def hidden_bundle(monkeypatch):
    from modules.printers import hms_codes
    monkeypatch.setattr(hms_codes, 'load_bundle', lambda _: {
        'device_hms': {'0500030000020018': '', '0501040000030002': 'Synthetic notice'},
    })


def stored(db, pid=9):
    db.expire_all()
    return db.execute(text('SELECT hms_errors FROM printers WHERE id=:pid'), {'pid': pid}).scalar_one()


def refusal(db, value, pid=9):
    from modules.printers.resume_guard import resume_refusal
    return resume_refusal(db, SimpleNamespace(id=pid, hms_errors=value), {'_auth_kind': 'user_api_token'})


@pytest.mark.parametrize('raw', [HIDDEN, None, {}, 'unreadable', {'attr': 1},
    {'attr': True, 'code': 0x30002}, {'attr': 1, 'code': -1},
    {'attr': 1, 'code': '05010400_00030002'}, {'attr': 1, 'code': 0x100000000}])
def test_raw_safety_survives_filtering_and_clear(api_defaults, hidden_bundle, raw):
    db, handler, _, outgoing = api_defaults
    from modules.printers.models import Printer
    from modules.printers.schemas import PrinterResponse
    from modules.printers.hms_state import has_display_entries
    result = handler.process_hms_errors(9, [raw], live_state='FAILED')
    value = stored(db)
    assert json.loads(value) == [{'display': False, 'hms_safety': raw}]
    assert result['errors'] == [] and outgoing == []
    assert refusal(db, value).value == 'printer_fault_active'
    assert not has_display_entries(value)
    assert PrinterResponse.model_validate(db.get(Printer, 9)).model_dump()['hms_errors'] == '[]'
    assert db.execute(text('SELECT last_error_code FROM printers WHERE id=9')).scalar_one() is None
    assert db.execute(text('SELECT count(*) FROM hms_error_history')).scalar_one() == 0
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one() == 'running'
    # A missing report preserves state; only an explicit empty report clears it.
    handler.process_hms_errors(9, None)
    assert stored(db) == value
    handler.process_hms_errors(9, [])
    assert stored(db) == '[]' and refusal(db, stored(db)) is None


@pytest.mark.parametrize('level,blocked', [(0, True), (1, True), (2, True), (3, False), (4, False), (5, True)])
def test_hidden_levels_use_raw_severity(hms_boundary, monkeypatch, level, blocked):
    from modules.printers import hms_codes
    db, handler, _, outgoing = hms_boundary
    event = {'attr': 0x05000300, 'code': level << 16 | 24}
    monkeypatch.setattr(hms_codes, 'load_bundle', lambda _: {
        'device_hms': {f'{event["attr"]:08X}{event["code"]:08X}': ''},
    })
    handler.process_hms_errors(9, [event])
    assert bool(refusal(db, stored(db))) == blocked
    assert outgoing == []


def test_mixed_reports_preserve_display_and_alert_dedup(api_defaults, hidden_bundle):
    from modules.printers.models import Printer
    from modules.printers.schemas import PrinterResponse
    db, handler, _, outgoing = api_defaults
    fault = {'attr': 0x03003100, 'code': 0x00010001}
    for _ in range(2):
        handler.process_hms_errors(9, [HIDDEN, None, NOTICE, fault])
    value = stored(db)
    assert len(json.loads(value)) == 4
    wire = PrinterResponse.model_validate(db.get(Printer, 9)).model_dump()
    assert [e['raw_code'] for e in json.loads(wire['hms_errors'])] == [NOTICE['code'], fault['code']]
    assert len(outgoing) == 1
    assert refusal(db, value).value == 'printer_fault_active'
    handler.process_hms_errors(9, [NOTICE])
    assert refusal(db, stored(db)) is None
    assert len(json.loads(stored(db))) == 1


def test_classification_retry_keeps_safety_without_consuming_alert(hms_boundary, hidden_bundle, monkeypatch):
    db, handler, registry, outgoing = hms_boundary
    class Unavailable:
        def classify_monitor_observation(self, *args, **kwargs):
            raise RuntimeError('Synthetic unavailable classification')
    monkeypatch.setattr(registry, 'get_provider', lambda _: Unavailable())
    fault = {'attr': 0x03003100, 'code': 0x00010001}
    assert handler.process_hms_errors(9, [HIDDEN, fault])['retryable']
    assert refusal(db, stored(db)).value == 'printer_fault_active'
    assert outgoing == []
    monkeypatch.setattr(registry, 'get_provider', lambda _: None)
    handler.process_hms_errors(9, [HIDDEN, fault])
    assert len(outgoing) == 1
    assert len(json.loads(stored(db))) == 2


def test_education_queue_indication_ignores_safety_only_records(hms_boundary, monkeypatch):
    from modules.organizations.education_queues import head_block_reason
    from modules.printers import services
    db, _, _, _ = hms_boundary
    monkeypatch.setattr(services, 'evaluate_submission_compatibility', lambda *a, **kw: {'compatible': True})
    row = dict(hold=False, printer_active=True, printer_id=9, job_status='scheduled',
               print_file_id=11, gcode_state='IDLE', hms_errors=json.dumps([
                   {'display': False, 'hms_safety': HIDDEN}]))
    assert head_block_reason(db, row, 1).startswith('Live readiness')
    row['hms_errors'] = json.dumps([NOTICE])
    assert head_block_reason(db, row, 1).startswith('Printer has recorded HMS diagnostics')


@pytest.mark.parametrize('api_type', ['bambu', 'moonraker', None])
def test_api_filters_safety_even_after_printer_type_changes(api_defaults, hidden_bundle, api_type):
    from modules.printers.models import Printer
    from modules.printers.schemas import PrinterResponse
    db, handler, _, _ = api_defaults
    handler.process_hms_errors(9, [HIDDEN])
    db.expire_all()
    printer = db.get(Printer, 9)
    printer.api_type = api_type
    assert PrinterResponse.model_validate(printer).model_dump()['hms_errors'] == '[]'


@pytest.mark.parametrize('raw', [HIDDEN, None, {'attr': 1}])
def test_filtered_packet_blocks_http_resume_then_clear_allows(audited, review_db, hidden_bundle, monkeypatch, raw):
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import error_handling
    client, issue, calls = audited
    @contextmanager
    def database():
        conn = review_db.get_bind().raw_connection()
        try:
            yield _ConnectionAdapter(conn, False)
        finally:
            conn.close()
    monkeypatch.setattr(error_handling, 'get_db', database)
    headers = issue(['write:printers'])
    error_handling.process_hms_errors(299, [raw])
    response = client.post('/api/v1/printers/299/resume', headers=headers)
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'printer_fault_active'
    assert not calls and len(rows(review_db)) == 1
    error_handling.process_hms_errors(299, [])
    assert client.post('/api/v1/printers/299/resume', headers=headers).status_code == 200
    assert calls == [(299, 'resume_print', ())]
