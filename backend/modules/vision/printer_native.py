"""Detections reported by a Bambu printer's own camera: recorded and alerted, nothing else.

The Bambu monitor calls observe() for every status packet. A reviewed code that becomes
active is stored once as a vision detection (metadata source "printer") and raises the
alert a Vigil detection raises. Nothing here pauses, stops or fails a print: the printer
applies its own setting, and job outcomes stay with the HMS rules in docs/bambu-hms.md.

The monitor thread only compares small sets and, when the active set changes, runs one
short local transaction. The picture and the alert are finished on a worker thread.
"""
import json
import logging
import threading
import time

from sqlalchemy import or_, text

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
from core.db_compat import execute_insert_returning_id, sql
# Resolved at call time: never a stale get_db bound at import.
from core.db_live import live_get_db as get_db

log = logging.getLogger('printer_events')

SOURCE = 'printer'
INFORMATIONAL = 'informational'   # recorded without a picture: never waits in the review queue
SNAPSHOT_SECONDS = 3.0
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024

# Reviewed against backend/data/bambu_hms/*.json. The deliberate exception to "no
# hand-maintained code list" (docs/bambu-hms.md): only the printer knows what its camera
# saw, and the vendor lists carry no machine-readable class. Contract tests fail when a
# refreshed list drops one of these, changes its meaning, or adds an unreviewed sibling.
# 16 hex digits = HMS item (attr + code); 8 hex digits = print_error.
PRINTER_DETECTION_CODES = {
    '0C00030000030008': 'spaghetti',
    '0C0003000003001B': 'spaghetti',
    '0C00C004': 'spaghetti',
    '0C008042': 'spaghetti',
    '03008003': 'spaghetti',
    '0C00030000030007': 'first_layer',
    '0C008001': 'first_layer',
    '0C00C003': 'first_layer',
    '03008002': 'first_layer',
    '0C00803F': 'nozzle_clump',
    '0C008043': 'nozzle_clump',
    '0C008040': 'air_print',
    '05008099': 'shift_collapse',
    '0C00030000030006': 'chute_pileup',
    '0C008005': 'chute_pileup',
    '0C00C006': 'chute_pileup',
    '0300800A': 'chute_pileup',
}

# Mention the same subjects but are not a print problem seen during a print.
NOT_DETECTION_CODES = frozenset({
    # Camera or AI-monitoring health.
    '0C00010000020008', '0C00010000020017', '0C0001000003000C', '0C00040000020026',
    '0C008034', '05008086', '0500808F', '05008092',
    # Build-plate marker (plate placement).
    '0C0003000002000C', '0C008009',
    # Nozzle-clumping sensor: calibration, sensor faults and the pre-print probe check
    # (03008014 means different things by family).
    '030018000001000D', '030018000001000E', '03001A0000020003', '03001A0000030004',
    '03004015', '03004037', '0300802C', '03008014',
})

# Every bundled text for a code of this kind must contain one of these (lower case).
KIND_KEYWORDS = {
    'spaghetti': ('spaghetti',),
    'first_layer': ('first layer',),
    'nozzle_clump': ('clumping',),
    'air_print': ('air-printing',),
    'shift_collapse': ('collapse',),
    'chute_pileup': ('piled up', 'pile-up'),
}

_KIND_WORDS = {
    'spaghetti': 'possible spaghetti',
    'first_layer': 'possible first-layer defects',
    'nozzle_clump': 'possible nozzle clumping',
    'air_print': 'possible air printing',
    'shift_collapse': 'a possible shifted or collapsed print',
    'chute_pileup': 'a possible full waste chute',
}

# Vigil has three alert types and their recipient preferences; there is none for clumping,
# air printing or the waste chute. Each kind uses the nearest one so the same people are told.
_ALERTS = {
    'spaghetti': ('spaghetti_detected', 'critical'),
    'first_layer': ('first_layer_issue', 'warning'),
    'nozzle_clump': ('spaghetti_detected', 'critical'),
    'air_print': ('spaghetti_detected', 'critical'),
    'shift_collapse': ('detachment_detected', 'critical'),
    'chute_pileup': ('spaghetti_detected', 'warning'),
}

# json.dumps writes the first form; the second covers compact writers.
_SOURCE_PATTERNS = ('%"source": "printer"%', '%"source":"printer"%')
NOT_PRINTER_SOURCE_SQL = ("(metadata_json IS NULL OR (metadata_json NOT LIKE '%\"source\": \"printer\"%' "
                          "AND metadata_json NOT LIKE '%\"source\":\"printer\"%'))")


def source_clause(column):
    """SQLAlchemy predicate: this detection row came from the printer's own camera."""
    return or_(*(column.like(pattern) for pattern in _SOURCE_PATTERNS))


def source_of(metadata_json):
    try:
        value = json.loads(metadata_json) if isinstance(metadata_json, str) else None
    except ValueError:
        value = None
    return SOURCE if isinstance(value, dict) and value.get('source') == SOURCE else 'vigil'


def alert_title(printer_name, kind):
    return f"{printer_name}: the printer's camera saw {_KIND_WORDS[kind]}"


def _state_key(printer_id):
    return f'printer.native_detection_codes.{printer_id}'


def _hms_codes(items):
    codes = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        attr, code = item.get('attr'), item.get('code')
        if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in (attr, code)):
            continue
        full = f'{attr:08X}{code:08X}'
        if full in PRINTER_DETECTION_CODES:
            codes.add(full)
    return codes


def _print_error_code(value):
    """8 hex digits, '' for no error, None when the report is unreadable (no change)."""
    if type(value) is str and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        return None
    return f'{value:08X}' if value else ''


def _load_active(printer_id):
    with engine.connect() as conn:
        value = conn.execute(text('SELECT value FROM system_config WHERE key=:key'),
                             {'key': _state_key(printer_id)}).scalar()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return frozenset(code for code in value if code in PRINTER_DETECTION_CODES) if isinstance(value, list) else frozenset()


def observe(monitor, raw, status=None):
    """Monitor hook. Never raises; a failed write is retried by the next report that carries codes."""
    try:
        _observe(monitor, raw, status)
    except Exception:
        log.warning('Printer camera detection not recorded for printer %s; will retry',
                    getattr(monitor, 'printer_id', None), exc_info=True)


def _observe(monitor, raw, status):
    if not isinstance(raw, dict) or getattr(status, 'mqtt_retained', None) is True:
        return   # a replayed report is not this moment's camera
    hms = raw.get('hms')
    error = _print_error_code(raw.get('print_error'))
    if not isinstance(hms, list) and error is None:
        return   # omitted or null: nothing known, the active set stands
    active = getattr(monitor, '_native_detection_codes', None)
    if active is None:
        # First report after a restart: the stored identities, so nothing repeats.
        active = monitor._native_detection_codes = _load_active(monitor.printer_id)
    current = set(active)
    if isinstance(hms, list):
        current = {code for code in current if len(code) != 16} | _hms_codes(hms)
    if error is not None:
        current = {code for code in current if len(code) != 8}
        if error in PRINTER_DETECTION_CODES:
            current.add(error)
    current = frozenset(current)
    if current == active:
        return
    from modules.printers.bambu_hms_bundle import prefix_from_serial
    from modules.printers.hms_codes import lookup_hms_code
    prefix = prefix_from_serial(getattr(monitor, 'serial', ''))
    job_id = getattr(monitor, '_current_job_id', None)
    state = getattr(monitor, '_state', None) or {}
    filename = (raw.get('subtask_name') or raw.get('gcode_file')
                or state.get('subtask_name') or state.get('gcode_file'))
    created = []
    with engine.begin() as conn:
        kinds_active = {PRINTER_DETECTION_CODES[code] for code in active}
        for code in sorted(current - active):
            kind = PRINTER_DETECTION_CODES[code]
            if kind in kinds_active:
                continue   # the same problem under a second code: one detection per kind
            kinds_active.add(kind)
            message = lookup_hms_code(code, prefix)
            detection_id = execute_insert_returning_id(conn, (
                'INSERT INTO vision_detections (printer_id, print_job_id, detection_type, confidence,'
                ' status, metadata_json, created_at)'
                f' VALUES (:pid, :jid, :kind, 1.0, :status, :meta, {sql.now()})'),
                {'pid': monitor.printer_id, 'jid': job_id, 'kind': kind, 'status': INFORMATIONAL,
                 'meta': json.dumps({'source': SOURCE, 'code': code, 'text': message})})
            created.append((detection_id, kind, code, message))
        conn.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                          'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                     {'key': _state_key(monitor.printer_id), 'value': json.dumps(sorted(current))})
    # Only a committed set is remembered: a failed write leaves the codes new for the retry.
    monitor._native_detection_codes = current
    for detection_id, kind, code, message in created:
        _start_worker(_finish, monitor.printer_id, job_id, detection_id, kind, code, message, filename)


def _start_worker(target, *args):
    threading.Thread(target=target, args=args, daemon=True, name='printer-native-detection').start()


def _finish(printer_id, job_id, detection_id, kind, code, message, filename):
    """Worker thread: best-effort picture, then the alert. The detection row already exists."""
    try:
        frame_path = _snapshot(printer_id, kind)
    except Exception:
        log.debug('Printer camera detection %s: no picture', detection_id, exc_info=True)
        frame_path = None
    try:
        if frame_path:
            with engine.begin() as conn:
                # With a picture it is reviewed like any other detection.
                conn.execute(text(
                    "UPDATE vision_detections SET frame_path=:path, "
                    "status=CASE WHEN status=:informational THEN 'pending' ELSE status END "
                    'WHERE id=:id AND frame_path IS NULL'),
                    {'path': frame_path, 'informational': INFORMATIONAL, 'id': detection_id})
        _alert(printer_id, job_id, detection_id, kind, code, message, filename)
    except Exception:
        log.error('Printer camera detection %s: alert not sent', detection_id, exc_info=True)


def _snapshot(printer_id, kind):
    """One JPEG through go2rtc, as Vigil reads it; None without a camera or a frame in time."""
    from modules.vision.camera_source import camera_source
    with engine.connect() as conn:
        printer = conn.execute(text('SELECT * FROM printers WHERE id=:id'), {'id': printer_id}).mappings().first()
    if not printer or not printer['is_active'] or not printer['camera_enabled'] or not camera_source(printer):
        return None
    try:
        import cv2
        import httpx
        import numpy as np
    except ImportError:
        return None
    from modules.printers.go2rtc_access import go2rtc_api_url, go2rtc_client_kwargs
    from modules.vision import frame_storage
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    data = bytearray()
    with httpx.stream('GET', f'{go2rtc_api_url()}/api/frame.jpeg', params={'src': f'printer_{printer_id}'},
                      timeout=SNAPSHOT_SECONDS, **go2rtc_client_kwargs()) as response:
        if response.status_code != 200:
            return None
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_SNAPSHOT_BYTES or time.monotonic() > deadline:
                return None
    frame = cv2.imdecode(np.frombuffer(bytes(data), dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    # The detections folder only: this path never writes a training frame.
    return frame_storage.save_detection_frame(printer_id, frame, kind)


def _education_suppressed(printer_id, job_id, filename):
    """Same rule as HMS alerts: no external alert for an Education print; unsure means none."""
    try:
        from core.interfaces.education_policy import is_education_reserved_filename
        from core.registry import registry
        with get_db() as conn:
            row = conn.execute("SELECT id,filename FROM print_jobs WHERE printer_id=? AND id=?",
                               (printer_id, job_id)).fetchone() if job_id is not None else None
            stored = row[1] if row else None
            reserved = is_education_reserved_filename(filename) or is_education_reserved_filename(stored)
            policy = registry.get_provider('EducationPolicyProvider')
            if not policy:
                return reserved
            outcome = policy.classify_monitor_observation(
                conn, printer_id=printer_id, print_job_id=row[0] if row else None,
                observed_filename=filename or stored)
            return reserved or bool(outcome.get('education_owned') or outcome.get('education_reserved'))
    except Exception:
        log.error('Education check failed for a printer camera detection on printer %s', printer_id)
        return True


def _alert(printer_id, job_id, detection_id, kind, code, message, filename):
    if _education_suppressed(printer_id, job_id, filename):
        return
    from modules.notifications.alert_dispatch import dispatch_alert
    with engine.connect() as conn:
        row = conn.execute(text('SELECT name, nickname FROM printers WHERE id=:id'), {'id': printer_id}).first()
    name = (row[1] or row[0]) if row else f'Printer {printer_id}'
    alert_type, severity = _ALERTS[kind]
    # Printer only, as Vigil does: recipients pass the printer's organization and
    # controlled-print audience checks inside dispatch_alert. No 'vision_detection' live
    # event: that one is broadcast, and each recipient already gets the alert itself.
    dispatch_alert(alert_type=alert_type, severity=severity, title=alert_title(name, kind),
                   message=message, printer_id=printer_id,
                   metadata={'detection_id': detection_id, 'detection_type': kind, 'confidence': 1.0,
                             'source': SOURCE, 'code': code})
