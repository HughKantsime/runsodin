"""Certification group C — Filament & Spool Inventory (F06) behavior evidence.

Real assembled inventory router (/api) + real schema; Spoolman is a real local HTTP
peer; printers are not contacted."""
import csv
import io
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_inventory_claim_acceptance import inventory_client
from tests.test_contracts.test_education_review_workflow import review_db
from core.dependencies import get_current_user

REPO = Path(__file__).resolve().parents[2]


def _create(client, **extra):
    body = {'filament_id': 990, 'initial_weight_g': 1000, **extra}
    response = client.post('/api/spools', json=body)
    assert response.status_code == 200, response.text
    return response.json()['id']


# ---------------------------------------------------------- F06-6.1-01

def test_spool_tracks_material_color_brand_batch_weights_cost_and_statuses(inventory_client, review_db):
    review_db.execute(text("UPDATE filament_library SET color_hex='1A2B3C' WHERE id=990"))
    review_db.commit()
    sid = _create(inventory_client, initial_weight_g=750, lot_number='CERT-LOT-7', price=18.75)
    row = inventory_client.get(f'/api/spools/{sid}').json()
    assert (row['filament_material'], row['filament_brand'], row['filament_color_hex'], row['lot_number']) == ('PETG', 'Synthetic', '1A2B3C', 'CERT-LOT-7')
    assert (row['initial_weight_g'], row['remaining_weight_g'], row['price'], row['status']) == (750, 750, 18.75, 'active')
    # Status transitions: consuming to zero marks the spool empty; delete archives it.
    used = inventory_client.post(f'/api/spools/{sid}/use', json={'weight_used_g': 750})
    assert used.status_code == 200, used.text
    assert inventory_client.get(f'/api/spools/{sid}').json()['status'] == 'empty'
    assert inventory_client.patch(f'/api/spools/{sid}', json={'status': 'active', 'remaining_weight_g': 10}).status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['status'] == 'active'
    assert inventory_client.delete(f'/api/spools/{sid}').status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['status'] == 'archived'
    assert {r['id'] for r in inventory_client.get('/api/spools', params={'status': 'archived'}).json()} == {sid}


def test_filament_cost_per_gram_is_recorded_and_readable(inventory_client):
    # cost_per_gram is one install-wide rate: superadmin-only write (review HIGH-2).
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None}
    created = inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Cert ASA', 'material': 'ASA', 'cost_per_gram': 0.031})
    assert created.status_code == 200, created.text
    identity = created.json()['id']
    assert inventory_client.patch(f'/api/filaments/lib_{identity}', json={'cost_per_gram': 0.034}).status_code == 200
    assert inventory_client.get(f'/api/filaments/lib_{identity}').json().get('cost_per_gram') == 0.034


# ---------------------------------------------------------- F06-6.1-03

@pytest.mark.parametrize('role,allowed', [('viewer', False), ('operator', True), ('admin', True)])
def test_edit_and_delete_require_operator_or_admin(inventory_client, role, allowed):
    sid = _create(inventory_client)
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': role, 'group_id': 1}
    edit = inventory_client.patch(f'/api/spools/{sid}', json={'notes': 'cert edit'})
    archive = inventory_client.delete(f'/api/spools/{sid}')
    assert (edit.status_code, archive.status_code) == ((200, 200) if allowed else (403, 403))
    row = inventory_client.get(f'/api/spools/{sid}').json()
    assert (row['notes'], row['status']) == (('cert edit', 'archived') if allowed else (None, 'active'))


def test_bulk_delete_permanently_removes_archived_spools_only(inventory_client):
    archived, active = _create(inventory_client), _create(inventory_client)
    inventory_client.delete(f'/api/spools/{archived}')
    response = inventory_client.post('/api/spools/bulk-update', json={'spool_ids': [archived, active], 'action': 'delete'})
    assert response.status_code == 200, response.text
    assert inventory_client.get(f'/api/spools/{archived}').status_code == 404
    assert inventory_client.get(f'/api/spools/{active}').json()['status'] == 'active'


# ---------------------------------------------------------- F06-6.2-01/03/04

def test_qr_codes_unique_and_lookup_resolves_scanned_code(inventory_client, review_db):
    ids = [_create(inventory_client) for _ in range(5)]
    codes = [inventory_client.get(f'/api/spools/{i}').json()['qr_code'] for i in ids]
    assert len(set(codes)) == 5 and all(c.startswith('SPL-') for c in codes)
    found = inventory_client.get(f'/api/spools/lookup/{codes[2]}')
    assert found.status_code == 200 and found.json()['id'] == ids[2] and found.json()['material'] == 'PETG'
    assert inventory_client.get('/api/spools/lookup/SPL-NOTREAL').status_code == 404
    # Uniqueness is enforced by the schema, not only by random generation.
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        review_db.execute(text("INSERT INTO spools(filament_id,qr_code) VALUES(990,:code)"), {'code': codes[0]})
    review_db.rollback()


def test_scan_assign_associates_spool_with_printer_slot(inventory_client, review_db):
    sid = _create(inventory_client)
    code = inventory_client.get(f'/api/spools/{sid}').json()['qr_code']
    response = inventory_client.post('/api/spools/scan-assign', json={'qr_code': code, 'printer_id': 9, 'slot': 2})
    assert response.status_code == 200 and response.json()['success'] is True, response.text
    assert (response.json()['printer_name'], response.json()['slot']) == ('P1S-09', 2)
    spool = inventory_client.get(f'/api/spools/{sid}').json()
    assert (spool['location_printer_id'], spool['location_slot']) == (9, 2)
    slot = review_db.execute(text('SELECT assigned_spool_id,spool_confirmed,filament_type FROM filament_slots WHERE printer_id=9 AND slot_number=2')).one()
    assert (slot[0], bool(slot[1]), slot[2]) == (sid, True, 'PETG')
    invalid = inventory_client.post('/api/spools/scan-assign', json={'qr_code': code, 'printer_id': 9, 'slot': 99}).json()
    assert invalid['success'] is False and 'Invalid slot' in invalid['message']


# ---------------------------------------------------------- F06-6.4-01 (low stock)

def test_low_stock_threshold_defaults_to_50g_and_is_configurable(inventory_client):
    sid = _create(inventory_client, initial_weight_g=49)
    row = inventory_client.get(f'/api/spools/{sid}').json()
    assert row['low_stock_threshold_g'] == 50 and row['is_low_stock'] is True
    assert inventory_client.patch(f'/api/spools/{sid}', json={'low_stock_threshold_g': 40}).status_code == 200
    row = inventory_client.get(f'/api/spools/{sid}').json()
    assert row['low_stock_threshold_g'] == 40 and row['is_low_stock'] is False
    assert sid not in {r['id'] for r in inventory_client.get('/api/spools/low-stock').json()}


def test_capture_wire_fixture_for_spools_ui(inventory_client):
    low = _create(inventory_client, initial_weight_g=30, pa_profile='K0.042')
    normal = _create(inventory_client, initial_weight_g=800, low_stock_threshold_g=100)
    archived = _create(inventory_client, initial_weight_g=500)
    inventory_client.delete(f'/api/spools/{archived}')
    rows = inventory_client.get('/api/spools').json()
    for index, row in enumerate(sorted(rows, key=lambda r: r['id']), start=1):
        row['created_at'] = '2026-10-14T12:00:00'
        row['qr_code'] = f'SPL-CERT{index:04d}'  # generated codes are random; normalise for a stable fixture
    fixture = {'scope': 'Owned real GET /api/spools response (cert C)', 'spools': rows,
               'low_stock': inventory_client.get('/api/spools/low-stock').json()}
    (REPO / 'frontend/src/test-fixtures/cert-C-spools-wire.json').write_text(json.dumps(fixture, indent=2) + '\n')
    assert {r['id']: r['is_low_stock'] for r in rows} == {low: True, normal: False, archived: False}


# ---------------------------------------------------------- F06-6.5-02

def test_csv_export_exact_columns(inventory_client):
    sid = _create(inventory_client, lot_number='CERT-LOT', pa_profile='K0.03')
    reader = csv.DictReader(io.StringIO(inventory_client.get('/api/spools/export').text))
    assert reader.fieldnames == ['ID', 'Brand', 'Name', 'Material', 'Color', 'Initial Weight (g)', 'Remaining Weight (g)',
                                 '% Remaining', 'Status', 'Vendor', 'Price', 'Storage Location', 'Notes', 'PA Profile']
    row = next(r for r in reader if r['ID'] == str(sid))
    assert (row['Material'], row['Brand'], row['Status'], row['PA Profile']) == ('PETG', 'Synthetic', 'active', 'K0.03')
    # Batch/lot, QR code, spool tare and cost per gram are not exported.
    assert 'CERT-LOT' not in json.dumps(row)


# ---------------------------------------------------------- F06-6.6-01/02

def test_spool_can_be_linked_to_spoolman_spool(inventory_client, review_db):
    sid = _create(inventory_client, spoolman_spool_id=42)
    linked = review_db.execute(text('SELECT spoolman_spool_id FROM spools WHERE id=:id'), {'id': sid}).scalar_one()
    if linked != 42:
        inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': 42})
    review_db.expire_all()
    assert review_db.execute(text('SELECT spoolman_spool_id FROM spools WHERE id=:id'), {'id': sid}).scalar_one() == 42


@pytest.fixture
def spoolman_server(monkeypatch):
    from core.config import settings
    inventory = [{'id': 7, 'remaining_weight': 640.5, 'filament': {'name': 'Cert PLA', 'material': 'PLA', 'color_hex': 'FF0000'}},
                 {'id': 8, 'remaining_weight': 120, 'filament': {'name': 'Cert PETG', 'material': 'PETG', 'color_hex': '000000'}}]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(inventory if self.path == '/api/v1/spool' else []).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, 'spoolman_url', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.delenv('ODIN_ITAR_MODE', raising=False)
    try:
        yield inventory
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_spoolman_pull_is_read_through_and_sync_imports_nothing(inventory_client, spoolman_server, review_db):
    before = review_db.execute(text('SELECT COUNT(*) FROM spools')).scalar_one()
    listed = inventory_client.get('/api/spoolman/spools')
    assert listed.status_code == 200, listed.text
    assert [(s['id'], s['filament_type'], s['remaining_weight']) for s in listed.json()] == [(7, 'PLA', 640.5), (8, 'PETG', 120)]
    synced = inventory_client.post('/api/spoolman/sync')
    assert synced.status_code == 200 and synced.json()['spools_found'] == 2 and synced.json()['slots_updated'] == 0
    assert review_db.execute(text('SELECT COUNT(*) FROM spools')).scalar_one() == before


# ---------------------------------------------------------- F06-6.6-04 (Bambu remain %)

def test_bambu_completion_measures_ams_remain_delta_times_initial_weight(review_db, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.printers.monitors import mqtt_job_lifecycle
    engine = review_db.get_bind()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(7801,9,'Cert AMS','running',CURRENT_TIMESTAMP)"))

    @contextmanager
    def db():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', db)
    monkeypatch.setattr(mqtt_job_lifecycle, '_education_policy', lambda: None)
    start = {700: {'remain_pct': 80, 'initial_weight_g': 1000, 'slot': 1}, 701: {'remain_pct': 50, 'initial_weight_g': 500, 'slot': 2}}
    mqtt_job_lifecycle.record_job_ended(9, 'Cert', 7801, None, 'completed', {'_ams_remain': {1: 70, 2: 46}, 'subtask_name': 'Cert AMS'}, start, lambda **kw: None)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=7801')).scalar_one() == pytest.approx(120.0)


def test_v1_5_10_changelog_records_both_measurement_methods():
    entry = (REPO / 'CHANGELOG.md').read_text().split('## [1.5.10]')[1].split('\n## ')[0]
    assert 'AMS remain percentage delta × initial_weight_g' in entry
    assert '`print_stats.filament_used` (mm→grams conversion)' in entry


from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case


def test_multi_filament_sliced_usage_is_distributed_to_every_assigned_spool(archive_case):
    archive, engine = archive_case
    assert archive.create_print_archive(7099, 9, True) is not None
    with engine.connect() as conn:
        assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all() == [960, 960]
        consumed = json.loads(conn.execute(text('SELECT consumption_json FROM print_archives WHERE print_job_id=7099')).scalar_one())
        assert sorted(c['spool_id'] for c in consumed) == [7099, 7100]


# Decision 2026-10-15 (cert-fix-C, F06-6.6-04 -> claim mismatch): a single measured total from a
# multi-spool print is never split by guesswork. The archive stays 'unresolved' with
# measured_material_distribution_unknown and no spool is deducted; per-spool deduction for
# multi-filament prints uses the sliced per-slot estimates (test above).
def test_measured_multi_filament_total_is_refused_not_distributed(archive_case):
    archive, engine = archive_case
    with engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET filament_used_g=120 WHERE id=7099'))
    assert archive.create_print_archive(7099, 9, True) is not None
    with engine.connect() as conn:
        state = tuple(conn.execute(text('SELECT accounting_state,accounting_reason FROM print_archives WHERE print_job_id=7099')).one())
        remaining = conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()
    assert state == ('unresolved', 'measured_material_distribution_unknown')
    assert remaining == [1000, 1000]


# ------------------------------------------- regression: cert-fix-C inventory fixes

def test_filament_cost_per_gram_lists_clears_and_rejects_negative(inventory_client):
    # cost_per_gram is one install-wide rate: superadmin-only write (review HIGH-2).
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None}
    created = inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Cert PLA rate', 'material': 'PLA', 'cost_per_gram': 0.02})
    assert created.status_code == 200 and created.json()['cost_per_gram'] == 0.02
    identity = f"lib_{created.json()['id']}"
    assert next(f for f in inventory_client.get('/api/filaments').json() if f['id'] == identity)['cost_per_gram'] == 0.02
    assert inventory_client.patch(f'/api/filaments/{identity}', json={'cost_per_gram': -1}).status_code == 422
    assert inventory_client.patch(f'/api/filaments/{identity}', json={'cost_per_gram': None}).status_code == 200
    assert inventory_client.get(f'/api/filaments/{identity}').json()['cost_per_gram'] is None
    # Unrelated edits leave the rate untouched.
    inventory_client.patch(f'/api/filaments/{identity}', json={'cost_per_gram': 0.025})
    assert inventory_client.patch(f'/api/filaments/{identity}', json={'name': 'Cert PLA rate v2'}).status_code == 200
    assert inventory_client.get(f'/api/filaments/{identity}').json()['cost_per_gram'] == 0.025


def test_spoolman_link_is_readable_editable_and_unlinkable(inventory_client):
    sid = _create(inventory_client, spoolman_spool_id=42)
    assert inventory_client.get(f'/api/spools/{sid}').json()['spoolman_spool_id'] == 42
    assert next(s for s in inventory_client.get('/api/spools').json() if s['id'] == sid)['spoolman_spool_id'] == 42
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': 0}).status_code == 422
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': 77}).status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['spoolman_spool_id'] == 77
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': None}).status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['spoolman_spool_id'] is None
    assert inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': -3}).status_code == 422


@pytest.mark.parametrize('principal', [{'id': 3, 'role': 'operator', 'group_id': None}, {'id': 4, 'role': 'admin', 'group_id': 1}])
def test_global_filament_rate_write_requires_superadmin(inventory_client, principal):
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None}
    identity = f"lib_{inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Rate', 'material': 'PLA', 'cost_per_gram': 0.02}).json()['id']}"
    inventory_client.app.dependency_overrides[get_current_user] = lambda: principal
    assert inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Rated', 'material': 'PLA', 'cost_per_gram': 0.5}).status_code == 403
    assert inventory_client.patch(f'/api/filaments/{identity}', json={'cost_per_gram': 0.9}).status_code == 403
    assert inventory_client.patch(f'/api/filaments/{identity}', json={'cost_per_gram': None}).status_code == 403
    # The edit modal resends the unchanged rate with other edits; that is not a rate write.
    assert inventory_client.patch(f'/api/filaments/{identity}', json={'name': 'Rate v2', 'cost_per_gram': 0.02}).status_code == 200
    assert inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Unrated', 'material': 'PLA', 'cost_per_gram': None}).status_code == 200
    assert inventory_client.get(f'/api/filaments/{identity}').json()['cost_per_gram'] == 0.02


@pytest.mark.parametrize('raw', ['Infinity', '-Infinity', 'NaN', '1e6', '100.01'])
def test_filament_rate_rejects_non_finite_and_out_of_range(review_db, raw):
    # Real inventory router + the production 422 envelope function (core.app registers it).
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.errors import request_validation_response
    from modules.inventory.routes import router as inventory_router
    app = FastAPI()
    app.include_router(inventory_router, prefix='/api')
    app.add_exception_handler(RequestValidationError, lambda request, exc: request_validation_response(exc))
    app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None}
    app.dependency_overrides[get_db] = lambda: review_db
    with TestClient(app) as client:
        body = '{"brand":"Cert","name":"Bad","material":"PLA","cost_per_gram":%s}' % raw
        rejected = client.post('/api/filaments', content=body, headers={'Content-Type': 'application/json'})
        assert rejected.status_code == 422 and rejected.json()['error']['fields'] == ['cost_per_gram']
        identity = f"lib_{client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Ok', 'material': 'PLA', 'cost_per_gram': 100}).json()['id']}"
        patch = client.patch(f'/api/filaments/{identity}', content='{"cost_per_gram":%s}' % raw, headers={'Content-Type': 'application/json'})
        assert patch.status_code == 422
        assert client.get(f'/api/filaments/{identity}').json()['cost_per_gram'] == 100


# ------------------------------------------- review MEDIUM-5 / item 10: Spoolman link uniqueness

def test_spoolman_spool_links_to_one_odin_spool_install_wide(inventory_client, review_db):
    first = _create(inventory_client, spoolman_spool_id=42)
    duplicate = inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': 42})
    assert duplicate.status_code == 409
    second = _create(inventory_client)
    assert inventory_client.patch(f'/api/spools/{second}', json={'spoolman_spool_id': 42}).status_code == 409
    assert inventory_client.patch(f'/api/spools/{first}', json={'spoolman_spool_id': 42, 'notes': 'same link'}).status_code == 200
    assert inventory_client.patch(f'/api/spools/{first}', json={'spoolman_spool_id': None}).status_code == 200
    assert inventory_client.patch(f'/api/spools/{second}', json={'spoolman_spool_id': 42}).status_code == 200
    # Install-wide: a spool of another org cannot take the link either.
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 5, 'role': 'operator', 'group_id': 2}
    third = _create(inventory_client)
    assert inventory_client.patch(f'/api/spools/{third}', json={'spoolman_spool_id': 42}).status_code == 409
    assert inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': 42}).status_code == 409


def test_spoolman_link_race_is_mapped_to_409_by_the_unique_index(inventory_client, review_db):
    """With migration 030's index in place, a writer that passes the pre-check still gets 409."""
    with review_db.get_bind().begin() as connection:
        _migration().apply(connection)
    first, second = _create(inventory_client, spoolman_spool_id=42), _create(inventory_client)
    from modules.inventory.routes import spools as spool_routes
    original = spool_routes._refuse_taken_spoolman_link
    spool_routes._refuse_taken_spoolman_link = lambda *args, **kwargs: None
    try:
        assert inventory_client.patch(f'/api/spools/{second}', json={'spoolman_spool_id': 42}).status_code == 409
    finally:
        spool_routes._refuse_taken_spoolman_link = original
    assert inventory_client.get(f'/api/spools/{second}').json()['spoolman_spool_id'] is None


@pytest.mark.parametrize('value', [2_147_483_648, 10**12])
def test_spoolman_link_is_bounded_to_int32(inventory_client, value):
    sid = _create(inventory_client)
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': value}).status_code == 422
    assert inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': value}).status_code == 422
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': 2_147_483_647}).status_code == 200


def _migration():
    import importlib
    return importlib.import_module('core.schema.migrations.030_spoolman_link_uniqueness')


def test_migration_030_is_additive_idempotent_and_unregistered(tmp_path):
    from sqlalchemy import create_engine
    from core.schema import bootstrap_database
    from core.schema.migrator import _ALLOWED_PYTHON_MIGRATIONS
    assert 'core.schema.migrations.030_spoolman_link_uniqueness' not in _ALLOWED_PYTHON_MIGRATIONS
    engine = create_engine('sqlite:///' + str(tmp_path / 'spoolman-link.db'))
    try:
        assert _migration().MIGRATION_ID not in bootstrap_database(engine)['applied']
        with engine.begin() as connection:
            _migration().apply(connection)
            _migration().apply(connection)
            _migration().validate(connection)
            connection.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Cert','Dup','PLA')"))
            connection.execute(text("INSERT INTO spools(id,filament_id,spoolman_spool_id,status) VALUES(1,1,NULL,'active'),(2,1,NULL,'active'),(3,1,9,'active')"))
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO spools(id,filament_id,spoolman_spool_id,status) VALUES(4,1,9,'active')"))
    finally:
        engine.dispose()


def test_migration_030_refuses_existing_duplicates_and_wrong_shape(tmp_path):
    from sqlalchemy import create_engine
    from core.schema import bootstrap_database
    from core.schema.migrator import MigrationError
    engine = create_engine('sqlite:///' + str(tmp_path / 'spoolman-dupes.db'))
    try:
        bootstrap_database(engine)
        migration = _migration()
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Cert','Dup','PLA')"))
            connection.execute(text("INSERT INTO spools(id,filament_id,spoolman_spool_id,status) VALUES(1,1,7,'active'),(2,1,7,'active'),(3,1,NULL,'active'),(4,1,NULL,'active')"))
        with engine.connect() as connection:
            with pytest.raises(MigrationError, match='unlink all but one.*7'):
                migration.apply(connection)
            connection.rollback()
        with engine.begin() as connection:
            connection.execute(text('UPDATE spools SET spoolman_spool_id=NULL WHERE id=2'))
            connection.execute(text(f'CREATE UNIQUE INDEX {migration.INDEX} ON spools (spoolman_spool_id)'))
            with pytest.raises(MigrationError, match='shape mismatch'):
                migration.validate(connection)
            connection.execute(text(f'DROP INDEX {migration.INDEX}'))
            migration.apply(connection)
            migration.validate(connection)
    finally:
        engine.dispose()


@pytest.mark.xfail(strict=True, reason="pending post-027 migration registration")
def test_runtime_bootstrap_enforces_spoolman_link_uniqueness(tmp_path):
    """Once 030 is registered, an ordinarily bootstrapped database rejects a duplicate link itself."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from core.schema import bootstrap_database
    engine = create_engine('sqlite:///' + str(tmp_path / 'runtime-link.db'))
    try:
        assert _migration().MIGRATION_ID in bootstrap_database(engine)['applied']
        with engine.begin() as connection:
            _migration().validate(connection)
            connection.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Cert','Dup','PLA')"))
            connection.execute(text("INSERT INTO spools(id,filament_id,spoolman_spool_id,status) VALUES(1,1,9,'active')"))
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO spools(id,filament_id,spoolman_spool_id,status) VALUES(2,1,9,'active')"))
    finally:
        engine.dispose()
