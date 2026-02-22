# Spec: Path Traversal Sweep — cameras, vision, models

## Goal

Apply `realpath()` + boundary checks consistently across all file-serving and file-deletion endpoints that currently read paths from the database without validation. The correct pattern already exists in `routers/vision.py` for frame serving — apply it everywhere else.

## Correct Pattern (reference implementation)

```python
# From vision.py frame serving — copy this everywhere:
abs_path = os.path.realpath(os.path.join('/data/vision_frames', row.frame_path))
if not abs_path.startswith('/data/vision_frames/'):
    raise HTTPException(status_code=404, detail="Not found")
if not os.path.isfile(abs_path):
    raise HTTPException(status_code=404, detail="Not found")
```

## Items

### 1. Timelapse serve and delete (`backend/routers/cameras.py`, lines ~91 and ~104)

Both the serve and delete handlers construct:
```python
video_path = Path("/data/timelapses") / t.filename
```
`t.filename` comes from the DB. No boundary check.

**Fix for serve:**
```python
video_path = Path(os.path.realpath(Path("/data/timelapses") / t.filename))
if not str(video_path).startswith("/data/timelapses/"):
    raise HTTPException(status_code=404, detail="Not found")
if not video_path.is_file():
    raise HTTPException(status_code=404, detail="Timelapse not found")
```

**Fix for delete:**
Same `realpath()` check before `os.remove()`.

### 2. Vision training data export (`backend/routers/vision.py`, line ~556)

In the training data ZIP export, frames are bundled using:
```python
frame_abs = os.path.join('/data/vision_frames', row.frame_path)
```
No `realpath()` check. If `row.frame_path` contains `../`, arbitrary files could be bundled into the export.

**Fix:**
```python
frame_abs = os.path.realpath(os.path.join('/data/vision_frames', row.frame_path))
if not frame_abs.startswith('/data/vision_frames/'):
    continue  # skip corrupted or injected entries
if not os.path.isfile(frame_abs):
    continue
```

### 3. Model revision revert (`backend/routers/models.py`, line ~844)

The revert endpoint reads `target.file_path` from `model_revisions` table:
```python
if os.path.exists(target.file_path):
    shutil.copy2(target.file_path, current_file_path)
```
`target.file_path` is not boundary-checked.

**Fix:**
```python
safe_path = os.path.realpath(target.file_path)
if not safe_path.startswith('/data/'):
    raise HTTPException(status_code=400, detail="Invalid file path")
if os.path.exists(safe_path):
    shutil.copy2(safe_path, current_file_path)
```

### 4. Model revision upload — add size limit (`backend/routers/models.py`, line ~806)

The revision upload endpoint has no read size limit:
```python
content = await file.read()
```
The primary upload endpoint already defines `MAX_UPLOAD_BYTES = 100 * 1024 * 1024`. Reuse it:

```python
content = await file.read(MAX_UPLOAD_BYTES + 1)
if len(content) > MAX_UPLOAD_BYTES:
    raise HTTPException(status_code=413, detail="File exceeds 100 MB limit")
```

### 5. ONNX model upload — add size limit (`backend/routers/vision.py`, line ~325)

```python
content = await file.read()  # no limit
```

**Fix:**
```python
MAX_ONNX_BYTES = 500 * 1024 * 1024  # 500 MB — ONNX models can be large
content = await file.read(MAX_ONNX_BYTES + 1)
if len(content) > MAX_ONNX_BYTES:
    raise HTTPException(status_code=413, detail="Model file exceeds 500 MB limit")
```

### 6. Backup restore — add size limit and stronger validation (`backend/routers/system.py`)

The restore endpoint reads the uploaded file with no size limit. Add:
```python
MAX_BACKUP_BYTES = 100 * 1024 * 1024  # 100 MB
content = await file.read(MAX_BACKUP_BYTES + 1)
if len(content) > MAX_BACKUP_BYTES:
    raise HTTPException(status_code=413, detail="Backup file too large")
```

Also scan for unexpected triggers/views in `sqlite_master` after the existing integrity check:
```python
# After integrity check passes:
with sqlite3.connect(tmp_path) as conn:
    triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    if triggers:
        raise HTTPException(status_code=400, detail="Backup contains unexpected database triggers")
```

### 7. Detection label_class validation (`backend/routers/vision.py`, line ~511)

The label endpoint accepts `label_class` from request body without allowlist validation:
```python
det.detection_type = request.label_class
```

**Fix:**
```python
VALID_DETECTION_TYPES = {"spaghetti", "first_layer_failure", "detachment", "false_positive"}
if request.label_class not in VALID_DETECTION_TYPES:
    raise HTTPException(status_code=400, detail=f"Invalid detection type. Must be one of: {', '.join(sorted(VALID_DETECTION_TYPES))}")
```

## Acceptance Criteria

- [ ] Timelapse serve: `realpath()` + prefix check before sending file
- [ ] Timelapse delete: `realpath()` + prefix check before removing file
- [ ] Vision training export: skips entries with paths outside `/data/vision_frames/`
- [ ] Model revision revert: `realpath()` + `/data/` prefix check
- [ ] Model revision upload: 100 MB size limit
- [ ] ONNX upload: 500 MB size limit
- [ ] Backup restore: 100 MB size limit + trigger scan
- [ ] Detection label_class: validated against allowlist
- [ ] `make test` passes

## Technical Notes

- `os.path.realpath()` resolves symlinks and `..` components — always use this, not `os.path.abspath()`
- The timelapse paths: check `Path` usage — use `str(os.path.realpath(video_path))` for comparison
- Find the revision upload handler by searching for `model_revisions` INSERT in `models.py`
- Find the ONNX upload handler by searching for `.onnx` in `vision.py`
- Find the backup restore handler by searching for `RESTORE` or `restore_backup` in `system.py`
