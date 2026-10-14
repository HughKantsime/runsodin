"""Plate-clear API scope and unchanged empty-queue behavior on owned data."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client


@pytest.mark.parametrize('scopes,expected',[(['read'],403),(['write:printers'],200)])
def test_plate_clear_empty_queue_respects_write_scope_without_changing_print(offline_store,token_client,scopes,expected):
    from modules.printers import routes_controls
    client,issue=token_client
    client.app.include_router(routes_controls.router)
    before=tuple(offline_store.execute(text('SELECT gcode_state,last_seen FROM printers WHERE id=199')).one())
    response=client.post('/printers/199/plate-cleared',headers=issue(scopes))
    assert response.status_code==expected
    if expected==200:assert response.json()=={'success':True,'message':'No jobs waiting for plate clear'}
    assert tuple(offline_store.execute(text('SELECT gcode_state,last_seen FROM printers WHERE id=199')).one())==before


def test_plate_clear_missing_printer_returns_not_found(token_client):
    from modules.printers import routes_controls
    client,issue=token_client;client.app.include_router(routes_controls.router)
    assert client.post('/printers/999999/plate-cleared',headers=issue(['write:printers'])).status_code==404
