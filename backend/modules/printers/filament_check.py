"""Pre-dispatch filament check.

Compares what a print needs per filament with what the mapped spool has left.
Read-only SQL (no ORM imports from other modules). Result per slot:
  enough  - remaining >= needed
  short   - remaining <  needed
  unknown - no estimate, no slot, no spool, no remaining weight, no valid
            quantity, or an ambiguous mapping. Never guessed, never holds.

needed = grams for that filament x (1 + margin/100), for ONE print: a dispatch prints one plate,
and completion deducts the slot grams once (Job.quantity is a repeat count, not this print's
usage). Pieces per bed (quantity_on_bed, quantity_per_bed) are
never multiplied in.

Settings live in system_config (no migration):
  filament_check_mode        off | warn (default) | hold
  filament_check_margin_pct  0..100 (default 10)

The gate runs once per dispatch, in dispatch_orchestration, after options are
validated and before any Education reservation or admission. A failure of the
check itself never blocks dispatch.
"""
import json
import logging
import math
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from core.filament_sources import indexed_materials
from core.system_config_values import config_text, write_config_text

log = logging.getLogger('odin.dispatch')

MODE_KEY = 'filament_check_mode'
MARGIN_KEY = 'filament_check_margin_pct'
MODES = ('off', 'warn', 'hold')
DEFAULT_MODE = 'warn'
DEFAULT_MARGIN_PCT = 10.0
MAX_MARGIN_PCT = 100.0
REFUSAL_CODE = 'filament_short'

_NO_MATERIAL = {'', 'empty', 'unknown', 'other'}

_WHY = {
    'no_estimate': "the file and model have no filament weight estimate",
    'no_spool': 'no spool is assigned to this slot',
    'no_remaining_weight': 'no remaining weight is recorded for its spool',
    'quantity_unknown': 'the job has no valid quantity',
    'no_mapping': 'the file uses several filaments and no AMS mapping is saved',
    'slot_unknown': "it is unclear which slot this printer will use",
    'slot_missing': 'the printer has no such slot',
    'spool_change_pending': 'a spool change is waiting to be confirmed',
    'other_plate': 'the weight on file is for the first plate and this job prints another one',
}


# ---------------------------------------------------------------- settings

def parse_margin(value) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= MAX_MARGIN_PCT else None


def read_settings(conn) -> dict:
    """Stored settings, or the defaults when absent or invalid."""
    raw = {}
    for key in (MODE_KEY, MARGIN_KEY):
        row = conn.execute(text('SELECT value FROM system_config WHERE key = :k'), {'k': key}).first()
        raw[key] = None if row is None else config_text(row[0])
    mode = (raw[MODE_KEY] or '').strip().lower()
    margin = parse_margin(raw[MARGIN_KEY]) if raw[MARGIN_KEY] not in (None, '') else None
    return {'mode': mode if mode in MODES else DEFAULT_MODE,
            'margin_pct': DEFAULT_MARGIN_PCT if margin is None else margin}


def write_settings(db, *, mode=None, margin_pct=None) -> None:
    """Stage new values in the caller's transaction. Caller audits and commits."""
    if mode is not None:
        if mode not in MODES:
            raise ValueError('Unsupported filament check mode')
        write_config_text(db, MODE_KEY, mode)
    if margin_pct is not None:
        if parse_margin(margin_pct) is None:
            raise ValueError('Margin must be between 0 and 100')
        write_config_text(db, MARGIN_KEY, format(float(margin_pct), 'g'))


# ------------------------------------------------------------ small helpers

def _as_json(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _grams(value) -> Optional[float]:
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _positive(value) -> Optional[float]:
    grams = _grams(value)
    return grams if grams else None  # zero or missing is "no estimate", not "needs nothing"


def _mapping(value):
    mapping = _as_json(value)
    if isinstance(mapping, list) and mapping and all(type(i) is int and -1 <= i < 16 for i in mapping):
        return mapping
    return None


def _model_grams(value) -> dict:
    """Legacy fallback, same convention as completion: sorted keys, positional."""
    requirements = _as_json(value)
    if not isinstance(requirements, dict):
        return {}
    grams = {}
    for index, key in enumerate(sorted(requirements)):
        item = requirements[key]
        amount = _positive(item.get('grams')) if isinstance(item, dict) else None
        if amount:
            grams[index] = amount
    return grams


def _material(value) -> Optional[str]:
    text_value = str(value or '').strip()
    return None if text_value.lower() in _NO_MATERIAL else text_value


def _label(slot, material, color) -> str:
    if slot is None:
        return 'This job'
    extra = ' '.join(part for part in (color, material) if part)
    return f'Slot {slot}' + (f' ({extra})' if extra else '')


def _printer_slots(conn, printer_id) -> dict:
    rows = conn.execute(text(
        'SELECT fs.slot_number, fs.filament_type, fs.color, fs.tray_observation, '
        'fs.assigned_spool_id, sp.remaining_weight_g '
        'FROM filament_slots fs LEFT JOIN spools sp ON sp.id = fs.assigned_spool_id '
        'WHERE fs.printer_id = :p ORDER BY fs.slot_number'), {'p': printer_id}).mappings().all()
    return {row['slot_number']: dict(row) for row in rows}


def _pending(row) -> bool:
    try:
        observed = _as_json(row.get('tray_observation')) if row.get('tray_observation') else {}
        return bool(isinstance(observed, dict) and observed.get('pending'))
    except Exception:
        return False


def _source_grams(index, indexed, model_grams, mapped_count) -> Optional[float]:
    """Per-plate file facts first; the model fallback only when unambiguous."""
    if indexed and index in indexed:
        item = indexed[index]
        for key in ('used_grams', 'weight_grams'):
            grams = _positive(item.get(key))
            if grams:
                return grams
    if mapped_count <= 1:
        if index in model_grams:
            return model_grams[index]
        if len(model_grams) == 1:
            return next(iter(model_grams.values()))
    return None


def _only_loaded_slot(slots) -> Optional[int]:
    if len(slots) == 1:
        return next(iter(slots))
    loaded = [number for number, row in slots.items() if _material(row.get('filament_type'))]
    return loaded[0] if len(loaded) == 1 else None


def _demands(mapping, indexed, model_grams, slots) -> list:
    if mapping is not None:
        used = [(i, target) for i, target in enumerate(mapping) if target >= 0]
        return [dict(source=i, slot=target + 1,
                     grams=_source_grams(i, indexed, model_grams, len(used)), reason=None)
                for i, target in used]
    sources = sorted(indexed) if indexed else sorted(model_grams)
    if not sources:
        return [dict(source=None, slot=None, grams=None, reason='no_estimate')]
    if len(sources) > 1:
        return [dict(source=None, slot=None, grams=None, reason='no_mapping')]
    slot = _only_loaded_slot(slots)
    return [dict(source=sources[0], slot=slot, reason=None if slot is not None else 'slot_unknown',
                 grams=_source_grams(sources[0], indexed, model_grams, 1))]


# -------------------------------------------------------------- evaluation

def _judge(slot_no, parts, row, prints, margin) -> dict:
    grams = None if any(p['grams'] is None for p in parts) else sum(p['grams'] for p in parts)
    material = _material(row.get('filament_type')) if row else None
    color = (row.get('color') or None) if row else None
    reason = next((p['reason'] for p in parts if p['reason']), None)
    remaining = None
    if reason is None:
        if slot_no is None:
            reason = 'slot_unknown'
        elif row is None:
            reason = 'slot_missing'
        elif grams is None:
            reason = 'no_estimate'
        elif prints is None:
            reason = 'quantity_unknown'
        elif row.get('assigned_spool_id') is None:
            reason = 'no_spool'
        elif _pending(row):
            reason = 'spool_change_pending'
        else:
            remaining = _grams(row.get('remaining_weight_g'))
            if remaining is None:
                reason = 'no_remaining_weight'
    label = _label(slot_no, material, color)
    item = dict(slot=slot_no, source_slots=[p['source'] + 1 for p in parts if p['source'] is not None],
                material=material, color=color, status='unknown', needed_g=None, print_g=None,
                remaining_g=None if remaining is None else round(remaining, 1),
                spool_id=row.get('assigned_spool_id') if row else None, reason=reason, message='')
    if reason is not None:
        item['message'] = f"{label}: can't check, {_WHY[reason]}"
        return item
    print_g = grams * prints
    needed = print_g * (100 + margin) / 100  # multiply first: 100 x 110 / 100 is exact, 100 x 1.1 is not
    item.update(print_g=round(print_g, 1), needed_g=round(needed, 1),
                status='short' if remaining < needed else 'enough')
    item['message'] = (f'{label} has about {math.floor(remaining)} g left; '
                       f'this print needs about {math.ceil(needed)} g')
    return item


def evaluate(conn, *, job, printer_id, mapping=None, settings=None, derive=True) -> dict:
    """Per-slot result for ``job`` on ``printer_id``.

    ``job`` is the dict from dispatch._load_job (ams_mapping, filaments_json).
    ``mapping`` is the validated or derived AMS vector the dispatch will use.
    ``derive`` False (the dispatch path): the mapping given is final, so a job sent with no AMS
    mapping (external spool) is never judged against an AMS slot it will not use.
    """
    settings = settings or read_settings(conn)
    row = conn.execute(text(
        'SELECT j.quantity, m.color_requirements FROM jobs j '
        'LEFT JOIN models m ON m.id = j.model_id WHERE j.id = :id'), {'id': job['id']}).first()
    quantity = row[0] if row else None
    # One dispatch = one print. A job's quantity is how many times it repeats, not this print's usage.
    prints = 1 if type(quantity) is int and quantity >= 1 else None
    model_grams = _model_grams(row[1] if row else None)
    indexed = indexed_materials(_as_json(job.get('filaments_json')))
    api_type = conn.execute(text('SELECT api_type FROM printers WHERE id = :id'), {'id': printer_id}).scalar()
    slots = _printer_slots(conn, printer_id)
    mapping = _mapping(mapping if mapping is not None or not derive else job.get('ams_mapping'))
    if (derive and mapping is None and str(api_type or '').lower() == 'bambu' and len(slots) > 1
            and indexed and len(indexed) == 1):
        source = next(iter(indexed))  # the same derivation dispatch applies to a single-source file
        mapping = [-1] * source + [source]

    # The stored estimate (file and model) describes the first plate only. A job that prints
    # another plate is unknown, never judged with the first plate's grams (unknown never holds).
    other_plate = job.get('plate_index') not in (None, 0)
    grouped, order = {}, []
    for demand in _demands(mapping, indexed, model_grams, slots):
        if other_plate and demand['reason'] is None:
            demand.update(grams=None, reason='other_plate')
        key = demand['slot'] if demand['slot'] is not None else ('none', len(order))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(demand)
    items = [_judge(key if isinstance(key, int) else None, grouped[key],
                    slots.get(key) if isinstance(key, int) else None, prints, settings['margin_pct'])
             for key in order]
    statuses = {item['status'] for item in items}
    overall = 'short' if 'short' in statuses else ('enough' if items and statuses == {'enough'} else 'unknown')
    return {'job_id': job['id'], 'printer_id': printer_id, 'mode': settings['mode'],
            'margin_pct': settings['margin_pct'], 'prints': prints, 'overall': overall,
            'would_hold': overall == 'short' and settings['mode'] == 'hold',
            'slots': items, 'warnings': [i['message'] for i in items if i['status'] == 'short']}


def check_job(conn, job_id, printer_id) -> dict:
    """Result for the read route: what dispatch would decide right now."""
    settings = read_settings(conn)
    base = {'job_id': job_id, 'printer_id': printer_id, 'mode': settings['mode'],
            'margin_pct': settings['margin_pct'], 'prints': None, 'would_hold': False,
            'slots': [], 'warnings': []}
    if settings['mode'] == 'off':
        return {**base, 'overall': 'off'}
    from modules.printers.dispatch import _load_job
    job = _load_job(job_id, connection=conn)
    if job is None:  # no linked file: nothing to estimate from
        return {**base, 'overall': 'unknown'}
    return evaluate(conn, job=job, printer_id=printer_id, mapping=None, settings=settings)


def hide_spool_details(result) -> dict:
    """The same verdict without the printer owner's spool id or remaining weight.

    Used when the caller reaches the printer only because it is shared: whether the job fits is
    theirs to know, the owner's inventory is not.
    """
    slots = []
    for item in result.get('slots', []):
        item = dict(item, spool_id=None, remaining_g=None)
        label = _label(item.get('slot'), item.get('material'), item.get('color'))
        if item['status'] == 'enough':
            item['message'] = f'{label} has enough filament for this print'
        elif item['status'] == 'short':
            item['message'] = (f'{label} does not have enough filament; '
                               f"this print needs about {math.ceil(item['needed_g'])} g")
        slots.append(item)
    return {**result, 'slots': slots, 'spool_details_hidden': True,
            'warnings': [i['message'] for i in slots if i['status'] == 'short']}


# -------------------------------------------------------------------- gate

def _audit_details(job, result) -> dict:
    return {'printer_id': job.get('printer_id'), 'mode': result['mode'], 'margin_pct': result['margin_pct'],
            'prints': result['prints'],
            'slots': [{k: s[k] for k in ('slot', 'status', 'needed_g', 'remaining_g')}
                      for s in result['slots'] if s['status'] == 'short']}


def _record_audit(engine, action, job, result) -> None:
    try:
        from sqlalchemy.orm import Session
        from core.audit import audit_download
        with Session(engine) as db:
            audit_download(db, action, 'job', job['id'], _audit_details(job, result))
    except Exception:
        log.warning('[dispatch] job_id=%s filament check audit could not be recorded', job.get('id'))


def _report_hold(job, refusal, detail) -> None:
    """Timeline event and the job owner's alert, same record as other gate refusals."""
    from modules.printers import dispatch
    from modules.printers.dispatch_ams_gate import GateRefusal
    try:
        dispatch._notify_start_outcome(job, 'adapter_rejected',
                                       gate_refusal=GateRefusal(REFUSAL_CODE, detail), message=detail)
    except Exception as error:
        log.warning('[dispatch] job_id=%s filament hold could not be reported (%s)', job.get('id'), type(error).__name__)
    # No websocket 'failed' here: like the other pre-admission refusals, the dispatch worker
    # reports the refused result once.


def _raise_alert(engine, job, result, message) -> None:
    try:
        from sqlalchemy.orm import Session
        from core.base import AlertType, AlertSeverity
        from modules.notifications.alert_dispatcher import dispatch_alert
        creator = job.get('charged_to_user_id') or job.get('submitted_by')
        spool = next((s['spool_id'] for s in result['slots'] if s['status'] == 'short'), None)
        with Session(engine) as db:
            dispatch_alert(db=db, alert_type=AlertType.SPOOL_LOW, severity=AlertSeverity.WARNING,
                           title='Filament may run out mid-print', message=message,
                           printer_id=job.get('printer_id'), job_id=job['id'], spool_id=spool,
                           metadata={'reason': 'filament_check', 'slots': _audit_details(job, result)['slots']},
                           target_user_ids=[creator] if creator is not None else None)
    except Exception:
        log.warning('[dispatch] job_id=%s filament warning alert could not be raised', job.get('id'))


def _job_owns_printer(engine, job) -> bool:
    """True when the job is charged to the printer's own organization (or the printer has none).
    Anything unreadable counts as not the owner, so the wording stays the hidden one."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                'SELECT p.org_id, j.charged_to_org_id FROM jobs j JOIN printers p ON p.id = :printer '
                'WHERE j.id = :job'), {'printer': job['printer_id'], 'job': job['id']}).first()
    except Exception:
        return False
    return row is not None and (row[0] is None or row[0] == row[1])


@dataclass
class GateOutcome:
    refusal: Optional[str] = None
    warning: Optional[str] = None
    result: Optional[dict] = None

    def announce(self, engine, job) -> None:
        """Audit and alert a warning. Called only once admission has succeeded,
        so a dispatch refused for another reason raises no filament alert."""
        if self.warning and self.result:
            _record_audit(engine, 'job.filament_warning', job, self.result)
            _raise_alert(engine, job, self.result, self.warning)


def dispatch_gate(engine, job, mapping=None) -> GateOutcome:
    try:
        with engine.connect() as conn:
            settings = read_settings(conn)
            if settings['mode'] == 'off':
                return GateOutcome()
            result = evaluate(conn, job=job, printer_id=job['printer_id'], mapping=mapping, settings=settings, derive=False)
    except Exception:
        log.warning('[dispatch] job_id=%s filament check could not run; dispatch continues', job.get('id'))
        return GateOutcome()
    if not _job_owns_printer(engine, job):
        # A job sent to another organization's shared printer: its people get the verdict in the
        # refusal, warning and alert, never the owner's spool id or remaining weight.
        result = hide_spool_details(result)
    shorts = result['warnings']
    if not shorts:  # enough or unknown: unknown never holds and raises nothing
        return GateOutcome(result=result)
    message = '; '.join(shorts)
    if result['mode'] == 'hold':
        refusal = (f'{REFUSAL_CODE}: {message}. Load more filament, or ask a superadmin '
                   'to set the filament check to warn.')
        _record_audit(engine, 'job.filament_hold', job, result)
        _report_hold(job, refusal, message)
        return GateOutcome(refusal=refusal, result=result)
    return GateOutcome(warning=f'Filament warning: {message}', result=result)
