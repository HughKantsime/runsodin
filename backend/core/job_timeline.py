"""Best-effort post-commit history with a bounded queue and private DB connection.

The admission-only bed-clear event is deliberately excluded from this droppable path.
"""
import atexit
from collections import deque
from datetime import datetime, timezone
import json
import os
import re
import threading
import time
import uuid
from sqlalchemy import text
from sqlalchemy.pool import NullPool

# Values are bounded facts, never arbitrary strings or copies of transport messages.
DETAILS = {
    'dispatch_uploading': {}, 'dispatch_starting': {}, 'dispatch_dispatched': {},
    'dispatch_failed': {}, 'nozzle_unverified': {'geometry_unverified': bool}, 'gate_refused': {'gate': str}, 'started': {}, 'paused': {}, 'resumed': {},
    'runout': {'slot': int}, 'hms': {},
    'completed': {'duration_seconds': (int,float)}, 'failed': {'duration_seconds': (int,float)},
    'cancelled': {'duration_seconds': (int,float)},
    'inspection_recorded': {'accepted': int, 'rejected': int}, 'summary_sent': {},
}
_CODE = re.compile(r'[A-Z0-9_:-]{1,64}\Z')


def now():
    return datetime.now(timezone.utc)


def utc(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def event(job_id, kind, *, printer_id=None, print_attempt_uid=None, org_id=None,
          code=None, detail=None, occurred_at=None):
    if kind not in DETAILS or type(job_id) is not int or job_id <= 0:
        raise ValueError('Invalid timeline kind or job')
    detail = {} if detail is None else dict(detail)
    if set(detail) - set(DETAILS[kind]):raise ValueError('Invalid timeline detail field')
    for key,value in detail.items():
        if kind == 'nozzle_unverified' and key == 'geometry_unverified' and type(value) is bool:
            continue
        if kind == 'gate_refused' and key == 'gate' and isinstance(value, str) and _CODE.fullmatch(value):
            continue
        if isinstance(value,bool) or not isinstance(value,DETAILS[kind][key]) or not 0 <= value <= 31536000:
            raise ValueError('Invalid timeline detail value')
    if code is not None and (kind not in ('hms','gate_refused','runout') or not isinstance(code,str) or not _CODE.fullmatch(code)):
        raise ValueError('Invalid timeline code')
    return dict(id=uuid.uuid4().hex,job=job_id,kind=kind,printer=printer_id,attempt=print_attempt_uid,
                org=org_id,code=code,detail=json.dumps(detail,sort_keys=True),occurred=utc(occurred_at) if occurred_at is not None else now())


class TimelineWriter:
    def __init__(self, url, *, capacity=512, busy_timeout_ms=100, process_name="process"):
        if not 1 <= capacity <= 4096 or not 1 <= busy_timeout_ms <= 250:raise ValueError('Invalid timeline bounds')
        from core.database_config import create_database_engine
        role = {'mqtt_monitor':'monitor-bambu', 'moonraker_monitor':'monitor-moonraker',
                'prusalink_monitor':'monitor-prusalink', 'elegoo_monitor':'monitor-elegoo',
                'anycubic_lan_monitor':'monitor-anycubic-lan', 'vision_monitor':'vision'}.get(process_name, 'api')
        self.engine=create_database_engine(url, role=role, poolclass=NullPool,
                                           sqlite_busy_timeout_ms=busy_timeout_ms)
        self.timeout=busy_timeout_ms
        self.queue=deque();self.capacity=capacity;self.lock=threading.Lock()
        self.wake=threading.Event();self.stop=threading.Event()
        self.process_id=process_name+':'+str(os.getpid())+':'+uuid.uuid4().hex
        self.started=now();self.drop_epoch=0;self.error_epoch=0;self.epochs=deque(maxlen=1024);self.printers=set()
        # A failed registration is nonfatal; the worker retains epochs and retries.
        self.last_heartbeat=0.0
        self._drain()  # Best effort only; failures are retried by the worker.
        self.close_deadline=None
        self.thread=threading.Thread(target=self._run,name='job-timeline',daemon=True)
        self.thread.start()

    def enqueue(self, item):
        # No SQL, waiting for the writer, network, or shared DB connection here.
        with self.lock:
            if item['printer'] is not None:self.printers.add(item['printer'])
            if len(self.queue)>=self.capacity:
                self.queue.popleft();self.drop_epoch+=1
                self.epochs.append([now().isoformat(),'drop',self.drop_epoch])
            self.queue.append(item)
        self.wake.set()

    def _state(self, connection, drained, clean=False):
        with self.lock:
            data=dict(process=self.process_id,started=self.started,clean=int(clean),drained=drained,
                      drops=self.drop_epoch,errors=self.error_epoch,printers=json.dumps(sorted(self.printers)),
                      history=json.dumps(list(self.epochs)))
        connection.execute(text("INSERT INTO timeline_writer_state(process_id,started_at,clean_shutdown,last_drained_at,drop_epoch,error_epoch,printer_ids,epoch_history) VALUES(:process,:started,:clean,:drained,:drops,:errors,:printers,:history) ON CONFLICT(process_id) DO UPDATE SET clean_shutdown=:clean,last_drained_at=COALESCE(:drained,timeline_writer_state.last_drained_at),drop_epoch=:drops,error_epoch=:errors,printer_ids=:printers,epoch_history=:history"),data)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound

    def _drain(self):
        with self.lock:
            if not self.queue and not self.stop.is_set() and time.monotonic()-self.last_heartbeat < 5:
                return True
            items=[self.queue.popleft() for _ in range(min(32,len(self.queue)))]
        try:
            plans=[]
            with self.engine.begin() as c:
                if c.dialect.name=='postgresql':
                    c.exec_driver_sql("SET LOCAL lock_timeout = '100ms'")
                    c.exec_driver_sql("SET LOCAL statement_timeout = '250ms'")
                for original in items:
                    item=dict(original)
                    if 'resolve_printer' in item or 'resolve_attempt' in item:
                        if 'resolve_attempt' in item:
                            row=c.execute(text('SELECT scheduled_job_id,economic_uid,printer_id FROM print_jobs WHERE id=:id AND started_at<=:occurred'),{'id':item['resolve_attempt'],'occurred':item['occurred']}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
                        else:
                            rows=c.execute(text("SELECT scheduled_job_id,economic_uid,printer_id FROM print_jobs WHERE printer_id=:id AND started_at<=:occurred AND (ended_at IS NULL OR ended_at>=:occurred) ORDER BY id DESC LIMIT 2"),{'id':item['resolve_printer'],'occurred':item['occurred']}).all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
                            row=rows[0] if len(rows)==1 else None
                        if row is None or row[0] is None:continue
                        item.update(job=row[0],attempt=row[1],printer=row[2])
                        with self.lock:self.printers.add(row[2])
                    if item['printer'] is None:
                        target=c.execute(text('SELECT printer_id FROM jobs WHERE id=:id'),{'id':item['job']}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
                        if target is not None:item['printer']=target[0]
                    if item['printer'] is not None:
                        with self.lock:self.printers.add(item['printer'])
                    # Derive ownership from current authoritative job data. A deleted
                    # job drops its queued history rather than resurrecting it.
                    c.execute(text("INSERT INTO job_timeline_events(id,job_id,print_attempt_uid,printer_id,org_id,kind,code,detail,occurred_at) SELECT :id,j.id,:attempt,:printer,j.charged_to_org_id,:kind,:code,:detail,:occurred FROM jobs j WHERE j.id=:job ON CONFLICT(id) DO NOTHING"),item)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
                    if item['kind'] in ('paused','runout','failed') and item.get('attempt'):
                        from sqlalchemy.orm import Session
                        from modules.notifications.job_faults import stage
                        try:
                            with c.begin_nested():
                                with Session(bind=c) as fault_db:
                                    plan=stage(fault_db,item)
                                    fault_db.flush()
                            if plan is not None:plans.append(plan)
                        except Exception:
                            import logging
                            logging.getLogger('odin.job_timeline').warning('Fault staging failed; timeline retained')
                with self.lock:empty=not self.queue
                self._state(c,now() if empty else None,self.stop.is_set() and empty)
            self.last_heartbeat=time.monotonic()
            # External sends follow commit and cannot roll back history/claims.
            for plan in plans:
                try:
                    from sqlalchemy.orm import Session
                    from modules.notifications.alert_dispatcher import _deliver_staged_alert
                    from modules.notifications.alert_outbox import deliver_mobile
                    with Session(bind=self.engine) as delivery_db:
                        _deliver_staged_alert(delivery_db,plan)
                        deliver_mobile(delivery_db,plan)
                except Exception:
                    pass
            return True
        except Exception:
            with self.lock:
                self.error_epoch+=1;self.epochs.append([now().isoformat(),'error',self.error_epoch])
                # Bounded retries retain original order; new events cannot grow it.
                for item in reversed(items):self.queue.appendleft(item)
                while len(self.queue)>self.capacity:
                    self.queue.popleft();self.drop_epoch+=1
                    self.epochs.append([now().isoformat(),'drop',self.drop_epoch])
            return False

    def _run(self):
        while True:
            self.wake.wait(.2);self.wake.clear()
            succeeded=self._drain()
            if self.stop.is_set():
                with self.lock:empty=not self.queue
                if (succeeded and empty) or time.monotonic()>=self.close_deadline:
                    break
                self.wake.set()
        self.engine.dispose()

    def close(self):
        self.close_deadline=time.monotonic()+.8
        self.stop.set();self.wake.set();self.thread.join(timeout=1)


_writer=None
_writer_lock=threading.Lock()
_retry_timer=None


def start(process_name="process", url=None):
    with _writer_lock:
        return _start_writer(process_name, url)


def _start_writer(process_name, url):
    """Called under the startup lock, including by the retry timer."""
    global _writer
    if _writer is None:
        try:
            if url is None:
                from core.db import current_engine
                url = current_engine().url
            _writer = TimelineWriter(url, process_name=process_name)
        except Exception:
            _retry_start(process_name, url)
            return None
        atexit.register(_writer.close)
    return _writer


def _retry_start(process_name, url):
    global _retry_timer
    if _retry_timer is not None:
        return
    def retry():
        global _retry_timer
        with _writer_lock:
            if _retry_timer is not timer:
                return
            _retry_timer=None
            _start_writer(process_name, url)
    timer=threading.Timer(5, retry)
    timer.daemon=True
    _retry_timer=timer
    timer.start()


def shutdown():
    global _writer, _retry_timer
    with _writer_lock:
        if _retry_timer is not None:
            _retry_timer.cancel()
            _retry_timer=None
        if _writer is not None:
            _writer.close()
            _writer = None


def enqueue(item):
    try:
        # Runtime entrypoints register before accepting work. Embedded callers
        # must also start explicitly; producers never perform startup SQL.
        if _writer is None:
            return False
        _writer.enqueue(item)
        return True
    except Exception:
        return False


def for_printer(printer_id, kind, *, code=None, detail=None):
    """Resolve the physical identity in the writer, never on a telemetry caller."""
    # Producers with known job identity use event() directly. Printer-only
    # resolution is queued for the dedicated writer too.
    item=event(1,kind,printer_id=printer_id,code=code,detail=detail)
    if kind in ('paused','runout'):
        from modules.notifications.job_faults import notify
        notify(printer_id,kind,code)
    item['resolve_printer']=printer_id
    return enqueue(item)


def for_attempt(print_job_id, kind):
    item=event(1,kind)
    item['resolve_attempt']=print_job_id
    return enqueue(item)


def history_complete(db, printer_id, started_at, ended_at):
    """Conservative across crashes: missing or unclean writer evidence is incomplete."""
    rows=db.execute(text('SELECT process_id,started_at,clean_shutdown,last_drained_at,printer_ids,epoch_history,drop_epoch,error_epoch FROM timeline_writer_state')).mappings().all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
    relevant=[]
    def stamp(value):
        return utc(value)
    start,end=stamp(started_at),stamp(ended_at)
    for row in rows:
        born=stamp(row['started_at'])
        if born>end:continue
        if row['clean_shutdown'] and row['last_drained_at'] is not None and stamp(row['last_drained_at']) < start:
            continue
        role = row['process_id'].split(':')[0]
        successors = [stamp(other['started_at']) for other in rows
                      if other['process_id'].split(':')[0] == role
                      and stamp(other['started_at']) > born]
        restarted = min(successors) if successors else None
        # Even a successful drain cannot prove the final interval before a crash.
        if not row['clean_shutdown'] and restarted is not None:
            if restarted >= start:return False
            continue
        if not row['clean_shutdown'] and (row['last_drained_at'] is None or stamp(row['last_drained_at'])<end):return False
        if row['drop_epoch']+row['error_epoch']>len(json.loads(row['epoch_history'])):return False
        if any(start<=stamp(epoch[0])<=end for epoch in json.loads(row['epoch_history'])):return False
        if printer_id not in json.loads(row['printer_ids']):continue
        relevant.append(row)
        if row['drop_epoch']+row['error_epoch']>len(json.loads(row['epoch_history'])):return False
        if born>start or row['last_drained_at'] is None or stamp(row['last_drained_at'])<end:return False
        if any(start<=stamp(epoch[0])<=end for epoch in json.loads(row['epoch_history'])):return False
    return bool(relevant)
