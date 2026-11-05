"""Synthetic vendor envelopes: semantic regressions, no redistributed list text."""
import io
import json
import hashlib
from datetime import datetime
from types import SimpleNamespace
import pytest
from modules.printers import bambu_hms_bundle as bundle
from modules.printers.hms_codes import lookup_hms_code, lookup_print_error, parse_hms_errors
from ops import fetch_bambu_hms as fetcher

PRODUCTION = {
 '094': [('05010400_00030004', 'Clean and lubricate rails'),
         ('03002E00_00030001', 'Recalibrate motor noise cancellation'),
         ('05010400_00030002', 'Lubricate threaded rods'),
         ('0C000200_00010001', 'Laser fault')],
 '00M': [('03000D00_0001000C', 'Heatbed leveling data abnormal'),
         ('0C000100_00010004', 'Micro Lidar lens dirty'),
         ('0C000300_00020001', 'Exposure metering failed'),
         ('0C000300_0003000B', 'Inspecting first layer'),
         ('07010200_00010001', 'AMS odometry fault')],
 '01P': [('03003100_00010001', 'Part cooling fan slow or stopped')],
 '039': [('12002200_00020001', 'AMS lite slot 3 runout')],
 '22E': [('05000600_00020070', '')],
}


def response(entries=None):
    return {'result': 0, 'ver': 202611050001, 'data': {
      'device_hms': {'ver': 1, 'en': [{'ecode': c.replace('_',''), 'intro': m} for c,m in (entries or PRODUCTION['094'])]},
      'device_error': {'ver': 1, 'en': [{'ecode': '03004002', 'intro': 'Synthetic print action'}]}}}


class Reply(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, payload): self.payload, self.calls = payload, []
    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        return Reply(self.payload)


@pytest.fixture
def lists(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, 'BUNDLE_DIR', tmp_path)
    bundle.load_bundle.cache_clear()
    for prefix, entries in PRODUCTION.items():
        fetcher.write_bundle(tmp_path, [prefix], Opener(json.dumps(response(entries)).encode()))
    yield tmp_path
    bundle.load_bundle.cache_clear()


@pytest.mark.parametrize('prefix,code,message', [(p,c,m) for p,rows in PRODUCTION.items() for c,m in rows])
def test_production_meanings_from_recorded_envelope(lists, prefix, code, message):
    assert lookup_hms_code(code, prefix) == message
    assert lookup_hms_code(code.replace('_','-').lower(), prefix) == message
    attr, raw = [int(c,16) for c in code.split('_')]
    parsed = parse_hms_errors([dict(attr=attr,code=raw)], prefix)
    assert bool(parsed) == bool(message)
    if message:
        assert parsed[0]['message'] == message
        assert parsed[0]['wiki_url'] == 'https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/' + '_'.join(code.replace('_','')[i:i+4] for i in range(0,16,4))


def test_prefix_isolation_plain_unknown_and_print_error(lists):
    assert lookup_hms_code('03000D00_0001000C', '00M') == 'Heatbed leveling data abnormal'
    assert lookup_hms_code('03000D00_0001000C', '094') == 'Printer code 0300-0D00-0001-000C'
    assert lookup_hms_code('03000D00_0001000C', '') == 'Printer code 0300-0D00-0001-000C'
    assert lookup_print_error('0300_4002', '094') == 'Synthetic print action'
    assert lookup_print_error(0x03004002, '094') == 'Synthetic print action'
    assert lookup_hms_code('DEADBEEF_CAFEBABE', '../') == 'Printer code DEAD-BEEF-CAFE-BABE'


@pytest.mark.parametrize('level,expected', [(1,'critical'),(2,'error'),(3,'info'),(4,'info'),(0,'unknown'),(5,'unknown')])
def test_severity_comes_from_code_not_attr(level,expected):
    assert parse_hms_errors([{'attr':0x12002200,'code':level<<16|1}])[0]['severity'] == expected


def test_empty_entry_hidden_dedup_and_bad_packets(lists):
    assert parse_hms_errors([{'attr':0x05000600,'code':0x00020070}], '22E') == []
    event = {'attr':0x03002E00,'code':0x00030001}
    assert len(parse_hms_errors([event,event,None,{}, {'attr':True,'code':1}], '094')) == 1


@pytest.mark.parametrize('bad', [b'not json', b'{}', b'{"result":1}', b'[]'])
def test_failed_fetch_preserves_last_good(tmp_path,bad):
    fetcher.write_bundle(tmp_path, ['094'], Opener(json.dumps(response()).encode()))
    original = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError): fetcher.write_bundle(tmp_path, ['094'], Opener(bad))
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == original


@pytest.mark.parametrize('mutation', ['missing_table','not_list','bad_code','duplicate','bad_intro','missing_version'])
def test_shape_validation(mutation):
    raw = response()
    rows = raw['data']['device_hms']['en']
    if mutation == 'missing_table': del raw['data']['device_error']
    if mutation == 'not_list': raw['data']['device_hms']['en'] = {}
    if mutation == 'bad_code': rows[0]['ecode'] = 'bogus'
    if mutation == 'duplicate': rows.append(rows[0])
    if mutation == 'bad_intro': rows[0]['intro'] = None
    if mutation == 'missing_version': del raw['ver']
    with pytest.raises(ValueError): bundle.validate_response(raw)


def test_fetch_bounds_and_metadata(tmp_path,monkeypatch):
    opener=Opener(json.dumps(response()).encode())
    index=fetcher.write_bundle(tmp_path,['094'],opener)
    data=json.loads((tmp_path/'094.json').read_text())
    assert opener.calls == [('https://e.bambulab.com/query.php?lang=en&d=094',20)]
    assert data['sha256'] == hashlib.sha256(bundle.payload_bytes(data['response'])).hexdigest()
    assert datetime.fromisoformat(data['fetched_at']).tzinfo is not None
    assert index['lists']['094']['version'] == response()['ver']
    assert (tmp_path/'094.json').stat().st_mode & 0o777 == 0o644
    monkeypatch.setattr(fetcher,'MAX_BYTES',10)
    with pytest.raises(ValueError,match='size limit'): fetcher.fetch('094',opener)
    with pytest.raises(ValueError): fetcher.fetch('../',opener)
    with pytest.raises(ValueError): fetcher.NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.invalid')


def test_timeout_preserves_bundle(tmp_path):
    class Timeout:
        def open(self,*args,**kwargs): raise TimeoutError()
    with pytest.raises(TimeoutError): fetcher.write_bundle(tmp_path,['094'],Timeout())
    assert list(tmp_path.iterdir()) == []


def test_corrupt_bundle_falls_back(lists):
    path=lists/'094.json'; raw=json.loads(path.read_text()); raw['response']['data']['device_hms']['en'][0]['intro']='corrupt'
    path.write_text(json.dumps(raw))
    assert lookup_hms_code('05010400_00030004','094') == 'Printer code 0501-0400-0003-0004'


def test_runtime_never_opens_network(lists,monkeypatch):
    def forbidden(*args,**kwargs): raise AssertionError('runtime network')
    monkeypatch.setattr(fetcher.urllib.request.OpenerDirector,'open',forbidden)
    assert lookup_hms_code('05010400_00030004','094') == 'Clean and lubricate rails'
    assert lookup_hms_code('05010400_00030004','ZZZ').startswith('Printer code')


def test_serial_credentials_and_api_decode(lists,monkeypatch):
    from core import crypto
    from modules.printers.schemas import PrinterResponse
    monkeypatch.setattr(crypto,'decrypt', lambda _: '094SYNTHETIC|synthetic')
    row=SimpleNamespace(id=1,name='Synthetic',api_type='bambu',api_key='encrypted',camera_url=None,
                        api_host=None,model='H2D',machine_type='H2D',created_at=datetime.now(),updated_at=datetime.now(),
                        hms_errors=json.dumps([{'attr':0x05010400,'code':0x00030002}]),
                        print_stage='Paused: print_error=03004002',gcode_state='PAUSE')
    wire=PrinterResponse.model_validate(row).model_dump()
    assert json.loads(wire['hms_errors'])[0]['message']=='Lubricate threaded rods'
    assert wire['print_stage']=='Paused: Synthetic print action'
    assert 'hms_model_prefix' not in wire and 'api_key' not in wire
    assert 'SYNTHETIC' not in json.dumps(wire,default=str)


def test_later_fetch_failure_keeps_all_last_good_files(tmp_path):
    fetcher.write_bundle(tmp_path, ['094','00M'], Opener(json.dumps(response()).encode()))
    original = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    class FailsSecond(Opener):
        def open(self, request, timeout):
            if self.calls: raise TimeoutError()
            return super().open(request, timeout)
    with pytest.raises(TimeoutError):
        fetcher.write_bundle(tmp_path, ['094','00M'], FailsSecond(json.dumps(response()).encode()))
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == original


def test_release_prefixes_cover_spec_and_upstream_families():
    assert set(fetcher.PREFIXES) == {'00M','00W','03W','01S','01P','030','039','094','239','093','20P','22E','31B','26A'}


def test_runtime_missing_corrupt_and_wrong_prefix_fail_plain(lists):
    assert lookup_hms_code('05010400_00030002','ZZZ').startswith('Printer code ')
    (lists/'094.json').write_text('{')
    assert lookup_hms_code('05010400_00030002','094').startswith('Printer code ')
    bundle.load_bundle.cache_clear()
    fetcher.write_bundle(lists,['094'],Opener(json.dumps(response()).encode()))
    raw=json.loads((lists/'094.json').read_text());raw['prefix']='00M'
    (lists/'094.json').write_text(json.dumps(raw))
    assert lookup_hms_code('05010400_00030002','094').startswith('Printer code ')


def test_ledger_dto_preserves_model_lookup_without_exposing_context(lists,monkeypatch):
    from core import crypto
    from modules.printers.ledger_display import _to_printer_dto
    monkeypatch.setattr(crypto,'decrypt',lambda _: '094SYNTHETIC|synthetic')
    row=SimpleNamespace(id=1,name='Synthetic',api_type='bambu',api_key='encrypted',camera_url=None,
                        api_host=None,model='H2D',machine_type='H2D',created_at=datetime.now(),updated_at=datetime.now(),
                        hms_errors=json.dumps([{'attr':0x05010400,'code':0x00030002}]))
    wire=_to_printer_dto(row,None,False).model_dump()
    assert json.loads(wire['hms_errors'])[0]['message']=='Lubricate threaded rods'
    assert 'hms_model_prefix' not in wire and 'api_key' not in wire


def test_oversize_runtime_bundle_falls_back(lists,monkeypatch):
    monkeypatch.setattr(bundle,'MAX_BYTES',10)
    assert lookup_hms_code('05010400_00030002','094').startswith('Printer code ')


def test_pause_does_not_attribute_hidden_or_maintenance_notices(lists):
    from modules.printers.monitors.pause_cause import stage, display_stage
    monitor=SimpleNamespace(serial='22ESYNTHETIC',_state={'gcode_state':'PAUSE'},
                            _pause_active_hms=[{'attr':0x05000600,'code':0x00020070}])
    assert stage(monitor, '') == 'Paused: Cause unavailable'
    assert display_stage('Paused: hms=05000600_00020070','22E') == 'Paused: Cause unavailable'
    monitor.serial='094SYNTHETIC'
    monitor._pause_active_hms=[{'attr':0x05010400,'code':0x00030002}]
    assert stage(monitor, '') == 'Paused: Cause unavailable'
    assert display_stage('Paused: hms=05010400_00030002','094') == 'Paused: Cause unavailable'
