from sqlalchemy import text
from tests.test_contracts.test_plate_cache_routes import env, library, token_client, review_db, mark_controlled


def test_completed_history_is_scoped_and_limited(env):
    path = f'/print-files/{env.file_id}/completed-jobs'
    for scope in ('read:models', 'read:jobs'):
        assert env.client.get(path, headers=env.issue([scope])).status_code == 403
    auth = env.issue(['read:models', 'read:jobs'])
    assert env.client.get(path, headers=auth).json() == []
    for i in range(24):
        env.db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,charged_to_org_id,printer_id,plate_index,ams_mapping,actual_end) "
                            "VALUES(:id,'Test',:status,:f,:org,9,1,'[0,2]','2026-11-01')"),
                       dict(id=800+i, status='printing' if i == 23 else 'completed', f=env.file_id, org=2 if i == 22 else 1))
    env.db.commit()
    result = env.client.get(path, headers=auth)
    assert result.status_code == 200, result.text
    assert [j['id'] for j in result.json()] == list(range(821,801,-1))
    assert result.json()[0]['ams_mapping'] == [0, 2]
    mark_controlled(env, env.file_id)
    assert env.client.get(path, headers=auth).status_code == 403
