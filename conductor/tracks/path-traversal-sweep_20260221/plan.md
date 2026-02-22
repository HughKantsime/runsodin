# Plan: Path Traversal Sweep

## Overview

Apply `realpath()` boundary checks and file size limits across 3 backend routers (cameras.py, vision.py, models.py, system.py). All 7 items are well-specified with exact code patterns in spec.md.

## Tasks

- [x] 1.1 — cameras.py: timelapse serve — add `os.path.realpath()` + prefix check
- [x] 1.2 — cameras.py: timelapse delete — add `os.path.realpath()` + prefix check
- [x] 2.1 — vision.py: training export — add `os.path.realpath()` + prefix check per frame
- [x] 3.1 — models.py: revision revert — add `os.path.realpath()` + `/data/` prefix check
- [x] 4.1 — models.py: revision upload — add 100 MB size limit reusing `MAX_UPLOAD_BYTES`
- [x] 5.1 — vision.py: ONNX upload — add 500 MB size limit
- [x] 6.1 — system.py: backup restore — add 100 MB size limit + trigger scan
- [x] 7.1 — vision.py: label_class — add allowlist validation

## DAG

```
1.1 → done (independent)
1.2 → done (independent)
2.1 → done (independent)
3.1 → done (independent)
4.1 → done (independent)
5.1 → done (independent)
6.1 → done (independent)
7.1 → done (independent)
```

All tasks are independent. Execute sequentially for safety.
