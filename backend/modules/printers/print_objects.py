"""Authorized, receipt-proven objects of the running Bambu print. No printer I/O."""
from fastapi import HTTPException
from sqlalchemy import text

from core.controlled_parts import allowed
from core.display_names import display_names
from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL
from core.model_preview_access import accessible_preview_file
from core.rbac import _check_token_scope, _is_platform_superadmin, check_org_access
from core.rbac_policy import action_roles
from modules.models_library.services import stored_print_objects
from modules.printers import monitor_commands, support_levels
from modules.printers.printer_models import bambu_model_name
from modules.printers.routes_plate_thumbnail import _RUNNING_PRINT

BOTTOM_FRONT_MODELS = frozenset({'P2S'})

REASONS = {
    'not_printing': 'Nothing is printing on this printer.',
    'printer_type_unsupported': 'ODIN can skip objects on Bambu Lab printers. This printer is a different type.',
    'started_outside_odin': "ODIN did not start this print from one of its stored files, so it cannot list its objects. Skip the object at the printer or in the printer maker's app.",
    'dispatch_not_proven': 'ODIN cannot prove which stored file this print came from, so it cannot list its objects.',
    'file_unavailable': 'ODIN does not have a readable sliced file for this print, so it cannot list its objects.',
    'preparing': 'ODIN is reading the sliced file. Try again in a few seconds.',
    'no_object_list': 'The sliced file does not list its objects, so ODIN cannot tell the printer which one to skip.',
    'labels_off': 'This file was sliced without object labels, so the printer cannot skip objects in it. Turn on Label objects in the slicer for the next print.',
    'single_object': 'This print has one object. To end it, use Stop.',
    'too_many_objects': 'This plate has {n} objects. Bambu printers can skip objects only on plates with 64 or fewer.',
    'firmware_unsupported': "This printer's firmware does not report support for skipping objects.",
    'experimental_not_enabled': support_levels.EXPERIMENTAL_MESSAGE,
}
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- shared source-owned selection fragment; bound job id
_JOB = text('SELECT ' + SELECTED_PRINT_FILE_ID_SQL + ' AS file_id, j.plate_index, j.payload_sha256, j.item_name '
            'FROM jobs j LEFT JOIN models m ON m.id=j.model_id '
            'LEFT JOIN education_submissions s ON s.job_id=j.id WHERE j.id=:job ORDER BY s.id LIMIT 1')
_RECEIPT = text('SELECT r.claim_token, c.payload_sha256, c.token AS retained_token FROM dispatch_attempt_receipts r '
                'LEFT JOIN printer_dispatch_claims c ON c.token=r.claim_token '
                'AND c.printer_id=r.printer_id AND c.job_id=r.job_id '
                'WHERE r.printer_id=:pid AND r.observed_print_job_id=:observed AND r.job_id=:job')


def _may_skip(db, printer, principal):
    if not check_org_access(principal, printer.org_id):
        return False
    if not _is_platform_superadmin(principal) and principal.get('role') not in action_roles(db, 'printers.edit'):
        return False
    try:
        _check_token_scope(principal, 'write:printers')
        return True
    except HTTPException:
        return False


def running_print_objects(db, printer, principal):
    gated = 'skip_objects' in support_levels.gated_capabilities(printer.api_type)
    result = dict(printer_id=printer.id, supported=False, reason_code=None, reason=None,
                  retry_after_seconds=None, experimental=gated, source=None, print=None,
                  skipped_reported=False, current_object_id=None, can_skip=False, max_selectable=0,
                  picture=dict(available=False, aspect=None, front_edge=None), objects=[])

    def refuse(code, **values):
        result.update(reason_code=code, reason=REASONS[code].format(**values))
        return result

    running = db.execute(_RUNNING_PRINT, {'pid': printer.id}).first()
    if running is None:
        return refuse('not_printing')
    result['print'] = dict(print_job_id=running.id, job_id=running.scheduled_job_id, name=None, plate=None)
    if support_levels.normalize(printer.api_type) != 'bambu':
        return refuse('printer_type_unsupported')
    if gated and not support_levels.experimental_enabled(db, printer.id, printer.api_type):
        refuse('experimental_not_enabled')
        result['reason'] = support_levels.experimental_capability_message(printer.api_type, 'skip_objects')
        return result
    if running.scheduled_job_id is None:
        # Restart answers and an unmatched admitted attempt are not proof of an outside start.
        hint = db.execute(text('SELECT 1 FROM system_config WHERE key=:restart '
                               'UNION ALL SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:pid LIMIT 1'),
                          {'restart': f'restart_print:{running.id}', 'pid': printer.id}).first()
        return refuse('dispatch_not_proven' if hint else 'started_outside_odin')
    receipt = db.execute(_RECEIPT, {'pid': printer.id, 'observed': running.id, 'job': running.scheduled_job_id}).first()
    if receipt is None:
        return refuse('dispatch_not_proven')
    job = db.execute(_JOB, {'job': running.scheduled_job_id}).first()
    if job is None or job.file_id is None or not allowed(db, 'print_job', running.id, principal):
        return refuse('file_unavailable')
    try:
        row = accessible_preview_file(db, job.file_id, principal)
    except HTTPException:
        return refuse('file_unavailable')
    sent_hash = receipt.payload_sha256 if receipt.retained_token is not None else job.payload_sha256
    if not sent_hash or row.file_hash != sent_hash:
        return refuse('file_unavailable')
    document, retry = stored_print_objects(row)
    if retry is not None:
        result['retry_after_seconds'] = retry
        return refuse('preparing')
    if document is None:
        return refuse('file_unavailable')
    number = int(job.plate_index or 0) + 1
    plate = next((p for p in document['plates'] if p['index'] == number), None)
    if plate is None or not plate['objects']:
        return refuse('no_object_list')
    if not plate['label_object_enabled']:
        return refuse('labels_off')
    count = len(plate['objects'])
    if count == 1:
        return refuse('single_object')
    if count > 64:
        return refuse('too_many_objects', n=count)
    state = monitor_commands.request_skip_state(printer.id)
    skipped = None
    if state is not None:
        if state['fun'] is not None:
            try:
                supported = bool(int(state['fun'], 16) & (1 << 49))
            except ValueError:
                supported = False
            if not supported:
                return refuse('firmware_unsupported')
        if state['plate_idx'] is not None and state['plate_idx'] != number:
            return refuse('file_unavailable')
        if state['print_job_id'] == running.id:
            skipped = state['skipped']
    objects = [dict(id=str(o['identify_id']), number=i, name=o['name'],
                    state='skipped' if o['identify_id'] in (skipped or []) else 'printing',
                    skippable=True, bounds=o['bounds'], outline=None)
               for i, o in enumerate(plate['objects'], 1)]
    picture_available = all(o['bounds'] is not None for o in objects)
    front_edge = 'bottom' if picture_available and bambu_model_name(printer.model) in BOTTOM_FRONT_MODELS else None
    result.update(supported=True, source='bambu_3mf', skipped_reported=skipped is not None,
                  can_skip=_may_skip(db, printer, principal), objects=objects,
                  max_selectable=max(0, sum(o['state'] == 'printing' for o in objects) - 1),
                  picture=dict(available=picture_available, aspect=plate['aspect'], front_edge=front_edge))
    result['print'].update(name=display_names(db, 'job', [running.scheduled_job_id], principal).get(running.scheduled_job_id), plate=number)
    return result
