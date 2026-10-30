"""Immutable upload destination policy, shared by queue and dispatch owners.

File visibility must be checked by the caller before returning this policy.
"""
from sqlalchemy import text
from core.errors import ErrorCode, OdinError
from core.system_config_values import config_text, write_config_text


def target_printer(db, file_id):
    if file_id is None:
        return None
    row = db.execute(text("SELECT value FROM system_config WHERE key=:key"),
                     {"key": f"print_file_target:{file_id}"}).first()
    if row is None:
        return None
    value = config_text(row[0])
    if not value.isdecimal() or int(value) <= 0:
        raise OdinError(ErrorCode.resource_conflict, "Upload printer binding is invalid", status=409)
    return int(value)


def bind_file(db, file_id, printer_id):
    if printer_id is None:
        return
    if target_printer(db, file_id) is not None:
        raise OdinError(ErrorCode.resource_conflict, "Upload printer binding already exists", status=409)
    write_config_text(db, f"print_file_target:{file_id}", str(printer_id))


def require_bound_target(db, file_id, printer_id):
    target = target_printer(db, file_id)
    if target is not None and printer_id != target:
        raise OdinError(ErrorCode.resource_conflict,
                        f"Print file is bound to printer {target}; choose that printer", status=409)
    return target


def job_file_id(db, model_id, file_id=None):
    if file_id is not None or model_id is None:
        return file_id
    row = db.execute(text("SELECT print_file_id FROM models WHERE id=:id"), {"id": model_id}).first()
    if row and row[0] is not None:
        return row[0]
    return db.execute(text("SELECT id FROM print_files WHERE model_id=:id ORDER BY id DESC LIMIT 1"),
                      {"id": model_id}).scalar()
