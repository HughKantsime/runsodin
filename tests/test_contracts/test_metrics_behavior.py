"""Actual metrics HTTP authentication, tenant privacy and exposition escaping."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def metrics_client(token_client):
    from modules.system.routes_config import router
    client,issue=token_client;client.app.include_router(router)
    return client,issue


def test_unrelated_scope_cannot_scrape_metrics(metrics_client):
    client,issue=metrics_client
    response=client.get('/metrics',headers=issue(['write:jobs']))
    assert response.status_code==403


def test_tenant_metrics_hide_foreign_printer_labels_and_counts(metrics_client,review_db):
    client,issue=metrics_client
    review_db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id) VALUES(199,'Synthetic foreign metrics','Synthetic','Synthetic',1,'bambu',2)"));review_db.commit()
    response=client.get('/metrics',headers=issue(['read:analytics']))
    assert response.status_code==200
    assert 'Synthetic foreign metrics' not in response.text
    assert 'odin_printers_total 2\n' in response.text


def test_metrics_escape_label_quotes_backslashes_and_newlines(metrics_client,review_db):
    client,issue=metrics_client
    label='Synthetic "quote"\\slash\nline'
    review_db.execute(text('UPDATE printers SET name=:name,nickname=NULL WHERE id=9'),{'name':label});review_db.commit()
    response=client.get('/metrics',headers=issue(['read:analytics']))
    assert response.status_code==200
    escaped=label.replace('\\','\\\\').replace('\n','\\n').replace('"','\\"')
    assert 'printer="'+escaped+'",printer_id="9"' in response.text
    assert 'slash\nline' not in response.text
    from prometheus_client.parser import text_string_to_metric_families
    families=list(text_string_to_metric_families(response.text))
    samples=[sample for family in families for sample in family.samples]
    assert any(sample.labels.get('printer')==label for sample in samples)


@pytest.mark.parametrize('identity',['grouped','unassigned','superadmin'])
def test_metrics_counts_respect_resource_ownership_and_alert_recipient(metrics_client,review_db,identity):
    from modules.inventory.models import FilamentLibrary,Spool
    from modules.orders.models import Order
    from modules.jobs.models import Job
    from modules.notifications.models import Alert
    from prometheus_client.parser import text_string_to_metric_families
    client,issue=metrics_client
    if identity!='grouped':
        role='admin' if identity=='superadmin' else 'operator'
        review_db.execute(text('UPDATE users SET group_id=NULL,role=:role WHERE id=3'),{'role':role})
    material=FilamentLibrary(brand='Synthetic',name='Synthetic metrics PLA',material='PLA')
    review_db.add(material);review_db.flush()
    for org in [1,2,None]:
        review_db.add(Spool(filament_id=material.id,remaining_weight_g=50,org_id=org))
        review_db.add(Order(customer_name='Synthetic metrics',status='pending',org_id=org))
        review_db.add(Job(item_name='Synthetic metrics',status='pending',charged_to_org_id=org))
    for uid in [3,3,1]:
        review_db.add(Alert(user_id=uid,alert_type='printer_offline',severity='warning',title='Synthetic unread',is_read=False))
    review_db.commit()
    response=client.get('/metrics',headers=issue(['read:analytics']))
    assert response.status_code==200
    samples=[s for f in text_string_to_metric_families(response.text) for s in f.samples]
    def value(name,labels=None):
        selected=[s.value for s in samples if s.name==name and (labels is None or s.labels==labels)]
        assert len(selected)==1
        return selected[0]
    count={'grouped':2,'unassigned':1,'superadmin':3}[identity]
    assert value('odin_spools_total')==count and value('odin_spools_low')==count
    assert value('odin_orders_by_status',{'status':'pending'})==count
    assert value('odin_jobs_by_status',{'status':'pending'})==count
    expected_queue=count+(0 if identity=='unassigned' else 1)
    assert value('odin_queue_depth')==expected_queue
    assert value('odin_alerts_unread')==(3 if identity=='superadmin' else 2)


@pytest.mark.parametrize('age,expected',[(0,1),(89,1),(90,0),(91,0),(-60,0)])
def test_metrics_online_window_excludes_stale_and_future_timestamps(metrics_client,review_db,monkeypatch,age,expected):
    from datetime import datetime,timedelta,timezone
    from modules.system import routes_config
    from prometheus_client.parser import text_string_to_metric_families
    frozen=datetime(2026,10,10,12,0,tzinfo=timezone.utc)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return frozen
    monkeypatch.setattr(routes_config,'datetime',FixedDateTime)
    review_db.execute(text('UPDATE printers SET last_seen=NULL'))
    review_db.execute(text("UPDATE printers SET last_seen=:seen,gcode_state='RUNNING' WHERE id=9"),{'seen':(frozen-timedelta(seconds=age)).isoformat()});review_db.commit()
    client,issue=metrics_client
    response=client.get('/metrics',headers=issue(['read:analytics']))
    assert response.status_code==200
    samples={s.name:s.value for f in text_string_to_metric_families(response.text) for s in f.samples if not s.labels}
    assert samples['odin_printers_online']==expected
    assert samples['odin_printers_printing']==expected


@pytest.mark.parametrize('scope',['read','admin','interactive'])
def test_metrics_authorized_scopes_and_shared_printer_samples(metrics_client,review_db,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    from prometheus_client.parser import text_string_to_metric_families
    client,issue=metrics_client
    review_db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id,shared,nozzle_temp) VALUES(199,'Synthetic shared metrics','Synthetic','Synthetic',1,'bambu',2,1,210),(200,'Synthetic unscoped metrics','Synthetic','Synthetic',1,'bambu',NULL,0,205)"));review_db.commit()
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    response=client.get('/metrics',headers=headers)
    assert response.status_code==200
    samples=[s for f in text_string_to_metric_families(response.text) for s in f.samples]
    assert next(s.value for s in samples if s.name=='odin_printers_total')==4
    temps={s.labels['printer']:s.value for s in samples if s.name=='odin_printer_nozzle_temp_celsius'}
    assert temps=={'Synthetic shared metrics':210,'Synthetic unscoped metrics':205}
