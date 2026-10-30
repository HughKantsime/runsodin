"""Read-only verified payload metadata beside tenant-authorized live slot state."""
from defusedxml import ElementTree
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from core.db import get_db
from core.zip_bounds import open_bounded
from core.print_payload_snapshot import stage_payload
from modules.organizations.education_access import require_education_principal, is_superadmin, require_tenant_admin
from modules.organizations.education_review_service import _reviewable_submission, assert_file_preview_allowed

router = APIRouter(prefix='/education/submissions', tags=['Education'])


@router.get('/{submission_id}/slice-info')
def slice_info(submission_id: int, printer_id: int | None = Query(default=None, gt=0),
               principal=Depends(require_education_principal()), db=Depends(get_db),
               org_id: int | None = None):
    if is_superadmin(principal):
        # Read-only platform inspection requires an explicit school. Mutation
        # endpoints retain their original school-account authority checks.
        principal = {**principal, 'group_id': require_tenant_admin(principal, org_id)}
    elif org_id is not None and org_id != principal.get('group_id'):
        raise HTTPException(404, 'Organization not found')
    row = _reviewable_submission(db, submission_id=submission_id, principal=principal)
    assert_file_preview_allowed(db, file_id=row.print_file_id, principal=principal)
    file = db.execute(text('SELECT f.stored_path,f.file_hash,f.storage_bytes,j.plate_index '
        'FROM print_files f JOIN jobs j ON j.id=:job WHERE f.id=:file AND f.org_id=:org '
        "AND f.blob_state='present'"), {'job': row.job_id, 'file': row.print_file_id, 'org': row.org_id}).one()
    slots = []
    if printer_id is not None:
        allowed = db.execute(text('SELECT 1 FROM education_cost_center_printers e JOIN printers p '
            'ON p.id=e.printer_id AND p.org_id=e.org_id WHERE e.org_id=:org AND e.cost_center_id=:center '
            "AND e.printer_id=:printer AND e.state='active' AND p.is_active IS TRUE AND p.shared IS NOT TRUE"),
            {'org': row.org_id, 'center': row.cost_center_id, 'printer': printer_id}).first()
        if not allowed:
            raise HTTPException(404, 'Printer not found')
        slots = [dict(r) for r in db.execute(text('SELECT slot_number,filament_type,color_hex,loaded_at '
            'FROM filament_slots WHERE printer_id=:printer ORDER BY slot_number'), {'printer': printer_id}).mappings()]
    # Capture authority in the read transaction; metadata never grants dispatch authority.
    path, digest, size, selected = file.stored_path, file.file_hash, file.storage_bytes, file.plate_index or 0
    db.rollback()
    try:
        with stage_payload(path, digest) as snapshot:
            import os
            if os.path.getsize(snapshot.path) != size:
                raise ValueError()
            with open_bounded(snapshot.path) as package:
                member = package.getinfo('Metadata/slice_info.config')
                if member.file_size > 1024 * 1024:
                    raise ValueError()
                root = ElementTree.fromstring(package.read(member))
                plates = []
                plate_nodes = root.findall('plate')
                for plate in plate_nodes:
                    metadata = {m.get('key'): m.get('value') for m in plate.findall('metadata')}
                    index = metadata.get('index') or ('1' if len(plate_nodes) == 1 else None)
                    if not index or not index.isdecimal() or int(index) < 1:
                        raise ValueError()
                    if package.getinfo(f'Metadata/plate_{int(index)}.gcode').file_size <= 0:
                        raise ValueError()
                    plates.append({'plate': index, 'estimated_seconds': metadata.get('prediction'),
                                   'filaments': [{'source_slot': f.get('id'), 'material': f.get('type'), 'color': f.get('color')} for f in plate.findall('filament')]})
        return {'plates': plates, 'selected_plate_index': selected, 'slots': slots,
                'freshness': 'Last recorded slot state; timestamps are observations, not a fresh printer read'}
    except Exception:
        raise HTTPException(409, 'Verified slice metadata unavailable; reload or inspect the source file') from None
