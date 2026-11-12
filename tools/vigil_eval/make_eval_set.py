#!/usr/bin/env python3
"""Build a frozen evaluation manifest from a Vigil training export.

Reads an export zip (or an extracted export directory) of labelled samples and writes
  <out>                          the evaluation manifest (JSON)
  exclude_from_training.txt      beside it, one relative frame path per line

Selection is deterministic (hash-ordered, seeded by --seed) and stratified per
printer and per class, negatives (empty label file) being their own class. Source
frames are only read, never moved or modified. Nothing is sent anywhere.

Exit codes: 0 success, 1 failure (including refusing to overwrite), 2 usage error.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path

import vigil_common as common

MAX_LABEL_BYTES = 64 * 1024
EXCLUDE_NAME = 'exclude_from_training.txt'


def parse_labels(text):
    """YOLO label text (class cx cy w h, normalized) -> [{'class', 'box': [x1,y1,x2,y2]}]."""
    labels = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError('label line')
        class_id = int(parts[0])
        cx, cy, w, h = (float(v) for v in parts[1:])
        if (not 0 <= class_id < len(common.CLASS_NAMES) or w <= 0 or h <= 0
                or not all(math.isfinite(v) for v in (cx, cy, w, h))):
            raise ValueError('label values')
        box = [max(0.0, cx - w / 2), max(0.0, cy - h / 2), min(1.0, cx + w / 2), min(1.0, cy + h / 2)]
        labels.append({'class': common.CLASS_NAMES[class_id], 'box': [round(v, 6) for v in box]})
    return labels


def collect(source):
    """[(relative image path, label text or None)] from a directory or an export zip."""
    path = Path(source)
    entries = []
    if path.is_dir():
        images, labels = path / 'images', path / 'labels'
        if not images.is_dir() or not labels.is_dir():
            raise common.ToolError('Expected images/ and labels/ in the export directory')
        for image in sorted(images.iterdir()):
            if image.suffix.lower() not in common.IMAGE_SUFFIXES or image.is_symlink() or not image.is_file():
                continue
            label = labels / f'{image.stem}.txt'
            text = None
            if label.is_file() and not label.is_symlink() and label.stat().st_size <= MAX_LABEL_BYTES:
                text = label.read_text(encoding='utf-8', errors='replace')
            entries.append((f'images/{image.name}', text))
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            infos = {i.filename: i for i in archive.infolist() if not i.is_dir()}
            for name in sorted(infos):
                parts = name.split('/')
                if (len(parts) != 2 or parts[0] != 'images' or parts[1] in ('', '.', '..')
                        or '\\' in name or Path(parts[1]).suffix.lower() not in common.IMAGE_SUFFIXES):
                    continue
                label = infos.get(f'labels/{Path(parts[1]).stem}.txt')
                text = None
                if label is not None and label.file_size <= MAX_LABEL_BYTES:
                    text = archive.read(label).decode('utf-8', errors='replace')
                entries.append((name, text))
    else:
        raise common.ToolError(f'Not an export zip or directory: {source}')
    return entries


def load_index(path):
    """{detection id: (printer_id, captured_at)} from a CSV (id,printer_id,captured_at) or JSON."""
    if not path:
        return {}
    index = {}
    p = Path(path)
    if p.suffix.lower() == '.json':
        data = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise common.ToolError('JSON index must map detection id to {printer_id, captured_at}')
        rows = [(k, v.get('printer_id'), v.get('captured_at', v.get('created_at'))) for k, v in data.items()]
    else:
        with open(p, newline='', encoding='utf-8') as handle:
            rows = [(r.get('id'), r.get('printer_id'), r.get('captured_at') or r.get('created_at'))
                    for r in csv.DictReader(handle)]
    for key, printer, when in rows:
        key = str(key).removeprefix('detection_')
        index[key] = (str(printer) if printer not in (None, '') else 'unknown', when or None)
    return index


def build_frames(entries, index):
    frames, skipped = [], {'no_label_file': 0, 'bad_label': 0}
    for relative, text in entries:
        if text is None:
            skipped['no_label_file'] += 1
            continue
        try:
            labels = parse_labels(text)
        except ValueError:
            skipped['bad_label'] += 1
            continue
        stem = Path(relative).stem.removeprefix('detection_')
        printer, when = index.get(stem, ('unknown', None))
        frames.append({'path': relative, 'printer_id': printer, 'captured_at': when,
                       'labels': labels, 'split': 'eval'})
    return frames, skipped


def rank(seed, key):
    return hashlib.sha256(f'{seed}:{key}'.encode()).hexdigest()


def select(frames, seed, cap):
    """Deterministic stratified pick: strata are (printer, class); a frame's class is its
    globally rarest label class, or 'negative'. Round-robin over strata, hash order inside."""
    counts = Counter(c for f in frames for c in {l['class'] for l in f['labels']})

    def stratum(frame):
        classes = {l['class'] for l in frame['labels']}
        if not classes:
            return (frame['printer_id'], 'negative')
        return (frame['printer_id'], min(classes, key=lambda c: (counts[c], common.CLASS_NAMES.index(c))))

    strata = {}
    for frame in sorted(frames, key=lambda f: f['path']):
        strata.setdefault(stratum(frame), []).append(frame)
    for members in strata.values():
        members.sort(key=lambda f: rank(seed, f['path']))
    order = sorted(strata, key=lambda k: rank(seed, '|'.join(k)))
    chosen = []
    while len(chosen) < cap and any(strata.values()):
        for key in order:
            if strata[key] and len(chosen) < cap:
                chosen.append(strata[key].pop(0))
    return sorted(chosen, key=lambda f: f['path']), len(order)


def write_outputs(manifest_path, manifest, force):
    manifest_path = Path(manifest_path)
    exclude_path = manifest_path.with_name(EXCLUDE_NAME)
    if not force:
        for target in (manifest_path, exclude_path):
            if target.exists():
                raise common.ToolError(f'{target} already exists; pass --force to replace it')
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'w' if force else 'x'
    with open(manifest_path, mode, encoding='utf-8') as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    with open(exclude_path, mode, encoding='utf-8') as handle:
        handle.write(''.join(f['path'] + '\n' for f in manifest['frames']))
    return exclude_path


def build_parser():
    parser = argparse.ArgumentParser(
        description='Write a frozen evaluation manifest and exclude_from_training.txt from a Vigil '
                    'training export (zip or extracted directory). Source frames are never changed.')
    parser.add_argument('--source', required=True, help='export zip, or directory containing images/ and labels/')
    parser.add_argument('--out', required=True, help='manifest path to write; exclude_from_training.txt goes beside it')
    parser.add_argument('--index', help='CSV (id,printer_id,captured_at) or JSON mapping detection id to '
                                        'printer_id and captured_at; the export itself carries neither')
    parser.add_argument('--seed', type=int, default=0, help='selection seed (default 0)')
    parser.add_argument('--max-frames', type=int, default=400, help='cap on frames in the manifest (default 400)')
    parser.add_argument('--force', action='store_true', help='replace an existing manifest and exclude list')
    return parser


def run(args):
    if not args.force:
        for target in (Path(args.out), Path(args.out).with_name(EXCLUDE_NAME)):
            if target.exists():
                raise common.ToolError(f'{target} already exists; pass --force to replace it')
    index = load_index(args.index)
    frames, skipped = build_frames(collect(args.source), index)
    if not frames:
        raise common.ToolError('No labelled frames found in the source')
    if not index:
        print('warning: no --index given; printer_id is "unknown" and captured_at is null for every frame, '
              'so selection is not stratified per printer', file=sys.stderr)
    chosen, strata = select(frames, args.seed, args.max_frames)
    if args.max_frames < strata:
        print(f'warning: --max-frames {args.max_frames} is below the {strata} printer/class groups; '
              'some groups are not represented', file=sys.stderr)
    manifest = {'version': common.MANIFEST_VERSION, 'seed': args.seed, 'max_frames': args.max_frames,
                'classes': common.CLASS_NAMES, 'source': Path(args.source).name,
                'candidates': len(frames), 'skipped': skipped, 'frames': chosen}
    exclude = write_outputs(args.out, manifest, args.force)
    print(f'{len(chosen)} of {len(frames)} labelled frames selected -> {args.out}; list: {exclude}')
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_frames < 1:
        parser.error('--max-frames must be at least 1')
    return common.guard(lambda: run(args))


if __name__ == '__main__':
    sys.exit(main())
