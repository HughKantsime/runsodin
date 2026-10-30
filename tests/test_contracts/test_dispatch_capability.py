"""No hardware: supplied SubLab file 10/11 evidence and exact nozzle boundaries."""
import importlib
import json
import zipfile
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, text
from modules.printers.nozzle_capability import installed_nozzles, compare_nozzles
from modules.printers.dispatch_capability_payload import inspect_payload, geometry_refusal
from modules.printers.dispatch_capability_gate import create_capability_gate

REPORT = {'nozzle_type': 'HH05', 'nozzle_diameter': .4, 'device': {'nozzle': {
    'exist': 3, 'info': [{'id': i, 'type': 'HH05', 'diameter': .4, 'stat': 1, 'wear': 0.0} for i in (0, 1)]}}}


def payload(tmp_path, volume='Standard', physical=('1', '0'), box=(0, 0, 325, 320)):
    path = tmp_path / 'contract.3mf'
    settings = {'printer_model': 'Bambu Lab H2D', 'nozzle_diameter': ['0.4', '0.4'],
                'nozzle_volume_type': [volume, volume], 'nozzle_type': ['hardened_steel'] * 2,
                'physical_extruder_map': list(physical), 'filament_nozzle_map': ['1', '1'],
                'extruder_nozzle_stats': ['Standard#1'] * 2}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Metadata/project_settings.config', json.dumps(settings))
        archive.writestr('Metadata/plate_1.json', json.dumps({'bbox_all': box}))
        archive.writestr('Metadata/slice_info.config', f'<config><plate><metadata key="index" value="1"/>'
            f'<filament id="1" used_g="12" nozzle_diameter="0.40" volume_type="{volume}"/></plate></config>')
    return path


def gate(tmp_path, monkeypatch, volume='Standard', report=REPORT, previous=None):
    from modules.printers import dispatch_acquisition
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: report)
    return create_capability_gate(job={'id': 89, 'printer_id': 5}, creds={'machine_type': 'H2D'},
        staged_path=payload(tmp_path, volume), plate_number=1,
        target_handle=SimpleNamespace(held=lambda: True), previous=previous)


def test_file_10_refuses_left_standard_against_high_flow(tmp_path, monkeypatch):
    checked = gate(tmp_path, monkeypatch)
    assert checked.prepare() is False
    assert checked.refusal.code == 'nozzle_mismatch'
    assert checked.refusal.detail.startswith('left nozzle requires Standard; installed High Flow; report_read_ms=')
    assert checked() is False


def test_file_11_passes_single_use(tmp_path, monkeypatch):
    checked = gate(tmp_path, monkeypatch, 'High Flow')
    assert checked.prepare() is True
    assert checked() is True
    assert checked() is False


def test_diameter_and_stainless_hardness_refuse(tmp_path):
    facts = inspect_payload(payload(tmp_path, 'High Flow'), 1, 'H2D')['requirements']
    installed = installed_nozzles(REPORT)
    assert installed[1]['material'] == 'Tungsten carbide'
    installed[1]['diameter'] = .6
    assert '0.4 mm; installed 0.6 mm' in compare_nozzles(facts, installed)[0]
    installed[1].update(diameter=.4, material='Stainless steel')
    facts[0]['hrc'] = 50
    assert 'requires hardened' in compare_nozzles(facts, installed)[0]


@pytest.mark.parametrize('missing', ['file', 'printer'])
def test_missing_metadata_allows_with_timeline_note(tmp_path, monkeypatch, missing):
    from core import job_timeline
    notes = []
    monkeypatch.setattr(job_timeline, 'enqueue', notes.append)
    checked = gate(tmp_path, monkeypatch, 'High Flow', {} if missing == 'printer' else REPORT)
    if missing == 'file':
        path = tmp_path / 'empty.3mf'
        with zipfile.ZipFile(path, 'w') as archive:archive.writestr('Metadata/plate_1.gcode', 'G90')
        checked = create_capability_gate(job={'id': 89, 'printer_id': 5}, creds={}, staged_path=path,
            plate_number=1, target_handle=SimpleNamespace(held=lambda: True))
    assert checked() is True
    assert [note['kind'] for note in notes] == ['nozzle_unverified']


@pytest.mark.parametrize('model', ['X1C', 'P1S'])
def test_single_nozzle_top_level_fields(model):
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    packet = BambuPrintSection.model_validate({'nozzle_type':'hardened_steel','nozzle_diameter':.4})
    nozzle, = installed_nozzles(packet.model_dump(exclude_unset=True))
    assert (nozzle['diameter'], nozzle['material'], nozzle['flow']) == (.4, 'Hardened steel', None)


@pytest.mark.parametrize('physical,box,refused', [
    (('1','0'), [0,0,325,320], False), (('1','0'), [0,0,325.01,320], True),
    (('0','1'), [25,0,350,320], False), (('0','1'), [24.99,0,350,320], True),
    (('1','0'), [-.01,0,100,100], True), (('1','0'), [0,0,300,320.01], True),
])
def test_h2d_geometry_boundaries(tmp_path, physical, box, refused):
    facts = inspect_payload(payload(tmp_path, 'High Flow', physical, box), 1, 'H2D')
    assert bool(geometry_refusal(facts, {'machine_type': 'H2D'})) is refused


def test_geometry_uses_same_named_gate_reason(tmp_path, monkeypatch):
    from modules.printers import dispatch_acquisition
    monkeypatch.setattr(dispatch_acquisition,'bounded_bambu_report',lambda *a:pytest.fail('Geometry should refuse first'))
    for box in ([0,0,326,320], [0,0,351,320]):
        checked = create_capability_gate(job={'id':89,'printer_id':5},creds={'model':'H2D'},
            staged_path=payload(tmp_path,'High Flow',box=box),plate_number=1,target_handle=SimpleNamespace(held=lambda:True))
        assert checked.prepare() is False and checked.refusal.code == 'bed_geometry_mismatch'


def test_migration_changes_only_known_wrong_pair(tmp_path):
    migration = importlib.import_module('core.schema.migrations.042_dispatch_capability')
    engine = create_engine(f'sqlite:///{tmp_path}/migration.db')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY,api_type TEXT,machine_type TEXT,model TEXT,bed_x_mm FLOAT,bed_y_mm FLOAT)'))
        conn.execute(text('CREATE TABLE audit_logs(action TEXT,entity_type TEXT,entity_id INTEGER,details TEXT)'))
        for index, (model, x, y, protocol) in enumerate([
            ('H2D',320,320,'bambu'), ('O1E',320,320,'bambu'), ('H2D',321,320,'bambu'),
            ('H2D',320,319,'bambu'), ('P1S',320,320,'bambu'), ('H2D',320,320,'moonraker')]):
            conn.execute(text('INSERT INTO printers VALUES(:id,:api,:model,NULL,:x,:y)'),dict(id=index,api=protocol,model=model,x=x,y=y))
        migration.apply(conn)
        assert conn.execute(text('SELECT bed_x_mm FROM printers ORDER BY id')).scalars().all() == [350,350,321,320,320,320]
        migration.apply(conn)  # idempotent
        assert conn.execute(text('SELECT entity_id FROM audit_logs ORDER BY entity_id')).scalars().all() == [0, 1]
    engine.dispose()


def test_defaults_cover_all_listed_bambu_models():
    from modules.printers.printer_models import BAMBU_BEDS, BAMBU_MODEL_IDS
    from modules.printers.schemas import PrinterCreate
    assert set(BAMBU_BEDS) == set(BAMBU_MODEL_IDS.values())
    for model, dims in BAMBU_BEDS.items():
        printer = PrinterCreate(name='Synthetic', api_type='bambu', model=model)
        assert (printer.bed_x_mm, printer.bed_y_mm) == dims
    assert PrinterCreate(name='Custom',api_type='bambu',model='H2D',bed_x_mm=311,bed_y_mm=299).bed_x_mm == 311


@pytest.mark.parametrize('volume,expected', [('Standard', False), ('High Flow', True)])
def test_post_upload_bambu_transport_never_sends_start_on_mismatch(tmp_path, monkeypatch, volume, expected):
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import ftp_upload, session, broker_policy
    calls = []
    checked = gate(tmp_path,monkeypatch,volume)
    monkeypatch.setattr(dispatch,'_ws',lambda *a:None)
    monkeypatch.setattr(ftp_upload,'upload_file',lambda *a:calls.append('upload') or True)
    monkeypatch.setattr(broker_policy,'resolve_bambu_broker_config',lambda *a,**k:SimpleNamespace(host='test.invalid',port=8883,use_tls=True))
    def run(*args, **kwargs):
        assert kwargs['before_command']() is True
        calls.append('START')
        return True
    monkeypatch.setattr(session,'run_command',run)
    success, message = dispatch._dispatch_bambu_v2(89,str(tmp_path/'contract.3mf'),'safe.3mf',
        {'ip':'test.invalid','serial':'SYNTHETIC','access_code':'synthetic'},before_start=checked)
    assert success is expected
    assert calls == (['upload','START'] if expected else ['upload'])
    if not expected:assert 'nozzle_mismatch' in message and 'left' in message


def test_ams_refusal_precedes_nozzle_read_and_pass_rechecks_at_start(tmp_path, monkeypatch):
    from modules.printers import dispatch_acquisition
    from modules.printers.dispatch_ams_gate import GateRefusal
    order = []
    def previous():
        order.append('ams-final')
        return True
    previous.prepare = lambda **kw: order.append('ams-prepare') or True
    previous.refusal = None
    checked = gate(tmp_path, monkeypatch, 'High Flow', previous=previous)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: order.append('nozzles') or REPORT)
    assert checked.prepare() and checked()
    assert order == ['nozzles', 'ams-prepare', 'ams-final']
    previous.prepare = lambda **kw: False
    previous.refusal = GateRefusal('material_mismatch','Reported material=PLA; required material=PETG')
    checked = gate(tmp_path, monkeypatch, 'High Flow', previous=previous)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: REPORT)
    assert not checked.prepare() and checked.refusal.code == 'material_mismatch'


def test_reported_absent_nozzle_is_a_positive_mismatch(tmp_path, monkeypatch):
    checked = gate(tmp_path,monkeypatch,'High Flow',{'device':{'nozzle':{'exist':0,'info':[]}}})
    assert not checked.prepare()
    assert checked.refusal.code == 'nozzle_mismatch' and 'left' in checked.refusal.detail


def test_fresh_report_cannot_be_renewed_while_waiting_for_start(tmp_path, monkeypatch):
    from modules.printers import dispatch_capability_gate
    checked = gate(tmp_path, monkeypatch, 'High Flow')
    clock = [100.0]
    monkeypatch.setattr(dispatch_capability_gate.time,'monotonic',lambda:clock[0])
    assert checked.prepare()
    clock[0] = 110.0
    assert not checked() and checked.refusal.code == 'snapshot_stale'


def test_unused_extruder_mismatch_does_not_block_file_11(tmp_path):
    facts=inspect_payload(payload(tmp_path,'High Flow'),1,'H2D')
    installed=installed_nozzles(REPORT)
    installed[0].update(diameter=.8,flow='Standard',material='Stainless steel')
    assert compare_nozzles(facts['requirements'],installed) == (None,False)


def test_selected_plate_uses_only_its_filaments(tmp_path):
    path=payload(tmp_path,'High Flow')
    with zipfile.ZipFile(path,'a') as archive:
        archive.writestr('Metadata/plate_2.json',json.dumps({'bbox_all':[25,0,350,320]}))
    # No slice-info evidence for plate 2 must not inherit plate 1's left nozzle.
    facts=inspect_payload(path,2,'H2D')
    assert facts['requirements'] == [] and compare_nozzles(facts['requirements'],installed_nozzles(REPORT))[1]
