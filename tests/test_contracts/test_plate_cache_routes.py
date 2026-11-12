"""Plate facts/thumbnail routes, the printer-card endpoint and the payload census."""
import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import text

from modules.models_library import plate_cache
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401
from tests.test_contracts.test_cert_C_models import library, sliced_3mf, _upload  # noqa: F401
from tests.test_contracts.test_home_assistant_status import ha_client  # noqa: F401
from tests.test_contracts.test_model_preview_authorization_behavior import principal_headers
from tests.test_contracts.test_plate_facts_parser import png
import zipfile

BACKEND = Path(__file__).resolve().parents[2] / 'backend'
COLORS = {1: (250, 0, 0), 2: (0, 250, 0), 3: (0, 0, 250)}


def three_plate_package():
    """The cert-C sliced package with a distinct PNG per plate."""
    original = zipfile.ZipFile(io.BytesIO(sliced_3mf(plates=3)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in original.namelist():
            if name != 'Metadata/plate_1.png':
                archive.writestr(name, original.read(name))
        for number, color in COLORS.items():
            archive.writestr(f'Metadata/plate_{number}.png', png(color))
    return out.getvalue()


def pixel(content):
    return Image.open(io.BytesIO(content)).convert('RGB').getpixel((0, 0))


@pytest.fixture
def env(library, review_db, tmp_path):
    from modules.printers import routes_plate_thumbnail
    client, issue = library
    client.app.include_router(routes_plate_thumbnail.router)
    content = three_plate_package()
    body = _upload(client, issue(['write:models', 'read:models']), content)
    stored = review_db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'), {'id': body['id']}).scalar_one()
    review_db.commit()
    return type('Env', (), {'client': client, 'issue': issue, 'file_id': body['id'], 'stored': stored,
                            'digest': hashlib.sha256(content).hexdigest(), 'db': review_db, 'tmp': tmp_path})


def as_education_file(env):
    """Give the fixture's Education-owned file (11) the uploaded package's hash and cache.

    Education ownership is tied to the stored path, so the upload record moves with it."""
    primed = env.client.get(f'/print-files/{env.file_id}/plates', headers=env.issue(['read:models']))
    assert primed.status_code == 200, primed.text
    copy = env.tmp / 'edu-copy.3mf'
    shutil.copy(env.stored, copy)
    env.db.execute(text("UPDATE print_files SET file_hash=:h, stored_path=:p, blob_state='present' WHERE id=11"),
                   {'h': env.digest, 'p': str(copy)})
    # Education ownership follows the stored path: move the upload record with it.
    env.db.execute(text("UPDATE education_upload_operations SET final_path=:p WHERE operation_id='upload-1'"), {'p': str(copy)})
    env.db.commit()


def start_print(env, file_id=None, plate_index=None, printer=9, job=501, status='running', model_id=None):
    env.db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,plate_index,model_id,charged_to_org_id) "
                        "VALUES(:j,'Synthetic','printing',:f,:pi,:m,1)"),
                   {'j': job, 'f': file_id, 'pi': plate_index, 'm': model_id})
    env.db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,scheduled_job_id) "
                        "VALUES(:p,'synthetic.gcode',:s,'2026-11-12 10:00:00',:j)"), {'p': printer, 's': status, 'j': job})
    env.db.commit()


def mark_controlled(env, file_id, members=(1,)):
    env.db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
    for user in members:
        env.db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:u)"), {'u': user})
    env.db.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                        "VALUES(:f,'CUI',1,'Secure',7,'Contract restriction','2026-11-01T00:00:00+00:00')"), {'f': file_id})
    env.db.commit()


# ------------------------------------------------------------------ file routes

def test_upload_cached_facts_and_plates_route(env):
    response = env.client.get(f'/print-files/{env.file_id}/plates', headers=env.issue(['read:models']))
    assert response.status_code == 200, response.text
    facts = response.json()
    assert facts['version'] == 1 and facts['file_hash'] == env.digest and facts['plate_count'] == 3
    assert [p['index'] for p in facts['plates']] == [1, 2, 3] and all(p['has_thumbnail'] for p in facts['plates'])
    assert facts['plates'][0]['warnings'] == [] and facts['plates'][1]['warnings'] is None
    assert facts['plates'][0]['print_time_seconds'] == 5430 and facts['slicer']['version']
    assert not any('b64' in key or key == 'thumbnail' for plate in facts['plates'] for key in plate)


def test_each_plate_thumbnail_is_the_right_image_and_never_cached_by_browsers(env):
    for number, color in COLORS.items():
        response = env.client.get(f'/print-files/{env.file_id}/plates/{number}/thumbnail', headers=env.issue(['read:models']))
        assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
        assert response.headers['cache-control'] == 'no-store' and pixel(response.content) == color
    for missing in (4, 0, 99999):
        assert env.client.get(f'/print-files/{env.file_id}/plates/{missing}/thumbnail', headers=env.issue(['read:models'])).status_code == 404


def test_backfill_on_first_read_and_thumbnail_route_is_cache_only(env):
    shutil.rmtree(plate_cache.entry_dir(env.digest))
    assert env.client.get(f'/print-files/{env.file_id}/plates/1/thumbnail', headers=env.issue(['read:models'])).status_code == 404
    assert plate_cache.read_facts(env.digest) is None
    response = env.client.get(f'/print-files/{env.file_id}/plates', headers=env.issue(['read:models']))
    assert response.status_code == 200 and plate_cache.read_facts(env.digest) is not None
    assert env.client.get(f'/print-files/{env.file_id}/plates/2/thumbnail', headers=env.issue(['read:models'])).status_code == 200


def test_no_hash_or_purged_blob_means_not_available(env):
    env.db.execute(text("INSERT INTO print_files(id,filename,stored_path,org_id) VALUES(90,'x.3mf','/opaque/x.3mf',1)"))
    env.db.commit()
    headers = env.issue(['read:models'])
    assert env.client.get('/print-files/90/plates', headers=headers).status_code == 404
    assert env.client.get('/print-files/90/plates/1/thumbnail', headers=headers).status_code == 404
    env.db.execute(text("UPDATE print_files SET blob_state='purged' WHERE id=:id"), {'id': env.file_id})
    env.db.commit()
    assert plate_cache.read_facts(env.digest) is not None   # the cache still exists, the row no longer serves it
    assert env.client.get(f'/print-files/{env.file_id}/plates', headers=headers).status_code == 404
    assert env.client.get(f'/print-files/{env.file_id}/plates/1/thumbnail', headers=headers).status_code == 404


def test_file_routes_token_scopes(env):
    for path in (f'/print-files/{env.file_id}/plates', f'/print-files/{env.file_id}/plates/1/thumbnail'):
        assert env.client.get(path, headers=env.issue(['read:printers'])).status_code == 403
        for scopes in (['read:models'], ['read'], ['admin']):
            assert env.client.get(path, headers=env.issue(scopes)).status_code == 200


def test_other_organization_cannot_reach_a_cache_that_exists_for_its_hash(env):
    env.db.execute(text("INSERT INTO print_files(id,filename,stored_path,org_id,file_hash,blob_state) "
                        "VALUES(91,'f.3mf',:p,2,:h,'present')"), {'p': env.stored, 'h': env.digest})
    env.db.commit()
    headers = env.issue(['read:models'])
    assert env.client.get('/print-files/91/plates', headers=headers).status_code == 404
    assert env.client.get('/print-files/91/plates/1/thumbnail', headers=headers).status_code == 404


def test_controlled_part_audience_in_and_out(env):
    mark_controlled(env, env.file_id, members=(1,))
    for path in (f'/print-files/{env.file_id}/plates', f'/print-files/{env.file_id}/plates/1/thumbnail'):
        refused = env.client.get(path, headers=env.issue(['read:models']))   # user 3 is not in the audience
        assert refused.status_code == 403 and 'plate' not in refused.text.lower()
    env.db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)"))
    env.db.commit()
    for path in (f'/print-files/{env.file_id}/plates', f'/print-files/{env.file_id}/plates/1/thumbnail'):
        assert env.client.get(path, headers=env.issue(['read:models'])).status_code == 200


def test_education_owned_file_uses_the_education_policy(env):
    as_education_file(env)
    for user in (1, 2):
        for path in ('/print-files/11/plates', '/print-files/11/plates/1/thumbnail'):
            assert env.client.get(path, headers=principal_headers(env.issue, env.db, user)).status_code == 200
    for path in ('/print-files/11/plates', '/print-files/11/plates/1/thumbnail'):
        assert env.client.get(path, headers=principal_headers(env.issue, env.db, 4)).status_code == 404
    env.db.execute(text('UPDATE users SET role=\'viewer\' WHERE id=3'))
    env.db.commit()
    assert env.client.get('/print-files/11/plates/1/thumbnail', headers=env.issue(['read:models'])).status_code == 404


def test_education_file_needs_an_available_policy_provider(env, monkeypatch):
    from core.registry import registry
    as_education_file(env)
    monkeypatch.delitem(registry._providers, 'EducationPolicyProvider', raising=False)
    assert env.client.get('/print-files/11/plates/1/thumbnail', headers=principal_headers(env.issue, env.db, 1)).status_code == 404


def test_upload_survives_a_cache_failure_and_builds_the_cache_when_healthy(library, review_db, tmp_path, monkeypatch):
    client, issue = library
    healthy = three_plate_package()
    body = _upload(client, issue(['write:models']), healthy)
    assert body['id'] and plate_cache.read_facts(hashlib.sha256(healthy).hexdigest())['plate_count'] == 3

    def failing(*args, **kwargs):
        raise RuntimeError('cache exploded')
    monkeypatch.setattr(plate_cache, 'store_analysis', failing)
    other = sliced_3mf(plates=2, project='Another part')
    upload = client.post('/print-files/upload', headers=issue(['write:models']),
                         files={'file': ('Another part.3mf', other, 'application/octet-stream')})
    assert upload.status_code == 200, upload.text
    assert plate_cache.read_facts(hashlib.sha256(other).hexdigest()) is None


# ------------------------------------------------------------------ printer card endpoint

def thumb(env, printer=9, scopes=('read:printers', 'read:models')):
    return env.client.get(f'/printers/{printer}/current-plate-thumbnail', headers=env.issue(list(scopes)))


def test_current_plate_is_the_zero_based_plate_index_with_default_plate_one(env):
    start_print(env, env.file_id, plate_index=1)
    response = thumb(env)
    assert response.status_code == 200 and pixel(response.content) == COLORS[2]
    assert response.headers['cache-control'] == 'no-store' and response.headers['content-type'] == 'image/png'
    env.db.execute(text('UPDATE jobs SET plate_index=NULL WHERE id=501'))
    env.db.commit()
    assert pixel(thumb(env).content) == COLORS[1]
    env.db.execute(text('UPDATE jobs SET plate_index=2 WHERE id=501'))
    env.db.commit()
    assert pixel(thumb(env).content) == COLORS[3]
    env.db.execute(text('UPDATE jobs SET plate_index=7 WHERE id=501'))
    env.db.commit()
    assert thumb(env).status_code == 404


def test_current_plate_resolves_the_models_file_when_the_job_names_none(env):
    env.db.execute(text("INSERT INTO models(id,name,org_id,print_file_id) VALUES(700,'Synthetic model',1,:f)"), {'f': env.file_id})
    env.db.commit()
    start_print(env, None, model_id=700)
    assert pixel(thumb(env).content) == COLORS[1]


def test_current_plate_404_without_running_print_cache_or_thumbnail(env):
    assert thumb(env).status_code == 404                                  # nothing running
    start_print(env, env.file_id, status='completed')
    assert thumb(env).status_code == 404                                  # only a finished print
    env.db.execute(text("UPDATE print_jobs SET status='running'"))
    env.db.commit()
    assert thumb(env).status_code == 200
    shutil.rmtree(plate_cache.entry_dir(env.digest))
    assert thumb(env).status_code == 404                                  # cache-only: never parses
    assert plate_cache.read_facts(env.digest) is None


def test_current_plate_requires_printer_visibility(env):
    env.db.execute(text("INSERT INTO printers(id,name,org_id,is_active,api_type) VALUES(71,'Foreign printer',2,1,'bambu')"))
    env.db.commit()
    start_print(env, env.file_id, printer=71)
    assert thumb(env, printer=71).status_code == 404
    assert thumb(env, printer=9999).status_code == 404


def test_current_plate_outside_the_audience_is_404_and_member_is_served(env):
    start_print(env, env.file_id)
    mark_controlled(env, env.file_id, members=(1,))
    assert thumb(env).status_code == 404
    env.db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)"))
    env.db.commit()
    assert thumb(env).status_code == 200


def test_current_plate_marked_job_with_unmarked_file_is_still_404(env):
    start_print(env, env.file_id)
    env.db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
    env.db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)"))
    env.db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                        "VALUES(501,'CUI',1,'Secure',7,'Contract restriction','2026-11-01T00:00:00+00:00')"))
    env.db.commit()
    assert thumb(env).status_code == 404


def test_current_plate_education_file_follows_education_policy(env):
    as_education_file(env)
    env.db.execute(text("UPDATE jobs SET print_file_id=11 WHERE id=13"))
    env.db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,scheduled_job_id) "
                        "VALUES(9,'class.gcode','running','2026-11-12 10:00:00',13)"))
    env.db.commit()
    scopes = ['read:printers', 'read:models']
    ok = env.client.get('/printers/9/current-plate-thumbnail', headers=principal_headers(env.issue, env.db, 1, scopes))
    assert ok.status_code == 200 and pixel(ok.content) == COLORS[1]
    assert env.client.get('/printers/9/current-plate-thumbnail', headers=principal_headers(env.issue, env.db, 4, scopes)).status_code == 404


def test_current_plate_token_scopes(env):
    start_print(env, env.file_id)
    assert thumb(env, scopes=('read:printers',)).status_code == 403
    assert thumb(env, scopes=('read:models',)).status_code == 403
    assert thumb(env, scopes=('read:printers', 'read:models')).status_code == 200
    assert thumb(env, scopes=('read',)).status_code == 200
    assert thumb(env, scopes=('admin',)).status_code == 200


# ------------------------------------------------------------------ census: where the image can and cannot appear

BANNED_KEY_PARTS = ('thumb', 'plate_picture', 'plate_image', 'plate_preview')


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


def assert_no_image_keys(payload):
    bad = [key for key in all_keys(payload) if any(part in key.lower() for part in BANNED_KEY_PARTS)]
    assert bad == [], bad


def test_home_assistant_payload_has_no_thumbnail_key(ha_client):
    client, issue = ha_client
    response = client.get('/integrations/home-assistant/status', headers=issue(['read:printers', 'read:jobs']))
    assert response.status_code == 200 and response.json()['printers']
    assert_no_image_keys(response.json())


def test_overlay_payload_has_no_thumbnail_key(token_client, review_db):
    from modules.printers import routes_overlay
    client, _ = token_client
    client.app.include_router(routes_overlay.router)
    review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at) VALUES(9,'synthetic.gcode','running','2026-11-12 10:00:00')"))
    review_db.commit()
    response = client.get('/overlay/9')
    assert response.status_code == 200 and response.json()['job_name'] == 'synthetic.gcode'
    assert_no_image_keys(response.json())


def test_printer_response_schema_has_no_thumbnail_field():
    from modules.printers.schemas import PrinterResponse
    assert_no_image_keys({name: None for name in PrinterResponse.model_fields})


def test_only_the_cache_routes_reference_the_cache_or_the_endpoint():
    """Status payloads, WebSocket/MQTT, the overlay, Home Assistant and camera-token routes never import it."""
    cache_users, endpoint_users = set(), set()
    for root in (BACKEND / 'core', BACKEND / 'modules'):
        for path in root.rglob('*.py'):
            relative = path.relative_to(BACKEND)
            if any(part.startswith('preserved') for part in relative.parts):
                continue
            source = path.read_text(encoding='utf-8', errors='replace')
            if 'plate_cache' in source:
                cache_users.add(relative.as_posix())
            if 'current-plate-thumbnail' in source:
                endpoint_users.add(relative.as_posix())
    # The parser, its isolated worker and the Education inventory entry name it; nothing that
    # builds a status, event, overlay, Home Assistant or camera payload does.
    assert cache_users == {'modules/models_library/plate_cache.py', 'modules/models_library/mesh_isolation.py',
                           'modules/models_library/mesh_worker.py', 'modules/models_library/threemf_parser.py',
                           'modules/models_library/routes/print_files.py', 'modules/printers/routes_plate_thumbnail.py',
                           'modules/organizations/education_policy_inventory.py',
                           'core/app_maintenance.py'}  # the hourly sweep of orphaned entries
    assert endpoint_users == {'modules/printers/routes_plate_thumbnail.py',
                              'modules/organizations/education_policy_inventory.py'}


def test_sweep_removes_the_cache_of_a_file_that_no_longer_exists(env):
    from modules.models_library import plate_cache
    headers = env.issue(['read:models'])
    assert env.client.get(f'/print-files/{env.file_id}/plates', headers=headers).status_code == 200
    entry = plate_cache.entry_dir(env.digest)
    assert entry.is_dir() and plate_cache.sweep(env.db) == 0          # a present file keeps its cache
    # Something this module did not name is never touched.
    stranger = plate_cache.cache_root() / 'zz' / 'not-a-hash'
    stranger.mkdir(parents=True)
    env.db.execute(text("UPDATE print_files SET blob_state='retired' WHERE id=:id"), {'id': env.file_id})
    env.db.commit()
    assert plate_cache.sweep(env.db) == 1 and not entry.exists() and stranger.is_dir()
    assert env.client.get(f'/print-files/{env.file_id}/plates/1/thumbnail', headers=headers).status_code == 404


def test_sweep_keeps_a_cache_another_present_file_shares(env):
    from modules.models_library import plate_cache
    assert env.client.get(f'/print-files/{env.file_id}/plates', headers=env.issue(['read:models'])).status_code == 200
    env.db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,file_hash,blob_state) "
                        "VALUES(990,'twin.3mf','twin.3mf','/opaque/twin.3mf',:h,'present')"), {'h': env.digest})
    env.db.execute(text("DELETE FROM print_files WHERE id=:id"), {'id': env.file_id})
    env.db.commit()
    assert plate_cache.sweep(env.db) == 0 and plate_cache.entry_dir(env.digest).is_dir()
