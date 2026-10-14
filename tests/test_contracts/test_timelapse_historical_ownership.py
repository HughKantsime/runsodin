"""Historical media tenant boundaries using actual HTTP and owned bytes only."""
import pytest
from sqlalchemy import text,event
from tests.test_contracts.test_camera_scope_behavior import camera_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def historical_media(camera_client,review_db,tmp_path,monkeypatch):
    from modules.printers import camera_routes
    engine=review_db.get_bind()
    def enable(connection,record):connection.execute('PRAGMA foreign_keys=ON')
    event.listen(engine,'connect',enable)
    raw=review_db.connection().connection.driver_connection
    raw.execute('PRAGMA foreign_keys=ON')
    assert raw.execute('PRAGMA foreign_keys').fetchone()[0]==1
    root=tmp_path/'owned-historical-root';root.mkdir(mode=0o700)
    monkeypatch.setattr(camera_routes,'TIMELAPSE_ROOT',str(root))
    # The source job is charged to tenant 2. Printer 9 now belongs to tenant 1,
    # whose admin issues the token below. Sharing/reassignment is not evidence
    # that tenant 2's historical content became tenant 1's property.
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status) VALUES(444,'Tenant 2 historical print',9,2,'completed')"))
    review_db.execute(text('UPDATE timelapses SET print_job_id=444,ownership_operation_id=NULL WHERE id=199'))
    review_db.commit()
    media=tmp_path/'owned-historical.mp4';media.write_bytes(b'OWNED_TENANT_2_HISTORICAL_MEDIA')
    monkeypatch.setattr(camera_routes,'_get_timelapse_path',lambda t:media)
    return camera_client


def test_current_printer_tenant_cannot_read_historical_other_tenant_detail(historical_media):
    client,issue,_=historical_media
    assert client.get('/timelapses/199',headers=issue(['read:printers'])).status_code==404


def test_current_printer_tenant_does_not_list_historical_other_tenant_media(historical_media):
    client,issue,_=historical_media
    response=client.get('/timelapses',headers=issue(['read:printers']))
    assert response.status_code==200
    assert response.json()['total']==0 and response.json()['timelapses']==[]


@pytest.mark.parametrize('path',['stream','download'])
def test_current_printer_tenant_cannot_fetch_historical_other_tenant_bytes(historical_media,path):
    client,issue,_=historical_media
    response=client.get(f'/timelapses/199/{path}',headers=issue(['read:printers']))
    assert response.status_code==404
    assert b'OWNED_TENANT_2_HISTORICAL_MEDIA' not in response.content
