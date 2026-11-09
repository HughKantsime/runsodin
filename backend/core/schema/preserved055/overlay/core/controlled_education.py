"""Education cannot acquire aliases of controlled stored bytes, even for members."""
from sqlalchemy import text
from core.errors import OdinError, ErrorCode
from core.controlled_parts import policies


def controlled_path(db, path):
    ids = db.execute(text('SELECT id FROM print_files WHERE stored_path=:path ORDER BY id'), {'path': str(path)}).scalars().all()
    return any(policies(db, 'file', identifier) for identifier in ids)


def assert_unmarked_upload_path(db, path):
    if controlled_path(db, path):
        raise OdinError(ErrorCode.not_found, "Upload authorization changed", status=404)
