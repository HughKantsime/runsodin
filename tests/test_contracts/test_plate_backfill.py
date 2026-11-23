import json
from threading import Event
from sqlalchemy import text
from tests.test_contracts.test_plate_cache_routes import env, library, token_client, review_db
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers


def test_superadmin_pass_skips_education_and_rejects_double_start(env, monkeypatch):
    from modules.models_library import plate_backfill, plate_cache
    from modules.models_library.mesh_isolation import Busy
    import shutil
    path = '/print-files/plates/backfill'
    headers = interactive_headers()
    for method in (env.client.get, env.client.post):
        assert method(path, headers=headers).status_code == 403
    env.db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    env.db.execute(text("UPDATE print_files SET blob_state='present' WHERE id=11"))
    env.db.commit()
    shutil.rmtree(plate_cache.entry_dir(env.digest))
    ready, release = Event(), Event()
    seen = []
    def parse(row):
        seen.append(row.stored_path)
        if len(seen) == 1:
            raise Busy(1)
        ready.set()
        assert release.wait(5)
        return {}, None
    monkeypatch.setattr(plate_cache, 'facts_result', parse)
    threads = []
    original = plate_backfill.Thread
    def thread(**kwargs):
        t = original(**kwargs)
        threads.append(t)
        return t
    monkeypatch.setattr(plate_backfill, 'Thread', thread)
    try:
        result = env.client.post(path, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()['total'] == 1 and ready.wait(4)
        assert env.client.post(path, headers=headers).status_code == 409
        assert env.client.get(path, headers=headers).json()['state'] == 'running'
    finally:
        release.set()
        for t in threads:
            t.join(5)
    assert seen == [env.stored, env.stored]
    state = env.client.get(path, headers=headers).json()
    assert state['done'] == 1 and state['state'] == 'complete'
    audit = env.db.execute(text("SELECT details FROM audit_logs WHERE action='print_file.plates_backfill_started'")).scalar_one()
    assert json.loads(audit)['count'] == 1


def test_empty_pass_skips_existing_cache_and_status_survives_worker_exit(env, monkeypatch):
    from modules.models_library import plate_backfill
    from tests.test_contracts.cert_D_capture import publish
    from pathlib import Path
    path = '/print-files/plates/backfill'
    env.db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    env.db.commit()
    headers = interactive_headers()
    idle = env.client.get(path, headers=headers)
    assert idle.status_code == 200 and idle.json()['state'] == 'idle'
    fixture = Path(__file__).parents[1] / 'accessibility' / 'fixtures' / 'admin.json'
    publish(fixture, idle.json(), key='GET /api/print-files/plates/backfill')
    # Run the pass synchronously so completion/restart behaviour is deterministic.
    class InlineThread:
        def __init__(self, *, target, args, daemon):
            self.run = lambda: target(*args)
        def start(self):
            self.run()
    monkeypatch.setattr(plate_backfill, 'Thread', InlineThread)
    assert env.client.post(path, headers=headers).json()['total'] == 0
    state = env.client.get(path, headers=headers).json()
    assert state['state'] == 'complete'
    plate_backfill._save({**state, 'state':'running'})
    assert env.client.get(path, headers=headers).json()['state'] == 'interrupted'
