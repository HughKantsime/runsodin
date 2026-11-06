"""Queue-time exact payload verification; never sends printer commands."""
from contextlib import contextmanager
import hmac
from pathlib import Path
import re

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from core.print_payload import validate_plate
from core.print_payload_snapshot import (
    PayloadUnavailable, hash_regular_payload, stage_payload,
)

FILE_FIELDS = (
    'id', 'model_id', 'org_id', 'job_id', 'stored_path', 'file_hash', 'filename',
    'original_filename', 'compatible_api_types', 'filaments_json', 'project_name',
    'print_time_seconds', 'bed_x_mm', 'bed_y_mm',
)
TARGET_FIELDS = ('id', 'org_id', 'shared', 'api_type', 'is_active', 'machine_type',
                 'bed_x_mm', 'bed_y_mm')


def lock_queue_reference(db, table, identifier, fields):
    """Called after the file writer fence; later locks never wait on PostgreSQL."""
    if table not in ('models', 'printers'):
        raise ValueError('Unsupported queue reference')
    permitted = ('id', 'org_id') if table == 'models' else TARGET_FIELDS
    if tuple(fields) != permitted:
        raise ValueError('Unsupported queue reference fields')
    suffix = ' FOR UPDATE NOWAIT' if db.get_bind().dialect.name == 'postgresql' else ''
    try:
        return db.execute(text(f"SELECT {','.join(fields)} FROM {table} WHERE id=:id{suffix}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table checked against the ('models','printers') allowlist and fields against the permitted tuple; values are bound
                          {'id': identifier}).first()
    except OperationalError as exc:
        if getattr(exc.orig, 'pgcode', None) == '55P03' or getattr(exc.orig, 'sqlstate', None) == '55P03':
            raise HTTPException(409, 'Queue reference is busy; refresh before retrying') from None
        raise


def capture(row, fields):
    return {field: row._mapping[field] for field in fields} if row is not None else None


def assert_unchanged(db, table, captured):
    if captured is None:
        return
    permitted = FILE_FIELDS if table == 'print_files' else (('id', 'org_id') if table == 'models' else TARGET_FIELDS)
    if table not in ('print_files', 'models', 'printers') or tuple(captured) != permitted:
        raise ValueError('Unsupported queue comparison')
    current = db.execute(text(f"SELECT {','.join(permitted)} FROM {table} WHERE id=:id"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table checked against the ('print_files','models','printers') allowlist and columns against the permitted tuple; values are bound
                         {'id': captured['id']}).first()
    if current is None or capture(current, permitted) != captured:
        raise HTTPException(409, 'Queue source changed; refresh before retrying')


def file_snapshot_predicate(captured):
    if tuple(captured) != FILE_FIELDS:
        raise ValueError('Unsupported file snapshot')
    integers = {'id', 'model_id', 'org_id', 'job_id', 'print_time_seconds'}
    decimals = {'bed_x_mm', 'bed_y_mm'}
    clauses = []
    parameters = {}
    for field, value in captured.items():
        kind = 'INTEGER' if field in integers else ('DOUBLE PRECISION' if field in decimals else 'TEXT')
        parameter = 'queue_' + field
        clauses.append(f'({field}=:{parameter} OR ({field} IS NULL AND CAST(:{parameter} AS {kind}) IS NULL))')
        parameters[parameter] = value
    return ' AND '.join(clauses), parameters


def assert_payload_unchanged(path, digest):
    try:
        if not hmac.compare_digest(hash_regular_payload(path), digest):
            raise HTTPException(409, 'Print payload changed during scheduling')
    except PayloadUnavailable as exc:
        raise HTTPException(400, str(exc)) from None


@contextmanager
def verified_queue_payload(row, target):
    path = row.stored_path
    suffix = Path(path or '').suffix.lower()
    api = target.api_type if target is not None else ('bambu' if suffix == '.3mf' else '')
    if suffix not in ('.3mf', '.gcode', '.bgcode'):
        raise HTTPException(400, 'Unsupported print payload format')
    if target is not None:
        if api not in ('bambu', 'moonraker', 'prusalink') or not target.is_active:
            raise HTTPException(400, 'Selected printer does not support dispatch')
        allowed = [value.strip() for value in (row.compatible_api_types or '').split(',') if value.strip()]
        compatible = {'bambu': ('.3mf',), 'moonraker': ('.gcode',), 'prusalink': ('.gcode', '.bgcode')}
        if suffix not in compatible[api] or (allowed and api not in allowed):
            raise HTTPException(400, 'Print file is incompatible with the selected printer API')
        if all(value is not None for value in (row.bed_x_mm, row.bed_y_mm, target.bed_x_mm, target.bed_y_mm)):
            if row.bed_x_mm > target.bed_x_mm + 2 or row.bed_y_mm > target.bed_y_mm + 2:
                raise HTTPException(400, 'Print file exceeds the selected printer bed')
    try:
        digest = hash_regular_payload(path)
        if row.file_hash is not None:
            if not isinstance(row.file_hash, str) or not re.fullmatch('[0-9a-f]{64}', row.file_hash):
                raise HTTPException(409, 'Stored print payload hash is invalid')
            if not hmac.compare_digest(digest, row.file_hash):
                raise HTTPException(409, 'Print payload changed; refresh before scheduling')
        with stage_payload(path, digest) as snapshot:
            validate_plate(snapshot.path, 0, api)
            if not hmac.compare_digest(hash_regular_payload(path), snapshot.sha256):
                raise HTTPException(409, 'Print payload changed during scheduling')
            yield snapshot.sha256
    except PayloadUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
