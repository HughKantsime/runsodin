#!/usr/bin/env python3
"""Replay a frame sequence through the product's alert confirmation rule.

Runs the model on frames in time order, then applies the product's confirmation window
and cooldown to the per-frame best confidence and reports how many alerts (and how many
auto-pauses) WOULD have fired, and at which frames. With --assume-no-failures every
alert is false, giving false alerts per 100 print hours (hours = frames x interval).

The rule is MIRRORED from backend/modules/vision/detection_thread.py (it is a method on
the capture thread, not a pure function, and that module needs the whole backend):
  best >= threshold -> detected      detection_thread.py:229-230
  history of the last 3 frames       detection_thread.py:318-324  (_update_history)
  first_layer: 1 frame; others: 2 of the last 3, needing 3 in history
                                     detection_thread.py:326-343  (_should_trigger)
  cooldown ALERT_COOLDOWN = 60 s     detection_thread.py:37, :329-332
  history cleared after an alert     detection_thread.py:237
  auto-pause gates                   detection_thread.py:45-46, :603-609
tests/test_contracts/test_vigil_eval_tools.py pins these constants against that file.

Not modelled: print state and layer gating, job changes, and the printer actually
pausing (every alert is treated independently). Frames must be consecutive captures
at the stated interval; a stratified evaluation manifest is not a sequence.

Exit codes: 0 success, 1 failure (also when any frame could not be read), 2 usage error.
"""
import argparse
import sys
from pathlib import Path

import vigil_common as common

ALERT_COOLDOWN = 60                 # detection_thread.py:37
HISTORY_WINDOW = 3                  # detection_thread.py:323
REQUIRED_OF_WINDOW = 2              # detection_thread.py:343
AUTO_PAUSE_GRACE_SECONDS = 60       # detection_thread.py:45
AUTO_PAUSE_MIN_FRAMES = 3           # detection_thread.py:46
THRESHOLDS = {'spaghetti': 0.65, 'first_layer': 0.60, 'detachment': 0.70}   # detection_thread.py:196,199,202


def simulate(confidences, detection_type, threshold, interval_sec):
    """confidences: best detection confidence per frame, None when nothing was detected."""
    history, last_alert, alerts = [], None, []
    for index, conf in enumerate(confidences):
        now = index * interval_sec
        history.append(conf is not None and conf >= threshold)
        if len(history) > HISTORY_WINDOW:
            history.pop(0)
        if last_alert is not None and now - last_alert < ALERT_COOLDOWN:
            continue
        if detection_type == 'first_layer':
            fire = len(history) >= 1 and history[-1]
        else:
            fire = len(history) >= HISTORY_WINDOW and sum(history[-HISTORY_WINDOW:]) >= REQUIRED_OF_WINDOW
        if fire:
            alerts.append({'index': index, 't_sec': now, 'confidence': conf,
                           'would_pause': index + 1 >= AUTO_PAUSE_MIN_FRAMES and now >= AUTO_PAUSE_GRACE_SECONDS})
            history.clear()
            last_alert = now
    return alerts


def summarize(alerts, frame_count, interval_sec, assume_no_failures):
    hours = frame_count * interval_sec / 3600
    pauses = sum(1 for a in alerts if a['would_pause'])

    def per100(count):
        return round(count / hours * 100, 3) if hours > 0 else None

    return {'frames': frame_count, 'interval_sec': interval_sec, 'hours': round(hours, 4),
            'alert_count': len(alerts), 'alerts_per_100_hours': per100(len(alerts)),
            'pause_count': pauses, 'pauses_per_100_hours': per100(pauses),
            'false_alerts': len(alerts) if assume_no_failures else None,
            'false_alerts_per_100_hours': per100(len(alerts)) if assume_no_failures else None,
            'false_pauses_per_100_hours': per100(pauses) if assume_no_failures else None}


def sequence_paths(sequence, frames_root):
    """Frame paths in time order: file-name order for a directory, captured_at then path for a manifest."""
    path = Path(sequence)
    if path.is_dir():
        return [p for p in sorted(path.rglob('*'))
                if p.suffix.lower() in common.IMAGE_SUFFIXES and p.is_file() and not p.is_symlink()]
    if not path.is_file():
        raise common.ToolError(f'Sequence not found: {sequence}')
    manifest = common.load_manifest(path)
    frames = sorted(manifest['frames'], key=lambda f: (f.get('captured_at') or '', f['path']))
    return [common.resolve_frame(frames_root, f['path']) for f in frames]


def build_parser():
    parser = argparse.ArgumentParser(
        description="Replay frames in time order through the product's alert confirmation rule and "
                    'report the alerts that would have fired. Local files only.')
    parser.add_argument('--model', required=True, help='path to the .onnx model')
    parser.add_argument('--sequence', required=True,
                        help='directory of consecutive frames (sorted by file name) or an evaluation manifest')
    parser.add_argument('--interval-sec', type=float, default=10.0,
                        help='seconds between frames (default 10, the product capture interval)')
    parser.add_argument('--detection-type', choices=sorted(THRESHOLDS), default='spaghetti',
                        help='which product rule to apply (default spaghetti)')
    parser.add_argument('--threshold', type=float, help="override the product's default threshold for the type")
    parser.add_argument('--frames-root', help='directory the manifest paths are relative to (manifest input only)')
    parser.add_argument('--assume-no-failures', action='store_true',
                        help='the sequence contains no real failure: every alert is a false alert')
    parser.add_argument('--contract', help='file with the vision_models.metadata_json content')
    parser.add_argument('--out', help='write the JSON report here instead of stdout')
    return parser


def run(args):
    threshold = args.threshold if args.threshold is not None else THRESHOLDS[args.detection_type]
    paths = sequence_paths(args.sequence, args.frames_root)
    if not paths:
        raise common.ToolError('Sequence has no frames')
    model = common.load_model(args.model, args.contract)
    confidences, errors = [], []
    for path in paths:
        try:
            dets = common.detect(model, common.read_frame(path))
            confidences.append(max((d['confidence'] for d in dets), default=None))
        except Exception as exc:  # noqa: BLE001 - a bad frame counts as "nothing detected", and is reported
            confidences.append(None)
            errors.append({'path': path.name, 'error': f'{type(exc).__name__}: {exc}'})
    alerts = simulate(confidences, args.detection_type, threshold, args.interval_sec)
    for alert in alerts:
        alert['frame'] = paths[alert['index']].name
    report = {'tool': 'vigil_eval.replay',
              'model': {'file': Path(args.model).name, 'sha256': common.sha256_file(args.model)},
              'rule': {'detection_type': args.detection_type, 'threshold': threshold,
                       'cooldown_sec': ALERT_COOLDOWN, 'window': HISTORY_WINDOW,
                       'required': 1 if args.detection_type == 'first_layer' else REQUIRED_OF_WINDOW},
              **summarize(alerts, len(paths), args.interval_sec, args.assume_no_failures),
              'alerts': alerts, 'errors': errors}
    if args.out:
        common.write_json(args.out, report)
    else:
        import json
        print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if errors else 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval_sec <= 0:
        parser.error('--interval-sec must be greater than 0')
    if args.threshold is not None and not 0 < args.threshold <= 1:
        parser.error('--threshold must be in (0, 1]')
    if Path(args.sequence).is_file() and not args.frames_root:
        parser.error('--frames-root is required when --sequence is a manifest')
    return common.guard(lambda: run(args))


if __name__ == '__main__':
    sys.exit(main())
