"""Non-route helpers for models_crud.py: revision file storage and 3MF plate summaries.

Kept apart so models_crud.py stays under the route-file size limit. The revision
transaction and audit helpers stay in models_crud.py (test_audit_atomicity inspects them there).
"""

import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException


def _revision_directory(model_id):
    root = Path(os.environ.get("MODEL_REVISION_STORAGE_ROOT") or "/data/model_revisions")
    if not root.is_absolute():
        raise HTTPException(status_code=400, detail="Revision storage root must be an absolute path")
    root = root.resolve()
    expected = root / str(model_id)
    directory = expected.resolve()
    if directory != expected:
        raise HTTPException(status_code=400, detail="Invalid revision storage path")
    return directory


def _new_revision_file(model_id, number, filename, state):
    directory = _revision_directory(model_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Revalidate after directory creation; reject a model directory alias outside root.
    if directory != _revision_directory(model_id):
        raise HTTPException(status_code=400, detail="Invalid revision storage path")
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', filename or "file")
    destination = directory / f"v{number}_{uuid.uuid4().hex}_{safe_name}"
    stream = open(destination, "xb")
    state["new_file"] = destination
    return destination, stream


def _plate_summaries(path, indices=None):
    """Per-plate sellable pieces / grams / print time from the staged 3MF bytes (model-level
    fields describe plate 1). Restricted to the validated plate indices when given."""
    import zipfile
    from modules.models_library.threemf_parser import plate_summaries
    with zipfile.ZipFile(path) as archive:
        summaries = plate_summaries(archive)
    return [s for s in summaries if indices is None or s["plate_index"] in indices]
