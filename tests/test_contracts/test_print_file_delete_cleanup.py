"""Deleting a print file takes its upload binding and its payload with it, through the real routes.

The id itself isn't reused while sqlite_sequence survives, but a rebuilt database can lose that row,
and then a binding left behind fails the next targeted upload and silently steers the next plain one.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.print_file_binding import target_printer
from tests.test_contracts.test_print_file_upload_ownership_behavior import GCODE, review_db, token_client, upload_client  # noqa: F401

SCOPES = ['write:models', 'write:jobs', 'read:models', 'admin']


CUBE = Path(__file__).resolve().parents[1] / 'fixtures/release_gate/candidate-calibration-cube.3mf'


def _upload(client, headers, name, *, target=None):
    data = {'target_printer_id': str(target)} if target is not None else {}
    content = CUBE.read_bytes() if name.endswith('.3mf') else GCODE + f'; {name}\n'.encode()
    return client.post('/print-files/upload', headers=headers, data=data, files={'file': (name, content, 'application/octet-stream')})


def _file(db, file_id):
    db.commit()
    return db.execute(text('SELECT stored_path,model_id FROM print_files WHERE id=:id'), {'id': file_id}).one()


def _key(db, file_id):
    db.commit()
    return db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': f'print_file_target:{file_id}'}).first()


@pytest.fixture
def uploaded(upload_client, review_db):
    client, issue, storage = upload_client
    headers = issue(SCOPES)
    response = _upload(client, headers, 'Target.gcode', target=9)
    assert response.status_code == 200, response.text
    file_id = response.json()['id']
    stored, model_id = _file(review_db, file_id)
    assert _key(review_db, file_id) is not None and Path(stored).is_file()
    return client, headers, storage, file_id, Path(stored), model_id


def test_deleting_a_file_removes_its_binding_and_payload(uploaded, review_db):
    client, headers, storage, file_id, stored, _ = uploaded
    response = client.delete(f'/print-files/{file_id}', headers=headers)
    assert response.status_code == 200, response.text
    assert _key(review_db, file_id) is None
    assert not stored.exists()
    assert list(storage.iterdir()) == []


def test_deleting_a_variant_removes_its_binding_and_payload(uploaded, review_db):
    from modules.models_library.routes import models_crud
    client, headers, storage, file_id, stored, model_id = uploaded
    client.app.include_router(models_crud.router)
    response = TestClient(client.app, raise_server_exceptions=False).delete(f'/models/{model_id}/variants/{file_id}', headers=headers)
    assert response.status_code == 200, response.text
    assert _key(review_db, file_id) is None and not stored.exists()


def test_a_refused_delete_keeps_everything(uploaded, review_db):
    client, headers, _, file_id, stored, _ = uploaded
    review_db.execute(text("INSERT INTO jobs(item_name,status,print_file_id,quantity,priority) VALUES('Held','pending',:id,1,1)"), {'id': file_id})
    review_db.commit()
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 409
    assert _key(review_db, file_id) is not None and stored.is_file()
    assert _file(review_db, file_id).stored_path == str(stored)


def test_a_delete_that_rolls_back_keeps_everything(uploaded, review_db, monkeypatch):
    from modules.models_library import file_management
    client, headers, _, file_id, stored, _ = uploaded
    def fail(*arguments, **keywords):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(file_management, 'log_audit', fail)
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 500
    assert _key(review_db, file_id) is not None and stored.is_file()
    assert _file(review_db, file_id).stored_path == str(stored)


def test_a_delete_whose_commit_fails_keeps_the_payload(uploaded, review_db, monkeypatch):
    client, headers, _, file_id, stored, _ = uploaded
    real = Session.commit
    def fail(session):
        raise RuntimeError('synthetic commit failure')
    monkeypatch.setattr(Session, 'commit', fail)
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 500
    monkeypatch.setattr(Session, 'commit', real)
    assert stored.is_file()
    assert _key(review_db, file_id) is not None


@pytest.mark.parametrize('table,statement', [
    ('another file', "INSERT INTO print_files(filename,stored_path) VALUES('Other',:path)"),
    ('an archive', "INSERT INTO print_archives(print_name,file_path,status) VALUES('Old',:path,'completed')"),
    ('a model revision', "INSERT INTO model_revisions(model_id,revision_number,file_path,created_at) VALUES(1,99,:path,CURRENT_TIMESTAMP)"),
    ('an Education upload', "UPDATE education_upload_operations SET cleanup_path=:path WHERE operation_id='upload-1'"),
])
def test_a_payload_something_else_names_stays(uploaded, review_db, table, statement):
    client, headers, _, file_id, stored, _ = uploaded
    review_db.execute(text('PRAGMA foreign_keys=OFF'))
    review_db.execute(text(statement), {'path': str(stored)})
    review_db.commit()
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 200
    assert _key(review_db, file_id) is None
    assert stored.is_file(), table


@pytest.mark.parametrize('where', ['outside', 'subfolder', 'symlink', 'relative'])
def test_only_a_regular_file_directly_in_storage_can_go(uploaded, review_db, tmp_path, where):
    client, headers, storage, file_id, stored, _ = uploaded
    if where == 'outside':
        target = tmp_path / 'outside.gcode'
        target.write_bytes(b'keep')
        named = target
    elif where == 'subfolder':
        (storage / 'nested').mkdir()
        target = named = storage / 'nested' / 'file.gcode'
        target.write_bytes(b'keep')
    elif where == 'relative':
        target = storage / 'relative.gcode'
        target.write_bytes(b'keep')
        named = Path(os.path.relpath(target))
    else:
        target = tmp_path / 'target-of-link.gcode'
        target.write_bytes(b'keep')
        named = storage / 'link.gcode'
        named.symlink_to(target)
    review_db.execute(text('UPDATE print_files SET stored_path=:path WHERE id=:id'), {'path': str(named), 'id': file_id})
    review_db.commit()
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 200
    assert target.read_bytes() == b'keep' and named.exists()


def test_a_payload_that_cannot_go_does_not_fail_the_delete(uploaded, review_db, monkeypatch):
    client, headers, _, file_id, stored, _ = uploaded
    real = Path.unlink
    def refuse(self, *arguments, **keywords):
        if self == stored:
            raise PermissionError('synthetic')
        return real(self, *arguments, **keywords)
    monkeypatch.setattr(Path, 'unlink', refuse)
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 200
    assert _key(review_db, file_id) is None and stored.exists()
    assert review_db.execute(text('SELECT count(*) FROM print_files WHERE id=:id'), {'id': file_id}).scalar_one() == 0


def test_a_payload_already_gone_does_not_fail_the_delete(uploaded, review_db):
    client, headers, _, file_id, stored, _ = uploaded
    stored.unlink()
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 200
    assert _key(review_db, file_id) is None


def test_the_id_is_not_reused_while_the_sequence_survives(uploaded, review_db):
    client, headers, _, file_id, _, _ = uploaded
    assert client.delete(f'/print-files/{file_id}', headers=headers).status_code == 200
    response = _upload(client, headers, 'Next.gcode', target=9)
    assert response.status_code == 200 and response.json()['id'] == file_id + 1


@pytest.mark.parametrize('name', ['Reused.gcode', 'Reused.3mf'])
@pytest.mark.parametrize('next_upload,expected', [(9, 9), (None, None)], ids=['targeted', 'plain'])
def test_a_binding_left_behind_cannot_touch_a_new_file_with_that_id(uploaded, review_db, next_upload, expected, name):
    """As a database rebuilt without sqlite_sequence, with a key from before this fix, would have it."""
    client, headers, _, file_id, _, _ = uploaded
    # 2.0.39's delete: the row goes and everything else stays.
    review_db.execute(text('DELETE FROM print_files WHERE id=:id'), {'id': file_id})
    review_db.execute(text("DELETE FROM sqlite_sequence WHERE name='print_files'"))
    stale = file_id
    review_db.execute(text('UPDATE system_config SET value=:value WHERE key=:key'), {'value': '"77"', 'key': f'print_file_target:{stale}'})
    review_db.commit()
    response = _upload(client, headers, name, target=next_upload)
    assert response.status_code == 200, response.text
    assert response.json()['id'] == stale, 'the case only exists if the id really came back'
    review_db.commit()
    assert target_printer(review_db, stale) == expected
