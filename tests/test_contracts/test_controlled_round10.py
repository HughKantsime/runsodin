"""Public predecessors, undated media and the actual OBS camera boundary."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from core.audit import log_audit
from core.controlled_audit import audit_allowed
from core.controlled_parts import audit_visibility, policies
from core.controlled_retention import expired, preview_acknowledged
from core.controlled_retention_query import pending_count
from tests.test_contracts.test_controlled_parts import actor, database
from tests.test_contracts.test_controlled_guards import marked
from tests.test_contracts.test_controlled_round8 import (
    ORIGIN, clock_db, native_pg, add_job, add_rule, add_media, delete_job, new_job,
)


@pytest.mark.parametrize('missing_created', [False, True])
def test_public_predecessor_does_not_inherit_later_job_rules(clock_db, tier_license, tmp_path, missing_created):
    from core.controlled_redaction import redact_export
    from modules.archives.controlled_retention import purge_due
    from modules.organizations.routes_controlled_retention import acknowledge, PurgeAcknowledgement
    db = clock_db
    tier_license('enterprise')
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                    "VALUES(1,'member','unused','operator',:active),(2,'outsider','unused','admin',:active)"), {'active': True})
    db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)'))
    def audit(job_id, at):
        entry = log_audit(db, 'job.updated', 'job', job_id, {'name': 'instance'})
        entry.timestamp = at.replace(tzinfo=None)
        db.flush()
        return entry.id

    first_id = new_job(db)
    public_audit = audit(first_id, ORIGIN)
    add_media(db, 1, first_id, ORIGIN)
    delete_job(db, first_id)
    assert db.execute(text('SELECT count(*) FROM controlled_job_tombstones')).scalar_one() == 0
    second_id = new_job(db, created=ORIGIN + timedelta(days=2))
    assert second_id > first_id
    add_rule(db, 'jobs', second_id, marked=ORIGIN + timedelta(days=2), days=7)
    private_audit = audit(second_id, ORIGIN + timedelta(days=2))
    add_media(db, 2, second_id, ORIGIN + timedelta(days=2))
    delete_job(db, second_id)
    snapshot = dict(db.execute(text('SELECT job_id,audit_max_id FROM controlled_job_tombstones')).mappings().one())
    if missing_created:
        db.execute(text('UPDATE timelapses SET created_at=NULL'))
    db.commit()
    now = ORIGIN + timedelta(days=30)
    assert not expired(db, 1, now=now)
    assert expired(db, 2, now=now)
    assert pending_count(db, now=now) == 1
    acknowledge(PurgeAcknowledgement(tombstones=[snapshot]), actor(), db)
    assert not preview_acknowledged(db, 1)
    assert preview_acknowledged(db, 2)
    assert pending_count(db, now=now) == 0
    assert audit_allowed(db, actor(2), 'job', first_id, public_audit)
    assert not audit_allowed(db, actor(2), 'job', second_id, private_audit)
    visible, params = audit_visibility(actor(2), db)
    assert db.execute(text("SELECT id FROM audit_logs WHERE action='job.updated' AND " + visible), params).scalars().all() == [public_audit]
    rows = [{'id': identifier, 'entity_type': 'job', 'entity_id': first_id if identifier == public_audit else second_id, 'details': 'instance'}
            for identifier in (public_audit, private_audit)]
    assert [r['details'] for r in redact_export(db, actor(2), rows)] == ['instance', '[controlled]']
    # Run the real purge worker; the public predecessor is never selected.
    public_file = tmp_path / 'public.mp4'
    public_file.write_bytes(b'public media')
    db.execute(text("UPDATE timelapses SET filename='public.mp4' WHERE id=1"))
    db.commit()
    purge_due(db.get_bind(), tmp_path)
    assert public_file.read_bytes() == b'public media'
    assert db.execute(text('SELECT id FROM timelapses WHERE id=1')).scalar_one() == 1


@pytest.mark.parametrize('source', ['snapshot', 'live', 'anchor'])
@pytest.mark.parametrize('timestamps', ['created', 'completed', 'neither'])
def test_nullable_media_timestamp_notice_parity(clock_db, source, timestamps):
    db = clock_db
    add_job(db, 5, created=ORIGIN)
    captured = None
    if source == 'anchor':
        captured = 1
        db.execute(text("INSERT INTO print_files(id,filename) VALUES(1,'part.3mf')"))
        add_rule(db, 'files', 1, days=7)
    else:
        add_rule(db, 'jobs', 5, days=7)
    add_media(db, 1, 5, ORIGIN, captured=captured)
    if source == 'snapshot':
        delete_job(db, 5)
    if timestamps != 'created':
        db.execute(text('UPDATE timelapses SET created_at=NULL'))
    if timestamps == 'neither':
        db.execute(text('UPDATE timelapses SET completed_at=NULL'))
    db.commit()
    assert bool(policies(db, 'job', 5)) == (source != 'anchor')
    for offset in (-1, 0, 1):
        now = ORIGIN + timedelta(days=7, microseconds=offset)
        expected = timestamps != 'neither' and offset >= 0
        assert expired(db, 1, now=now) == expected
        assert pending_count(db, now=now) == int(expected)
    table = {'snapshot': 'controlled_job_tombstones', 'live': 'controlled_jobs', 'anchor': 'controlled_files'}[source]
    db.execute(text(f'UPDATE {table} SET purge_previewed_at=:now'), {'now': ORIGIN.isoformat()})
    assert preview_acknowledged(db, 1)
    assert pending_count(db, now=ORIGIN + timedelta(days=30)) == 0


def test_obs_camera_allows_finished_controlled_print_but_refuses_active(marked):
    from tests.test_contracts.test_cert_B_camera_token_scope import _store_token
    from tests.test_contracts.test_controlled_round3 import finished_print
    from modules.printers.camera_routes import overlay_camera_principal
    from modules.printers.models import Printer
    finished_print(marked)
    marked.execute(text("UPDATE printers SET camera_enabled=TRUE,is_active=TRUE,camera_url='rtsp://192.0.2.1/camera' WHERE id=1"))
    token = _store_token(marked, 2, ['read:cameras'])
    printer = marked.get(Printer, 1)
    assert policies(marked, 'printer_names', 1)  # Mutating the gate to this must fail.
    assert overlay_camera_principal(marked, token, printer)['id'] == 2
    marked.execute(text("UPDATE print_jobs SET status='running' WHERE id=50"))
    marked.commit()
    with pytest.raises(HTTPException) as error:
        overlay_camera_principal(marked, token, printer)
    assert error.value.status_code == 403
    assert error.value.detail['code'] == 'controlled_part_group_required'
    marked.execute(text("UPDATE print_jobs SET status='completed' WHERE id=50"))
    marked.commit()
    assert overlay_camera_principal(marked, token, printer)['id'] == 2
