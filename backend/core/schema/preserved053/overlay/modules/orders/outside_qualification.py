"""Owner-attested qualification and binding in one local transaction."""
from pathlib import PurePosixPath
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import text
from core.rbac import is_interactive_session
from core.interfaces.education_policy import assert_generic_model_reference_allowed
from modules.orders import batch_production as batches, outside_prints, square_store
from modules.orders.batch_schemas import OutsideBindingInput


def print_file(db, owner, row, data):
    # Scope before matching; filenames from another tenant must not affect ambiguity.
    files = db.execute(text('''SELECT f.* FROM print_files f JOIN models m ON m.id=f.model_id
        WHERE ((:owner=0 AND f.org_id IS NULL AND m.org_id IS NULL)
            OR (f.org_id=:owner AND m.org_id=:owner))'''), {'owner': owner}).mappings().all()
    names = {PurePosixPath(v.replace('\\', '/')).name.casefold()
             for v in (row['filename'], row['job_name']) if v}
    candidates = [f for f in files if any(v and PurePosixPath(v.replace('\\', '/')).name.casefold() in names
                  for v in (f['filename'], f['original_filename']))]
    if data.print_file_id is not None:
        matches = [f for f in files if f['id'] == data.print_file_id]
        if not matches:
            raise HTTPException(404, 'Print file not found')
        if candidates and not any(f['id'] == data.print_file_id for f in candidates):
            batches.fail('Explicit print_file_id must match an observed filename candidate')
    else:
        matches = candidates
        if len(matches) != 1:
            batches.fail('Observed filename is missing or ambiguous. Choose an explicit print_file_id')
    file = matches[0]
    assert_generic_model_reference_allowed(db, file['model_id'])
    digest = file['file_hash'] or ''
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        batches.fail('Print file needs a retained SHA-256 hash')
    if data.plate_index >= (file['plate_count'] or 1):
        batches.fail('Choose a plate in this print file')
    # Serialize deletion/reassignment of the selected file and model.
    for table in ('models', 'print_files'):
        identifier = file['model_id'] if table == 'models' else file['id']
        statement = ('UPDATE models SET id=id WHERE id=:id AND ((:owner=0 AND org_id IS NULL) OR org_id=:owner)'
                     if table == 'models' else
                     'UPDATE print_files SET id=id WHERE id=:id AND model_id=:model AND file_hash=:hash AND ((:owner=0 AND org_id IS NULL) OR org_id=:owner)')
        if db.execute(text(statement), {'id':identifier,'owner':owner,'model':file['model_id'],'hash':digest}).rowcount != 1:
            batches.fail('Print file ownership or revision changed; reload before qualifying')
    assert_generic_model_reference_allowed(db, file['model_id'])
    if db.execute(text('SELECT 1 FROM education_submissions WHERE print_file_id=:file OR model_id=:model'),
                  {'file':file['id'],'model':file['model_id']}).first():
        batches.fail('Education files require the Education submission workflow')
    return {'model_id':file['model_id'],'print_file_id':file['id'],
            'payload_sha256':digest,'plate_index':data.plate_index}


def assert_no_uncorrelated_dispatch(db, row):
    # Compare instants in UTC, not SQLite text spellings (which may include
    # offsets or fractional seconds). These receipts cannot prove an outside start.
    def stamp(value):
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    receipts = db.execute(text('''SELECT created_at FROM dispatch_attempt_receipts
        WHERE printer_id=:printer AND observed_print_job_id IS NULL'''),
        {'printer':row['printer_id']}).scalars()
    try:
        start, end = stamp(row['started_at']), stamp(row['ended_at'])
        if start > end:
            batches.fail('Observed print time window is invalid')
        if any(start <= stamp(created) <= end for created in receipts):
            batches.fail('An uncorrelated dispatch receipt overlaps this print; investigate before qualification')
    except (ValueError, TypeError):
        batches.fail('Observed print or dispatch time is unavailable; investigate before qualification')


def qualify_and_bind(db, user, print_job_id, data):
    # Shane, 2026-10-28: a write:production operator token may qualify outside
    # prints, as for ODIN-dispatched references. Sessions stay admin and interactive.
    from modules.orders.production_auth import assert_production_operator
    token = user.get('_auth_kind') == 'user_api_token'
    if token:
        assert_production_operator(user)
    elif user.get('role') != 'admin' or not is_interactive_session(user):
        raise HTTPException(403, 'Recipe qualification requires an admin password session')
    if not data.qualification_confirmed:
        batches.fail('Confirm the material setup and fixed yield before qualifying')
    owner = square_store.tenant(user)
    try:
        square_store.lock_owner(db, owner, user)
        from modules.orders.finishing import lock
        lock(db, owner, user)
        row = outside_prints.physical(db, owner, print_job_id, lock=True)
        if row['printer_id'] != data.printer_id:
            batches.fail('Selected printer does not match the observed print')
        if db.execute(text('SELECT 1 FROM system_config WHERE key=:key'),
                      {'key':outside_prints.PREFIX+str(print_job_id)}).first():
            batches.fail('This outside print is already bound')
        # Qualification is stricter than legacy bind restoration: any ODIN
        # dispatch receipt on a linked job requires the normal qualification path.
        if row['scheduled_job_id'] is not None and db.execute(text(
                'SELECT 1 FROM dispatch_attempt_receipts WHERE job_id=:id'),
                {'id':row['scheduled_job_id']}).first():
            batches.fail('ODIN-dispatched jobs must use the normal qualification path')
        assert_no_uncorrelated_dispatch(db, row)
        payload = print_file(db, owner, row, data)
        evidence = {**payload,'printer_id':row['printer_id'],
                    'stable_configuration_key':outside_prints.configuration_key(db,row['printer_id']),
                    'observation_uid':row['economic_birth_nonce']}
        confirmation = {'actor_user_id':user['id'],'confirmed_at':batches.now(),
                        'auth_kind':user.get('_auth_kind'),'actor_token_id':user.get('_token_id'),
                        'print_job_id':print_job_id,'attestation':data.attestation.model_dump(),
                        'matched_by':'explicit' if data.print_file_id is not None else 'filename',
                        'observed_filename':row['filename'],'observed_job_name':row['job_name']}
        recipe = batches.stage_recipe(db, owner, data, evidence, {
            'reference_job_id':None,'reference_claim_token':None,
            'qualified_from_outside_print':True,'outside_print_evidence':confirmation})
        outside_prints.audit(db,'production.recipe_qualified_from_outside_print','print_job',
                            print_job_id,{**confirmation,'recipe_id':recipe['id']})
        # An interactive admin session is the owner attesting in the app; a token
        # relays the owner's confirmation through the operator.
        return outside_prints.bind(db, user, print_job_id, OutsideBindingInput(
            recipe_id=recipe['id'],attestation=data.attestation,
            attested_by='owner_confirmed_to_operator' if token else 'owner_in_app'))
    except Exception:
        db.rollback()
        raise
