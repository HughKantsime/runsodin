"""Serialization and bounds contract tests for the external filament feed."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_filament_ledger_display import (
    _clean_cache,  # noqa: F401
    db,  # noqa: F401
    db_snapshot,
    fake,  # noqa: F401
    ledger_display,
    ledger_payload,
    refresh_and_wait,
    settings,
)


@pytest.fixture
def serialized_printers(db):
    """Exercise the production route and FastAPI's actual response serialization."""
    from sqlalchemy.orm import Session
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.printers.routes_crud import router

    app = FastAPI()
    app.include_router(router)

    def request_db():
        with Session(db.get_bind()) as session:
            yield session

    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {
        'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True,
    }
    with TestClient(app) as client:
        yield client


def _set_feed_text(payload, field, value):
    printer = payload['printers'][0]
    tool = printer['tools'][0]
    if field == 'key':
        printer['key'] = value
    elif field == 'display_name':
        tool[field] = value
    elif field == 'external_id':
        tool['spool_id'] = tool['spool']['id'] = value
    else:
        tool['spool'][field] = value


@pytest.mark.parametrize('field,value', [
    ('name', '\ud800'), ('name', '\udfff'), ('brand', '\ud800'),
    ('material', '\ud800'), ('display_name', '\ud800'),
    ('external_id', '\ud800'), ('key', '\ud800'), ('color_hex', '\ud800'),
    ('name', 'a' * 257), ('brand', 'a' * 257), ('material', 'a' * 1025),
    ('display_name', 'a' * 257), ('external_id', 'a' * 257), ('key', 'a' * 257),
], ids=['name-high-surrogate', 'name-low-surrogate', 'brand-surrogate',
        'material-surrogate', 'display-name-surrogate', 'external-id-surrogate',
        'key-surrogate', 'color-surrogate', 'name-limit', 'brand-limit', 'material-limit',
        'display-name-limit', 'external-id-limit', 'key-limit'])
def test_invalid_feed_text_preserves_serialized_printer_list(db, serialized_printers, fake, field, value):
    from modules.printers.models import Printer

    before = db_snapshot(db)
    payload = ledger_payload()
    _set_feed_text(payload, field, value)
    # JSON permits escaped surrogates, even though response UTF-8 cannot encode them.
    fake.chunks = [json.dumps(payload).encode('utf-8')]
    assert refresh_and_wait() == (False, {})
    rows = db.query(Printer).order_by(Printer.id).all()
    enriched = ledger_display.enrich_printers(rows)
    assert enriched[0].filament_source_status == 'unavailable'
    assert enriched[0].filament_slots == []
    assert enriched[1] is rows[1]
    response = serialized_printers.get('/printers')
    assert response.status_code == 200, response.text
    printers = response.json()
    assert [printer['id'] for printer in printers] == [9, 10]
    assert printers[0]['filament_source_status'] == 'unavailable'
    assert printers[0]['filament_slots'] == []
    assert printers[0]['loaded_colors'] == []
    assert printers[1]['filament_slots'][0]['color'] == 'local value'
    assert db_snapshot(db) == before


@pytest.mark.parametrize('tools', [
    [{'tool_index': index, 'spool_status': 'unmapped', 'spool': None} for index in range(65)],
    [{'tool_index': 256, 'spool_status': 'unmapped', 'spool': None}],
    [{'tool_index': 10**30, 'spool_status': 'unmapped', 'spool': None}],
], ids=['tool-count-limit', 'tool-index-limit', 'oversized-tool-index'])
def test_tool_bounds_fail_closed_in_serialized_response(db, serialized_printers, fake, tools):
    from modules.printers.models import Printer

    fake.chunks = [json.dumps(ledger_payload(tools=tools)).encode()]
    assert refresh_and_wait() == (False, {})
    row = db.query(Printer).filter_by(id=9).one()
    assert ledger_display.enrich_printers([row])[0].filament_source_status == 'unavailable'
    response = serialized_printers.get('/printers')
    assert response.status_code == 200
    assert [printer['id'] for printer in response.json()] == [9, 10]
    assert response.json()[0]['filament_source_status'] == 'unavailable'


def test_feed_limits_accept_unicode_boundaries_without_truncation(db, serialized_printers, fake, monkeypatch):
    from modules.printers.models import Printer

    payload = ledger_payload()
    label = '😀' * 256
    material = '日本語＋' * 256
    for field in ('name', 'brand', 'display_name', 'external_id', 'key'):
        _set_feed_text(payload, field, label)
    _set_feed_text(payload, 'material', material)
    tools = payload['printers'][0]['tools']
    tools.extend({'tool_index': index, 'spool_status': 'unmapped', 'spool': None}
                 for index in range(2, 64))
    monkeypatch.setattr(settings, 'filament_ledger_printers', {9: label})
    fake.chunks = [json.dumps(payload).encode()]
    assert refresh_and_wait()[0] is True
    row = db.query(Printer).filter_by(id=9).one()
    assert len(ledger_display.enrich_printers([row])[0].filament_slots) == 64
    response = serialized_printers.get('/printers')
    assert response.status_code == 200, response.text
    slots = response.json()[0]['filament_slots']
    assert len(slots) == 64
    assert slots[0]['display_name'] == label
    assert slots[0]['external_spool_id'] == label
    assert slots[0]['color'] == f'{label} {label}'
    assert slots[0]['material_type'] == material
    assert slots[-1]['slot_number'] == 64
    assert slots[-1]['display_name'] == 'Tool 64'


@pytest.mark.parametrize('display_name', [None, '', '   ', 42, False, [], {}])
def test_optional_tool_name_keeps_serialized_fallback(db, serialized_printers, fake, display_name):
    payload = ledger_payload()
    _set_feed_text(payload, 'display_name', display_name)
    fake.chunks = [json.dumps(payload).encode()]
    assert refresh_and_wait()[0] is True
    response = serialized_printers.get('/printers')
    assert response.status_code == 200
    assert response.json()[0]['filament_slots'][0]['display_name'] == 'Tool 1'


@pytest.mark.parametrize('spool_id,nested_id,valid', [
    (10**22, 1e22, False), (1, 1.0, True),
    (2**63 - 1, 2**63 - 1, True), (-(2**63), -(2**63), True),
    (2**63, 2**63, False), (-(2**63) - 1, -(2**63) - 1, False),
    (10**3999, 10**3999, False),
], ids=['equal-large-float', 'equal-small-float', 'max-int64', 'min-int64',
        'above-int64', 'below-int64', '4000-digit-id'])
def test_numeric_spool_identity_serializes_safely(db, serialized_printers, fake, spool_id, nested_id, valid):
    payload = ledger_payload()
    tool = payload['printers'][0]['tools'][0]
    tool['spool_id'] = spool_id
    tool['spool']['id'] = nested_id
    fake.chunks = [json.dumps(payload).encode()]
    assert refresh_and_wait()[0] is valid
    response = serialized_printers.get('/printers')
    assert response.status_code == 200, response.text
    printer = response.json()[0]
    if valid:
        external_id = printer['filament_slots'][0]['external_spool_id']
        assert type(external_id) is int
        assert external_id == spool_id
    else:
        assert printer['filament_source_status'] == 'unavailable'
        assert printer['filament_slots'] == []
    assert response.json()[1]['filament_slots'][0]['color'] == 'local value'


@pytest.mark.parametrize('tool_index', [64, 254, 255])
def test_external_holder_indexes_serialize(db, serialized_printers, fake, tool_index):
    payload = ledger_payload(tools=[{
        'tool_index': tool_index, 'spool_status': 'unmapped', 'spool': None,
    }])
    fake.chunks = [json.dumps(payload).encode()]
    assert refresh_and_wait()[0] is True
    response = serialized_printers.get('/printers')
    assert response.status_code == 200, response.text
    slot = response.json()[0]['filament_slots'][0]
    assert slot['slot_number'] == tool_index + 1
    assert slot['display_name'] == f'Tool {tool_index + 1}'
