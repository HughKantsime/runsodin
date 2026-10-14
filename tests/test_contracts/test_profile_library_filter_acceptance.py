"""Persisted HTTP acceptance for profile tag editing and existing-field filters."""
from sqlalchemy import text
from tests.test_contracts.test_profile_library_behavior import profile_client, token_client, review_db


def test_saved_tags_persist_and_are_searchable_without_content_changes(profile_client, review_db):
    client, issue, calls, _ = profile_client
    before = review_db.execute(text('SELECT raw_content,is_shared,is_default FROM printer_profiles WHERE id=101')).one()
    headers = issue(['read:models', 'write:models'])
    response = client.put('/profiles/101', headers=headers, json={'tags': 'school,reviewed'})
    assert response.status_code == 200, response.text
    assert client.get('/profiles/101', headers=headers).json()['tags'] == 'school,reviewed'
    assert [r['id'] for r in client.get('/profiles?search=reviewed', headers=headers).json()['profiles']] == [101]
    assert review_db.execute(text('SELECT raw_content,is_shared,is_default FROM printer_profiles WHERE id=101')).one() == before
    assert client.put('/profiles/101', headers=headers, json={'tags': ''}).status_code == 200
    assert client.get('/profiles/101', headers=headers).json()['tags'] == ''
    assert calls == []


def test_model_material_and_printer_filters_intersect_existing_scoped_metadata(profile_client, review_db):
    client, issue, calls, _ = profile_client
    review_db.execute(text("UPDATE printers SET model='Synthetic model' WHERE id IN (9,11)"))
    review_db.execute(text("UPDATE printer_profiles SET printer_id=9,filament_type='PETG' WHERE id IN (101,102)"))
    review_db.execute(text("UPDATE printer_profiles SET printer_id=11,filament_type='PETG' WHERE id=103"))
    review_db.commit()
    headers = issue(['read:models'])
    response = client.get('/profiles', headers=headers, params={'printer_model':'Synthetic model','filament_type':'PETG','printer_id':9})
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 1
    assert [r['id'] for r in response.json()['profiles']] == [101]
    # Null-org profile linked to a foreign printer cannot expose its hidden model through filtering.
    response = client.get('/profiles', headers=headers, params={'printer_model':'Synthetic model','filament_type':'PETG'})
    assert [r['id'] for r in response.json()['profiles']] == [101]
    response = client.get('/profiles', headers=headers, params={'printer_model':"' OR 1=1 --"})
    assert response.status_code == 200 and response.json()['total'] == 0
    assert client.get('/profiles?printer_id=bad', headers=headers).status_code == 422
    assert calls == []
