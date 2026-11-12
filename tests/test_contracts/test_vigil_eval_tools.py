"""Vigil evaluation tools (tools/vigil_eval): maths, selection, replay rule, no network code.

No real model is needed.
"""
import csv
import hashlib
import importlib.util
import re
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / 'tools' / 'vigil_eval'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(f'vigil_eval_test_{name}', TOOLS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load('vigil_common')
make_eval_set = _load('make_eval_set')
score = _load('score')
replay = _load('replay')


# ------------------------------------------------------------------ math

def test_iou_values():
    assert common.iou([0, 0, 1, 1], [0, 0, 1, 1]) == 1.0
    assert common.iou([0, 0, 1, 1], [2, 2, 3, 3]) == 0.0
    assert common.iou([0, 0, 2, 2], [1, 0, 3, 2]) == pytest.approx(1 / 3)


def test_matching_and_precision_recall_hand_made_case():
    a, b = [0, 0, .5, .5], [.6, .6, 1, 1]
    records = [
        {'path': 'f1', 'printer_id': '1', 'labels': [('spaghetti', a), ('spaghetti', b)],
         'dets': [('spaghetti', .9, [0, 0, .5, .45]),      # IoU 0.9 with a: TP
                  ('spaghetti', .8, [.1, .7, .2, .8]),     # no label: FP
                  ('detachment', .7, a)]},                 # wrong class: FP, label not consumed
        {'path': 'f2', 'printer_id': '1', 'labels': [], 'dets': [('spaghetti', .8, a)]},
        {'path': 'f3', 'printer_id': '2', 'labels': [], 'dets': []},
    ]
    report = score.score(records, 0.5)
    assert (report['overall']['tp'], report['overall']['fp'], report['overall']['fn']) == (1, 3, 1)
    spaghetti = report['per_class']['spaghetti']
    assert (spaghetti['tp'], spaghetti['fp'], spaghetti['fn']) == (1, 2, 1)
    assert spaghetti['precision'] == pytest.approx(1 / 3, abs=1e-3)
    assert spaghetti['recall'] == 0.5
    assert report['per_class']['detachment']['recall'] is None
    assert report['negative_frames']['false_alarms_per_100_frames'] == 50.0
    assert report['per_printer']['1']['negative_frames']['false_alarms_per_100_frames'] == 100.0
    assert report['per_printer']['2']['negative_frames']['false_alarms_per_100_frames'] == 0.0


def test_iou_threshold_decides_match():
    gts = [('spaghetti', [0, 0, 1, 1])]
    dets = [('spaghetti', .9, [0, 0, 1, .4])]   # IoU 0.4
    assert common.match_frame(dets, gts, 0.5)['spaghetti'] == [0, 1, 1]
    assert common.match_frame(dets, gts, 0.4)['spaghetti'] == [1, 0, 0]


def test_obico_decode_uses_product_contract():
    np = pytest.importorskip('numpy')
    boxes = np.array([[[[.1, .2, .5, .6]], [[.0, .0, .1, .1]]]], dtype=np.float32)
    confs = np.array([[[.9], [.2]]], dtype=np.float32)
    dets = common.postprocess_obico([boxes, confs], ['boxes', 'confs'], (100, 200, 3))
    assert len(dets) == 1                      # 0.2 is below the product floor
    assert dets[0]['bbox'] == pytest.approx([20, 20, 100, 60], abs=1e-3)
    assert dets[0]['class_id'] == 0


# ------------------------------------------------------------------ selection

def _make_export(root, count=120, with_index=True):
    (root / 'images').mkdir(parents=True)
    (root / 'labels').mkdir()
    rows = []
    for i in range(1, count + 1):
        (root / 'images' / f'detection_{i}.jpg').write_bytes(b'x%d' % i)
        kind = i % 4
        text = '' if kind == 0 else f'{kind - 1} 0.5 0.5 0.2 0.2\n'
        (root / 'labels' / f'detection_{i}.txt').write_text(text)
        rows.append((i, 1 + i % 3, f'2026-11-01T00:{i % 60:02d}:00'))
    index = root.parent / 'index.csv'
    with open(index, 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['id', 'printer_id', 'captured_at'])
        writer.writerows(rows)
    return index


def _tree_hash(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def _run(source, index, out, *extra):
    return make_eval_set.main(['--source', str(source), '--index', str(index), '--out', str(out), *extra])


def test_selection_deterministic_stratified_capped_and_source_untouched(tmp_path):
    export = tmp_path / 'export'
    index = _make_export(export)
    before = _tree_hash(export)
    assert _run(export, index, tmp_path / 'a' / 'eval.json', '--seed', '7', '--max-frames', '30') == 0
    assert _run(export, index, tmp_path / 'b' / 'eval.json', '--seed', '7', '--max-frames', '30') == 0
    assert _run(export, index, tmp_path / 'c' / 'eval.json', '--seed', '8', '--max-frames', '30') == 0
    a = (tmp_path / 'a' / 'eval.json').read_bytes()
    assert a == (tmp_path / 'b' / 'eval.json').read_bytes()
    assert a != (tmp_path / 'c' / 'eval.json').read_bytes()
    assert _tree_hash(export) == before
    import json
    manifest = json.loads(a)
    frames = manifest['frames']
    assert len(frames) == 30
    assert {f['split'] for f in frames} == {'eval'}
    assert {f['printer_id'] for f in frames} == {'1', '2', '3'}
    classes = {l['class'] for f in frames for l in f['labels']}
    assert classes == {'spaghetti', 'first_layer', 'detachment'}
    assert any(not f['labels'] for f in frames)            # negatives represented
    assert all(f['captured_at'] for f in frames)
    listed = (tmp_path / 'a' / 'exclude_from_training.txt').read_text().splitlines()
    assert listed == [f['path'] for f in frames]


def test_default_cap_and_zip_source(tmp_path):
    export = tmp_path / 'export'
    index = _make_export(export, count=40)
    archive = tmp_path / 'export.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for p in sorted(export.rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(export).as_posix())
    assert _run(archive, index, tmp_path / 'z' / 'eval.json') == 0
    assert _run(export, index, tmp_path / 'd' / 'eval.json') == 0
    assert (tmp_path / 'z' / 'eval.json').read_text().replace('export.zip', 'export') == \
        (tmp_path / 'd' / 'eval.json').read_text()


def test_refuses_to_overwrite_without_force(tmp_path):
    export = tmp_path / 'export'
    index = _make_export(export, count=20)
    out = tmp_path / 'o' / 'eval.json'
    assert _run(export, index, out, '--seed', '1') == 0
    first = out.read_bytes()
    assert _run(export, index, out, '--seed', '2') == 1
    assert out.read_bytes() == first
    assert _run(export, index, out, '--seed', '2', '--force') == 0


def test_without_index_printer_is_unknown(tmp_path):
    export = tmp_path / 'export'
    _make_export(export, count=8)
    out = tmp_path / 'u' / 'eval.json'
    assert make_eval_set.main(['--source', str(export), '--out', str(out)]) == 0
    import json
    assert {f['printer_id'] for f in json.loads(out.read_text())['frames']} == {'unknown'}


def test_usage_errors_exit_2():
    for module, argv in ((make_eval_set, []), (make_eval_set, ['--source', 'x', '--out', 'y', '--max-frames', '0']),
                         (score, ['--model', 'm', '--manifest', 'm', '--frames-root', 'd', '--conf', '2']),
                         (replay, ['--model', 'm', '--sequence', 'd', '--interval-sec', '0'])):
        with pytest.raises(SystemExit) as caught:
            module.main(argv)
        assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        score.main(['--help'])
    assert caught.value.code == 0


# ------------------------------------------------------------------ replay

def test_replay_two_of_three_and_cooldown():
    alerts = replay.simulate([.9] * 12, 'spaghetti', .65, 10)
    assert [a['index'] for a in alerts] == [2, 8]          # history cleared, then 60 s cooldown
    assert [a['would_pause'] for a in alerts] == [False, True]   # 20 s is inside the 60 s grace
    assert [a['index'] for a in replay.simulate([.9, .1, .9], 'spaghetti', .65, 10)] == [2]
    assert replay.simulate([.9, .1, .1, .9, .1], 'spaghetti', .65, 10) == []
    assert replay.simulate([.9, .9], 'spaghetti', .65, 10) == []      # needs 3 frames in history


def test_replay_first_layer_single_frame_and_missing_detections():
    assert [a['index'] for a in replay.simulate([None, .7], 'first_layer', .6, 10)] == [1]
    assert replay.simulate([None, None, None], 'spaghetti', .65, 10) == []
    assert replay.simulate([.64, .64, .64], 'spaghetti', .65, 10) == []


def test_replay_rates():
    summary = replay.summarize([{'would_pause': True}], 360, 10, True)
    assert summary['hours'] == 1.0
    assert summary['false_pauses_per_100_hours'] == 100.0
    assert replay.summarize([], 360, 10, False)['false_alerts'] is None


# ------------------------------------------------------------------ pins against product source

def _text(relative):
    return (REPO / relative).read_text(encoding='utf-8')


def test_replay_constants_pinned_to_detection_thread():
    source = _text('backend/modules/vision/detection_thread.py')
    assert int(re.search(r'^ALERT_COOLDOWN = (\d+)', source, re.M).group(1)) == replay.ALERT_COOLDOWN
    assert int(re.search(r'^AUTO_PAUSE_GRACE_SECONDS = (\d+)', source, re.M).group(1)) == replay.AUTO_PAUSE_GRACE_SECONDS
    assert int(re.search(r'^AUTO_PAUSE_MIN_FRAMES = (\d+)', source, re.M).group(1)) == replay.AUTO_PAUSE_MIN_FRAMES
    for kind, value in replay.THRESHOLDS.items():
        found = re.search(rf"self\.settings\.get\('{kind}_threshold', ([0-9.]+)\)", source)
        assert found and float(found.group(1)) == value
    assert f'if len(hist) > {replay.HISTORY_WINDOW}:' in source
    assert f'if len(hist) < {replay.HISTORY_WINDOW}:' in source
    assert f'return sum(hist[-{replay.HISTORY_WINDOW}:]) >= {replay.REQUIRED_OF_WINDOW}' in source
    assert 'return len(hist) >= 1 and hist[-1]' in source
    assert "if detection_type == 'first_layer':" in source
    assert "above = best is not None and best['confidence'] >= threshold" in source
    assert 'if now - last < ALERT_COOLDOWN:' in source


def test_decode_constants_pinned_to_inference_engine():
    source = _text('backend/modules/vision/inference_engine.py')
    assert f'if conf < {common.CONFIDENCE_FLOOR}:' in source
    assert f'if confidence < {common.CONFIDENCE_FLOOR}:' in source
    assert source.count(f'iou_threshold={common.NMS_IOU}') == 2
    assert 'img = img.astype(np.float32) / 255.0' in source
    assert 'cv2.cvtColor(img, cv2.COLOR_BGR2RGB)' in source
    assert 'cv2.resize(frame, (input_size, input_size))' in source
    assert 'frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)' in _text('backend/modules/vision/detection_thread.py')


def test_class_names_pinned_to_training_service():
    source = _text('backend/modules/vision/training_service.py')
    declared = re.search(r"^CLASSES=\{(.*?)\}", source, re.M).group(1)
    pairs = re.findall(r"'(\w+)':(\d+)", declared)
    assert [name for name, _ in sorted(pairs, key=lambda p: int(p[1]))] == common.CLASS_NAMES


# ------------------------------------------------------------------ no network code

def test_tools_contain_no_network_libraries():
    scripts = sorted(TOOLS.glob('*.py'))
    assert {p.name for p in scripts} >= {'make_eval_set.py', 'score.py', 'replay.py', 'vigil_common.py'}
    for script in scripts:
        text = script.read_text(encoding='utf-8')
        for banned in ('requests', 'urllib.request', 'httpx', 'socket'):
            assert banned not in text, f'{script.name} mentions {banned}'
