"""Actual archive comparison, tags and filtered CSV on owned persisted records."""
import csv,io
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client,review_db


def test_compare_returns_exact_differing_fields_and_refuses_foreign(reprint_client,review_db):
    client,_,_=reprint_client
    review_db.execute(text("UPDATE print_archives SET actual_duration_seconds=60,cost_estimate=2 WHERE id=101"))
    review_db.execute(text("INSERT INTO print_archives(id,printer_id,print_name,status,org_id,ownership_state,ownership_source,actual_duration_seconds,cost_estimate) VALUES(102,9,'Synthetic second','failed',1,'known','synthetic',120,3)"));review_db.commit()
    response=client.get('/archives/compare',params={'a':101,'b':102})
    assert response.status_code==200
    assert response.json()['diff']['actual_duration_seconds']=={'a':60,'b':120}
    assert response.json()['diff']['cost_estimate']=={'a':2,'b':3}
    assert response.json()['diff']['status']=={'a':'completed','b':'failed'}
    assert 'printer_display' not in response.json()['diff']
    review_db.execute(text('UPDATE print_archives SET org_id=2 WHERE id=102'));review_db.commit()
    assert client.get('/archives/compare',params={'a':101,'b':102}).status_code==404


def test_tags_are_trimmed_deduplicated_and_persisted(reprint_client,review_db):
    client,_,_=reprint_client
    response=client.patch('/archives/101/tags',json={'tags':[' Alpha ','','Alpha',' Beta ',' ']})
    assert response.status_code==200 and response.json()=={'tags':['Alpha','Beta']}
    assert review_db.execute(text('SELECT tags FROM print_archives WHERE id=101')).scalar_one()=='Alpha, Beta'


def test_filtered_archive_csv_preserves_quotes_duration_and_tag(reprint_client,review_db):
    client,_,_=reprint_client
    review_db.execute(text('UPDATE print_archives SET print_name=:name,tags=:tags,actual_duration_seconds=123 WHERE id=101'),{'name':'Synthetic, "quoted"','tags':'Alpha, Beta'});review_db.commit()
    response=client.get('/archives/log/export',params={'status':'completed','tag':'Alpha','search':'quoted'})
    assert response.status_code==200 and response.headers['content-type'].startswith('text/csv')
    rows=list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows)==1 and rows[0]['Print Name']=='Synthetic, "quoted"' and float(rows[0]['Duration (s)'])==123
    assert rows[0]['Tags']=='Alpha, Beta'
    injected=client.get('/archives/log/export',params={'search':"' OR 1=1 --"})
    assert injected.status_code==200 and list(csv.DictReader(io.StringIO(injected.text)))==[]
