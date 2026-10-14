"""Advertised project restore must retain exported archive metadata and bytes."""
from pathlib import Path
import io,zipfile,json
from sqlalchemy import text
from tests.test_contracts.test_project_behavior import project_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db


def test_owned_project_export_import_restores_archive_and_file(project_client,review_db,tmp_path):
    client,issue=project_client
    source=tmp_path/'owned-print.3mf';source.write_bytes(b'owned-project-archive-bytes')
    review_db.execute(text('UPDATE print_archives SET project_id=101,file_path=:path WHERE id=201'),{'path':str(source)})
    review_db.commit()
    exported=client.get('/projects/101/export',headers=issue(['read:jobs']))
    assert exported.status_code==200,exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as package:
        metadata=json.loads(package.read('project.json'))
        assert [row['id'] for row in metadata['archives']]==[201]
        assert package.read('files/owned-print.3mf')==source.read_bytes()
    imported=client.post('/projects/import',headers=issue(['write:jobs']),files={'file':('owned.zip',exported.content,'application/zip')})
    assert imported.status_code==200,imported.text
    project=imported.json()['id']
    assert project!=101
    restored=review_db.execute(text('SELECT print_name,file_path FROM print_archives WHERE project_id=:project'),{'project':project}).all()
    assert len(restored)==1,'Project import discarded exported archive history'
    assert restored[0].print_name=='Own archive'
    assert Path(restored[0].file_path).read_bytes()==source.read_bytes()
    assert restored[0].file_path!=str(source)
