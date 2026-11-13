# Vigil evaluation tools

Developer tooling for judging a Vigil detection model on a frozen evaluation set. Not
product code: nothing here is imported by the backend or shipped in the image.

Python 3.11. Uses the standard library plus `numpy`, `opencv-python-headless` and
`onnxruntime`, which are already in `backend/requirements.txt` (the same pins the
product runs). Run from the repository root; `score.py` and `replay.py` load the
product's `backend/modules/vision/output_contract.py` by file path.

## Privacy rule

- Frames never leave the machine. The scripts read local files and write local
  reports; they open no network connections (a test checks the source for network
  libraries).
- Controlled work and excluded work: the product does not capture training frames for
  controlled parts (`detection_thread._training_capture_allowed`) or for jobs whose
  model category is `no-training` (`_training_job_excluded`), and the export skips rows
  the exporting user may not see (`training_service.export_frames`). These tools do no
  filtering of their own, so only use an export produced that way.
- The evaluation manifest must never be trained on. `exclude_from_training.txt` lists
  every frame in it; give that list to whatever builds the training set.

## 1. Build the evaluation set

The export zip has `images/detection_<id>.jpg` and `labels/detection_<id>.txt` only. It
has no printer id and no capture time, so pass an index to get them:

```
# sqlite:   sqlite3 -header -csv <db> "SELECT id, printer_id, created_at FROM vision_detections WHERE frame_path IS NOT NULL" > index.csv
# postgres: \copy (SELECT id, printer_id, created_at FROM vision_detections WHERE frame_path IS NOT NULL) TO 'index.csv' CSV HEADER
```

Extract the zip into a new, empty directory (the scoring step needs the images on disk),
then:

```
mkdir eval_frames && unzip -q vigil-export.zip -d eval_frames
python tools/vigil_eval/make_eval_set.py --source eval_frames --index index.csv \
    --out evalset/eval.json --seed 1 --max-frames 400
```

`--source` also accepts the zip itself. Without `--index`, printer ids are `unknown` and
selection is only stratified by class. Output: `evalset/eval.json` and
`evalset/exclude_from_training.txt`. An existing manifest is never overwritten without
`--force`. Selection is deterministic: same export, index and seed give the same
manifest. Strata are (printer, class); negatives (empty label file) are their own class.
Strata are drawn round-robin, so rare printers and classes are not swamped.

## 2. Score a model

```
python tools/vigil_eval/score.py --model model.onnx --manifest evalset/eval.json \
    --frames-root eval_frames --conf 0.5 --iou 0.5 --out report_a.json
```

Reports true positives, false positives, false negatives, precision and recall overall,
per class and per printer, and `false_alarms_per_100_frames` on negative frames. A
model that is not the legacy Obico layout needs `--contract contract.json` (the
`vision_models.metadata_json` content). For a single-class Obico model the class is
named by `--single-class-name` (default `spaghetti`). Exit code 1 if any frame could
not be scored (the report is still written).

## 3. Replay a sequence

```
python tools/vigil_eval/replay.py --model model.onnx --sequence clean_print_frames/ \
    --interval-sec 10 --detection-type spaghetti --assume-no-failures --out replay_a.json
```

Frames are taken in file-name order (the product's frame names sort by time) and run
through the product's confirmation rule and 60 s cooldown. The report lists alerts and
auto-pauses that would have fired, with frame names, and `false_pauses_per_100_hours`
(hours = frames x interval). Use consecutive captures: a stratified evaluation
manifest is not a sequence.

## Comparing two models

Run `score.py` (and `replay.py`) once per model with the same manifest, frames root and
parameters, then diff:

```
diff <(python -m json.tool report_a.json) <(python -m json.tool report_b.json)
```

The report records the manifest and model SHA-256, so a mismatched comparison is visible.
Judge on false alarms per 100 frames and false pauses per 100 hours first, recall second.
