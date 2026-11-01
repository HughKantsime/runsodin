"""Import sliced 3MF plates as separate bound G-code files, never print them."""
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from core.base import FilamentType
from core.db import get_db
from core.db_compat import execute_insert_returning_id
from core.dependencies import log_audit
from core.errors import OdinError
from core.print_file_binding import bind_file
from core.rate_limit import limiter
from core.rbac import require_action
from modules.models_library import mesh_isolation, plate_import
from modules.models_library.upload_storage import _fresh_upload_actor, _new_upload_path

router = APIRouter(tags=['Print Files'])
log = logging.getLogger('odin.api')


def _target(db, printer_id, actor, *, lock=False):
    from modules.models_library.routes.print_files import _get_accessible_printer
    row = db.execute(text(
        'SELECT id,org_id,api_type,machine_type,is_active,bed_x_mm,bed_y_mm,nozzle_diameter '
        'FROM printers WHERE id=:id' + (' FOR UPDATE NOWAIT' if lock and db.get_bind().dialect.name == 'postgresql' else '')
    ), {'id': printer_id}).first()
    if row is None:
        raise HTTPException(404, 'Printer not found')
    # Match ordinary uploads, including explicitly shared foreign-org printers.
    # Recheck after the row lock, so visibility cannot change during persistence.
    _get_accessible_printer(db, printer_id, actor, allow_shared=True)
    return row


def _validate_filename(name):
    from core.interfaces.education_policy import is_education_reserved_filename
    if (not name or len(name) > 200 or Path(name).suffix.lower() != '.3mf'
            or '/' in name or '\\' in name or any(ord(c) < 32 for c in name)):
        raise HTTPException(400, 'Use a plain .3mf filename of at most 200 characters')
    try:
        name.encode('utf-8')
    except UnicodeError:
        raise HTTPException(400, 'Invalid filename text') from None
    if is_education_reserved_filename(name) or 'odin-dispatch-' in name.lower():
        raise HTTPException(400, 'Filename uses a reserved printer namespace')


def _persist(db, actor, printer_id, plates, original):
    """Short serialized batch: reauthorize, insert, bind, rename, commit once."""
    finals = []
    commit_started = False
    try:
        # This helper is async for historical reasons but performs synchronous DB
        # work only. This entire transaction runs on a worker, not the event loop.
        actor = asyncio.run(_fresh_upload_actor(db, actor))
        target = _target(db, printer_id, actor, lock=True)
        plate_import.check_target(plates, target)
        files = []
        for plate in plates:
            filename = plate_import.plate_filename(original, plate['number'])
            name = filename.removesuffix('.gcode')
            facts = plate['meta']['safety_facts']
            file_id = execute_insert_returning_id(db, '''
                INSERT INTO print_files(filename,original_filename,project_name,print_time_seconds,
                    total_weight_grams,filaments_json,file_hash,org_id,created_by,bed_x_mm,bed_y_mm,
                    nozzle_diameter,printer_model,compatible_api_types,compatibility_facts_json)
                VALUES(:filename,:filename,:name,:seconds,:grams,:filaments,:hash,:org,:actor,
                    :x,:y,:nozzle,:machine,:api,:facts)
            ''', {'filename': filename, 'name': name, 'seconds': plate['print_time_seconds'],
                  'grams': plate['total_weight_grams'], 'filaments': json.dumps(plate['filaments']),
                  'hash': plate['sha256'], 'org': actor.get('group_id'), 'actor': actor['id'],
                  'x': plate['meta']['bed_x_mm'], 'y': plate['meta']['bed_y_mm'],
                  'nozzle': facts['nozzle'].get('value'), 'machine': facts['machine'].get('value'),
                  # Explicit binding authorizes this target API, never all APIs.
                  'api': target.api_type, 'facts': json.dumps(facts, sort_keys=True)})
            # Match ordinary upload requirements without inventing missing facts.
            # check_target already restricts the plate to one used material slot.
            used = [f for f in plate['filaments'] if f['used_grams'] is None
                    or f['used_grams'] > 0 or (f['used_meters'] or 0) > 0]
            requirements = {}
            for filament in used:
                requirement = {}
                if filament['color'] is not None:
                    requirement['color'] = filament['color']
                if filament['used_grams'] is not None:
                    requirement['grams'] = filament['used_grams']
                requirements[f"slot{filament['slot']}"] = requirement
            raw_type = used[0]['type'] if used else None
            # The model column is an enum. Keep unsupported slicer labels only in
            # filaments_json rather than creating a model that ORM cannot read.
            filament_type = raw_type if raw_type in {t.value for t in FilamentType} else None
            if raw_type and filament_type is None:
                recognized = FilamentType.from_bambu_code(raw_type)
                if recognized != FilamentType.OTHER:
                    filament_type = recognized.value
            model_id = execute_insert_returning_id(db, '''
                INSERT INTO models(name,print_file_id,category,org_id,build_time_hours,
                    color_requirements,default_filament_type)
                VALUES(:name,:file,'Uploaded',:org,:hours,:requirements,:filament_type)
            ''', {'name': name, 'file': file_id, 'org': actor.get('group_id'),
                  'requirements': json.dumps(requirements), 'filament_type': filament_type,
                  'hours': plate['print_time_seconds'] / 3600 if plate['print_time_seconds'] is not None else None})
            final = _new_upload_path(file_id, filename)
            finals.append((plate['path'], final))
            db.execute(text('UPDATE print_files SET model_id=:model,stored_path=:path WHERE id=:id'),
                       {'model': model_id, 'path': str(final), 'id': file_id})
            bind_file(db, file_id, printer_id)
            log_audit(db, 'print_file_plates_imported', 'print_file', file_id,
                      {'source_plate': plate['number'], 'target_printer_id': printer_id})
            files.append({'id': file_id, 'model_id': model_id, 'filename': filename,
                          'source_plate': plate['number'], 'print_time_seconds': plate['print_time_seconds'],
                          'total_weight_grams': plate['total_weight_grams'], 'filaments': plate['filaments'],
                          'target_printer_id': printer_id})
        db.flush()
        for staged, final in finals:
            os.replace(staged, final)  # staging is on the same storage filesystem
        commit_started = True
        db.commit()
        return {'files': files, 'plate_count': len(files), 'target_printer_id': printer_id}
    except BaseException as exc:
        rollback_failed = False
        try:
            db.rollback()
        except Exception:
            rollback_failed = True
        if commit_started or rollback_failed:
            # A lost acknowledgement can mean committed rows. Preserve payloads
            # exactly as the existing upload route does, rather than orphan rows.
            log.warning('Plate import transaction outcome unknown; retained %s payloads', len(finals))
            raise HTTPException(500, 'Plate import outcome is uncertain. Contact support before retrying.') from None
        for _staged, final in finals:
            final.unlink(missing_ok=True)
        if isinstance(exc, (HTTPException, OdinError)):
            raise
        if isinstance(exc, OperationalError) and (
            getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '55P03'
            or getattr(getattr(exc, 'orig', None), 'pgcode', None) == '55P03'
            or 'database is locked' in str(getattr(exc, 'orig', ''))
        ):
            raise HTTPException(409, 'Another upload is in progress; retry this import') from None
        log.warning('Plate import failed (%s)', type(exc).__name__)
        raise HTTPException(500, 'Unable to save imported plates') from None


@router.post('/print-files/import-plates')
@limiter.limit('10/minute')
async def import_sliced_plates(
    request: Request,
    file: UploadFile = File(...),
    target_printer_id: int = Form(...),
    current_user: dict = Depends(require_action('upload.upload', scope='write:models')),
    db: Session = Depends(get_db),
):
    """Stage and save one exact file and model per plate. No printer commands."""
    _validate_filename(file.filename)
    _target(db, target_printer_id, current_user)
    # Generate paths using existing storage policy, never names from the archive.
    root = await run_in_threadpool(lambda: _new_upload_path(0, 'stage').parent)
    with tempfile.TemporaryDirectory(prefix='.plate-import-', dir=root) as directory:
        archive = Path(directory) / 'source.3mf'
        total = 0
        with archive.open('xb') as output:
            while chunk := await file.read(65536):
                total += len(chunk)
                if total > plate_import.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, 'Maximum plate import upload is 100 MiB')
                await run_in_threadpool(output.write, chunk)
        try:
            analysis = await mesh_isolation.import_plates_isolated(archive, directory)
        except mesh_isolation.Busy as busy:
            raise HTTPException(429, '3MF analysis is busy; try again shortly',
                                headers={'Retry-After': str(busy.retry_after)}) from None
        plates = await run_in_threadpool(plate_import.prepare_staged, analysis, directory)
        return await run_in_threadpool(_persist, db, current_user, target_printer_id, plates, file.filename)
