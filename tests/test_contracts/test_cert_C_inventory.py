"""Certification group C — Filament & Spool Inventory (F06) behavior evidence.

Real assembled inventory router (/api) + real schema; Spoolman is a real local HTTP
peer; printers are not contacted."""
import csv
import io
import json
import threading
import time
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


@pytest.mark.parametrize('value', [2_147_483_648, 10**12])
def test_spoolman_link_is_bounded_to_int32(inventory_client, value):
    sid = _create(inventory_client)
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': value}).status_code == 422
    assert inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': value}).status_code == 422
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spoolman_spool_id': 2_147_483_647}).status_code == 200


def test_taken_link_409_names_the_holding_spool(inventory_client):
    holder = _create(inventory_client, spoolman_spool_id=51)
    qr = inventory_client.get(f'/api/spools/{holder}').json()['qr_code']
    refused = inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': 51})
    assert refused.status_code == 409 and f'#{holder}' in refused.json()['detail'] and qr in refused.json()['detail']
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 5, 'role': 'operator', 'group_id': 2}
    foreign = inventory_client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': 51})
    assert foreign.status_code == 409 and 'another organization' in foreign.json()['detail'] and qr not in foreign.json()['detail']


def test_existing_duplicate_links_are_kept_editable_and_reported_to_admins(inventory_client, review_db):
    """1.9.x data may already hold duplicates: never rewritten, other edits keep working, admins are warned."""
    a, b, c = _create(inventory_client), _create(inventory_client), _create(inventory_client, spoolman_spool_id=99)
    review_db.execute(text('UPDATE spools SET spoolman_spool_id=77 WHERE id IN (:a,:b)'), {'a': a, 'b': b})
    review_db.commit()
    assert inventory_client.patch(f'/api/spools/{a}', json={'notes': 'still editable'}).status_code == 200
    assert inventory_client.patch(f'/api/spools/{a}', json={'spoolman_spool_id': 77, 'notes': 'unchanged link'}).status_code == 200
    assert inventory_client.patch(f'/api/spools/{a}', json={'spoolman_spool_id': 99}).status_code == 409
    rows = review_db.execute(text('SELECT id, spoolman_spool_id FROM spools WHERE id IN (:a,:b,:c) ORDER BY id'), {'a': a, 'b': b, 'c': c}).all()
    assert [tuple(r) for r in rows] == [(a, 77), (b, 77), (c, 99)]
    assert inventory_client.get('/api/spools/spoolman-link-conflicts').status_code == 403
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None}
    conflicts = inventory_client.get('/api/spools/spoolman-link-conflicts')
    assert conflicts.status_code == 200
    assert [(item['spoolman_spool_id'], sorted(s['id'] for s in item['spools'])) for item in conflicts.json()] == [(77, sorted([a, b]))]
    from modules.system.diagnostics import _spoolman_link_conflicts
    from unittest import mock
    from sqlalchemy.orm import sessionmaker
    with mock.patch('core.db.SessionLocal', sessionmaker(bind=review_db.get_bind())):
        assert [item['spoolman_spool_id'] for item in _spoolman_link_conflicts({'id': 1, 'role': 'admin', 'group_id': None})] == [77]


@pytest.fixture
def concurrent_inventory(inventory_client, review_db, monkeypatch):
    """Real inventory router over a file SQLite database with one session per request."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core.db import get_db
    from modules.inventory.routes import router as inventory_router
    from modules.inventory.routes import spools as spool_routes
    from sqlalchemy import create_engine, event
    engine = create_engine(review_db.get_bind().url, connect_args={'timeout': 15})

    @event.listens_for(engine, 'connect')
    def _pragmas(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    app = FastAPI()
    app.include_router(inventory_router, prefix='/api')
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'operator', 'group_id': 1}

    def database():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db] = database
    original = spool_routes._refuse_taken_spoolman_link

    def slow_check(*args, **kwargs):
        original(*args, **kwargs)
        time.sleep(0.4)  # widen the check-then-write window
    monkeypatch.setattr(spool_routes, '_refuse_taken_spoolman_link', slow_check)
    yield app, engine
    engine.dispose()


def _race(app, requests):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    barrier = threading.Barrier(len(requests))

    def send(request):
        method, url, body = request
        client = TestClient(app)
        barrier.wait()
        return getattr(client, method)(url, json=body).status_code
    with ThreadPoolExecutor(len(requests)) as pool:
        return sorted(pool.map(send, requests))


def test_parallel_creates_with_one_spoolman_link_have_exactly_one_winner(concurrent_inventory):
    app, engine = concurrent_inventory
    statuses = _race(app, [('post', '/api/spools', {'filament_id': 990, 'spoolman_spool_id': 64})] * 4)
    assert statuses == [200, 409, 409, 409]
    with engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM spools WHERE spoolman_spool_id=64')).scalar_one() == 1


def test_parallel_link_updates_have_exactly_one_winner(concurrent_inventory, inventory_client):
    app, engine = concurrent_inventory
    ids = [_create(inventory_client) for _ in range(3)]
    statuses = _race(app, [('patch', f'/api/spools/{sid}', {'spoolman_spool_id': 65}) for sid in ids])
    assert statuses == [200, 409, 409]
    with engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM spools WHERE spoolman_spool_id=65')).scalar_one() == 1


# ------------------------------------------- final review HIGH-1: every input of the rate lookup

@pytest.mark.parametrize('principal', [{'id': 3, 'role': 'operator', 'group_id': None}, {'id': 4, 'role': 'admin', 'group_id': 1}])
def test_moving_or_deleting_a_rated_filament_requires_superadmin(inventory_client, review_db, principal):
    """material + cost_per_gram + id select the install-wide rate (material_cost_per_gram); id is immutable."""
    from modules.models_library.services import material_cost_per_gram
    superadmin = {'id': 1, 'role': 'admin', 'group_id': None}
    inventory_client.app.dependency_overrides[get_current_user] = lambda: superadmin
    rated = f"lib_{inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Rated ASA', 'material': 'ASA', 'cost_per_gram': 0.04}).json()['id']}"
    unrated = f"lib_{inventory_client.post('/api/filaments', json={'brand': 'Cert', 'name': 'Plain', 'material': 'ASA'}).json()['id']}"
    inventory_client.app.dependency_overrides[get_current_user] = lambda: principal
    assert inventory_client.patch(f'/api/filaments/{rated}', json={'material': 'PETG'}).status_code == 403
    assert inventory_client.delete(f'/api/filaments/{rated}').status_code == 403
    review_db.expire_all()
    assert material_cost_per_gram(review_db, 'ASA') == 0.04
    # Unrated rows and rate-neutral edits stay with operators.
    assert inventory_client.patch(f'/api/filaments/{rated}', json={'name': 'Rated ASA v2', 'material': 'ASA'}).status_code == 200
    assert inventory_client.patch(f'/api/filaments/{unrated}', json={'material': 'PETG'}).status_code == 200
    assert inventory_client.delete(f'/api/filaments/{unrated}').status_code == 204
    inventory_client.app.dependency_overrides[get_current_user] = lambda: superadmin
    assert inventory_client.patch(f'/api/filaments/{rated}', json={'material': 'PETG'}).status_code == 200
    assert inventory_client.delete(f'/api/filaments/{rated}').status_code == 204


def test_link_conflicts_mask_spools_the_org_admin_cannot_see(inventory_client, review_db):
    """Final review MEDIUM-2: same masking as the 409; diagnostics filtered the same way."""
    own = _create(inventory_client)
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {'id': 5, 'role': 'operator', 'group_id': 2}
    foreign = _create(inventory_client)
    foreign_qr = inventory_client.get(f'/api/spools/{foreign}').json()['qr_code']
    review_db.execute(text('UPDATE spools SET spoolman_spool_id=88 WHERE id IN (:a,:b)'), {'a': own, 'b': foreign})
    review_db.commit()
    org_admin = {'id': 3, 'role': 'admin', 'group_id': 1}
    inventory_client.app.dependency_overrides[get_current_user] = lambda: org_admin
    body = inventory_client.get('/api/spools/spoolman-link-conflicts').json()
    assert [c['spoolman_spool_id'] for c in body] == [88]
    spools = body[0]['spools']
    assert any(s['id'] == own and s['label'].startswith(f'#{own} (') for s in spools)
    masked = [s for s in spools if s['id'] != own]
    assert masked == [{'id': None, 'qr_code': None, 'org_id': None, 'label': 'a spool in another organization'}]
    assert foreign_qr not in json.dumps(body)
    from unittest import mock
    from sqlalchemy.orm import sessionmaker
    from modules.system.diagnostics import _spoolman_link_conflicts
    with mock.patch('core.db.SessionLocal', sessionmaker(bind=review_db.get_bind())):
        assert foreign_qr not in json.dumps(_spoolman_link_conflicts(org_admin))
        everything = _spoolman_link_conflicts({'id': 1, 'role': 'admin', 'group_id': None})
    assert foreign_qr in json.dumps(everything)


def test_spool_patch_takes_the_link_lock_before_purchase_and_financial_locks(inventory_client, monkeypatch):
    """Final review LOW-6: one lock order on every spool write path (link lock first)."""
    from modules.inventory.routes import spools as spool_routes
    sid = _create(inventory_client, price=10.0)
    order = []
    for name in ('_serialize_spoolman_link', 'require_financial_owner', 'write_purchase'):
        real = getattr(spool_routes, name)

        def traced(*args, _real=real, _name=name, **kwargs):
            order.append(_name)
            return _real(*args, **kwargs)
        monkeypatch.setattr(spool_routes, name, traced)
    response = inventory_client.patch(f'/api/spools/{sid}', json={'price': 12.5, 'spoolman_spool_id': 501})
    assert response.status_code == 200, response.text
    assert order == ['_serialize_spoolman_link', 'require_financial_owner']
