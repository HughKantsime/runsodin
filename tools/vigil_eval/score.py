#!/usr/bin/env python3
"""Score a detection model on a frozen evaluation manifest.

Runs the model on each manifest frame with the product's preprocessing and output
decoding (mirrored in vigil_common.py) and reports true/false positives, false
negatives, precision and recall overall, per class and per printer, plus false alarms
per 100 negative frames. A detection matches a label of the same class at IoU >= --iou.
Reads local files and writes a local report only.

Exit codes: 0 success, 1 failure (also when any frame could not be scored), 2 usage error.
"""
import argparse
import sys
from pathlib import Path

import vigil_common as common


def _blank():
    return {'tp': 0, 'fp': 0, 'fn': 0}


def _add(counts, tp, fp, fn):
    counts['tp'] += tp
    counts['fp'] += fp
    counts['fn'] += fn


def _finish(counts):
    precision, recall = common.precision_recall(counts['tp'], counts['fp'], counts['fn'])
    return {**counts,
            'precision': None if precision is None else round(precision, 4),
            'recall': None if recall is None else round(recall, 4)}


def _negatives(frames, alarmed, detections):
    return {'frames': frames, 'frames_with_detection': alarmed, 'detections': detections,
            'false_alarms_per_100_frames': round(100 * alarmed / frames, 3) if frames else None}


def score(records, iou_min):
    """records: [{'path', 'printer_id', 'labels': [(class, box)], 'dets': [(class, conf, box)]}]."""
    overall, per_class, per_printer = _blank(), {}, {}
    neg_total = [0, 0, 0]
    neg_printer = {}
    for record in records:
        printer = per_printer.setdefault(record['printer_id'], {'overall': _blank(), 'per_class': {}})
        for cls, (tp, fp, fn) in common.match_frame(record['dets'], record['labels'], iou_min).items():
            for target in (overall, per_class.setdefault(cls, _blank()), printer['overall'],
                           printer['per_class'].setdefault(cls, _blank())):
                _add(target, tp, fp, fn)
        if not record['labels']:
            counts = neg_printer.setdefault(record['printer_id'], [0, 0, 0])
            for acc in (neg_total, counts):
                acc[0] += 1
                acc[1] += 1 if record['dets'] else 0
                acc[2] += len(record['dets'])
    return {
        'overall': _finish(overall),
        'per_class': {c: _finish(v) for c, v in sorted(per_class.items())},
        'per_printer': {p: {**_finish(v['overall']),
                            'per_class': {c: _finish(x) for c, x in sorted(v['per_class'].items())},
                            'negative_frames': _negatives(*neg_printer.get(p, [0, 0, 0]))}
                        for p, v in sorted(per_printer.items())},
        'negative_frames': _negatives(*neg_total),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description='Score an ONNX detection model on a frozen evaluation manifest, using the product '
                    'preprocessing and decoding. Local files only.')
    parser.add_argument('--model', required=True, help='path to the .onnx model')
    parser.add_argument('--manifest', required=True, help='evaluation manifest from make_eval_set.py')
    parser.add_argument('--frames-root', required=True, help='directory the manifest paths are relative to')
    parser.add_argument('--conf', type=float, default=0.5,
                        help='minimum confidence for a detection to count (default 0.5; the product never '
                             'sees detections below 0.3)')
    parser.add_argument('--iou', type=float, default=0.5, help='minimum IoU for a match (default 0.5)')
    parser.add_argument('--out', help='write the JSON report here instead of stdout')
    parser.add_argument('--contract', help='file with the vision_models.metadata_json content '
                                           '(required unless the model is the legacy Obico boxes/confs layout)')
    parser.add_argument('--class-names', help='comma-separated names for model class ids '
                                              '(default: export order, or one name for an Obico model)')
    parser.add_argument('--single-class-name', default='spaghetti',
                        help='class name for a single-class Obico model (default spaghetti)')
    return parser


def run(args):
    manifest = common.load_manifest(args.manifest)
    frames = [f for f in manifest['frames'] if f.get('split') == 'eval']
    if not frames:
        raise common.ToolError('Manifest has no frames with split "eval"')
    model = common.load_model(args.model, args.contract)
    names = common.class_names(args.class_names, args.single_class_name, model)
    if args.conf < common.CONFIDENCE_FLOOR:
        print(f'warning: --conf {args.conf} is below the product floor {common.CONFIDENCE_FLOOR}; '
              'the decoder drops lower detections anyway', file=sys.stderr)
    records, errors = [], []
    for frame in frames:
        try:
            image = common.read_frame(common.resolve_frame(args.frames_root, frame['path']))
            dets = [common.normalize(d, image.shape, names) for d in common.detect(model, image)
                    if d['confidence'] >= args.conf]
            records.append({'path': frame['path'], 'printer_id': str(frame.get('printer_id', 'unknown')),
                            'labels': [(l['class'], l['box']) for l in frame['labels']], 'dets': dets})
        except Exception as exc:  # noqa: BLE001 - reported per frame, run continues
            errors.append({'path': frame['path'], 'error': f'{type(exc).__name__}: {exc}'})
    report = {'tool': 'vigil_eval.score',
              'model': {'file': Path(args.model).name, 'sha256': common.sha256_file(args.model),
                        'input_size': model.input_size, 'format': model.contract.format},
              'manifest': {'sha256': common.sha256_file(args.manifest), 'frames': len(frames)},
              'params': {'conf': args.conf, 'iou': args.iou, 'class_names': names},
              'frames_scored': len(records), 'errors': errors,
              **score(records, args.iou)}
    if args.out:
        common.write_json(args.out, report)
    else:
        import json
        print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if errors else 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 < args.conf <= 1:
        parser.error('--conf must be in (0, 1]')
    if not 0 < args.iou <= 1:
        parser.error('--iou must be in (0, 1]')
    return common.guard(lambda: run(args))


if __name__ == '__main__':
    sys.exit(main())
