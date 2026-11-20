"""Durable reminders for prints that remain paused. Uses existing delivery policy."""
import asyncio
import json
import logging
from datetime import datetime, timezone

from core.base import AlertSeverity, AlertType
from core.system_config_values import write_config_text
from modules.notifications import alert_dispatcher
from modules.notifications.alert_copy import human_name, humanize, job_names
from modules.notifications.pause_reminder_settings import read_json, read_settings

log = logging.getLogger('odin.pause_reminders')
LOCK_NAME = '.pause-reminders.lock'


def _utc(value):
    if not value:
        return None
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def describe(db, item, hours):
    days, remaining = divmod(hours, 24)
    span = f'{hours} hour' + ('' if hours == 1 else 's')
    if days:
        span = f'{days} day' + ('' if days == 1 else 's')
        if remaining:
            span += f' {remaining} hour' + ('' if remaining == 1 else 's')
    name = human_name(*job_names(db, item['job']), item['job_name'], item['filename'])
    title = f"{item['printer_name']}: {name} still paused after {span}"[:200]
    action = 'Check the printer, then resume or stop.'
    message = {
        'filament': 'Waiting for filament. Load filament at the printer, then resume.',
        'spaghetti': 'Paused for a possible failed print. Check the plate, then resume or stop.',
        'planned': 'Planned pause. Do what the pause is for, then resume.',
    }.get(item['cause'], action)
    if item['cause'] == 'error':
        episode = item['episode']
        _, first, _ = humanize(db, AlertType.PRINT_PAUSED, '', '', item['printer'], item['job'],
                               {**episode.get('context', {}), 'code': episode.get('code')})
        reason = first.split('Check the printer and filament, then retry or resume.')[0].strip()
        reason = reason or item['reason']
        message = reason.rstrip('. ') + '. ' + action
    return title, message


def run_pass(session_factory, *, now=None):
    """The caller holds the process lock. State and inbox rows commit together."""
    from core.interfaces.education_policy import is_education_reserved_filename
    from core.recipient_policy import education_notification_scope
    from modules.printers.services import paused_prints
    now = now or datetime.now(timezone.utc)
    raised, plans = [], []
    with session_factory() as db:
        settings = read_settings(db)
        if not settings['enabled']:
            return raised
        for item in paused_prints(db, now):
            try:
                if (is_education_reserved_filename(item['filename']) or
                        education_notification_scope(db, job_id=item['job'], printer_id=item['printer'])):
                    continue
                key = 'pause_reminder:' + str(item['printer'])
                episode = item['episode']
                previous = read_json(db, key)
                if previous.get('since') != episode['since']:
                    previous = {}
                sent = previous.get('sent', 0)
                if sent and (not settings['repeat_hours'] or
                             (now - _utc(previous['last_at'])).total_seconds() < settings['repeat_hours'] * 3600):
                    continue
                hours = item['paused_seconds'] // 3600
                title, message = describe(db, item, hours)
                plan = alert_dispatcher.stage_alert(
                    db, AlertType.PRINT_PAUSED, AlertSeverity.CRITICAL, title, message,
                    printer_id=item['printer'], job_id=item['job'],
                    metadata={'kind': 'pause_reminder', 'cause': item['cause'], 'hours': hours,
                              'since': episode['since'], 'reminder': sent + 1})
                write_config_text(db, key, json.dumps({'since': episode['since'], 'sent': sent + 1,
                                                      'last_at': now.isoformat()}))
                db.commit()
                raised.append(item['printer'])
                if plan is not None:
                    plans.append(plan)
            except Exception:
                db.rollback()
                log.warning('Pause reminder could not be raised for printer %s; will retry', item['printer'], exc_info=True)
        for plan in plans:
            try:
                alert_dispatcher.deliver_staged_alert(db, plan)
            except Exception:
                log.warning('Pause reminder delivery failed', exc_info=True)
    return raised


def check(*, session_factory=None, now=None):
    from core.audit_keys import file_lock
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    with file_lock(LOCK_NAME, blocking=False) as acquired:
        if acquired:
            return run_pass(session_factory, now=now)
    return []


async def run(interval=300, startup_delay=60):
    await asyncio.sleep(startup_delay)
    while True:
        try:
            await asyncio.to_thread(check)
        except Exception:
            log.warning('Pause reminder check failed; will retry', exc_info=True)
        await asyncio.sleep(interval)
