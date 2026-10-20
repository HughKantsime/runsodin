"""Internal transaction primitives, not initiating authorization or admission.

Caller must authorize and lock parent resources BEFORE these methods, and own
commit/rollback. No network actions are allowed while this transaction is open.
"""
import uuid
from dataclasses import dataclass
from sqlalchemy import text
from core.ams_evidence_schema import generations, current_reads
from core.runtime_identity import revalidate_runtime_identity
from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration
from modules.printers.telemetry.bambu.ams_database_clock import sample_database_clock


class AMSTransitionUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class StartedAMSRead:
    generation_id: str
    printer_id: int
    configuration_key: str
    owner_org_id: int | None
    runtime_incarnation: str


def _lock_printer(db, printer_id):
    if type(printer_id) is not int or printer_id <= 0:
        raise ValueError()
    dialect=db.get_bind().dialect.name
    if dialect=='sqlite':
        # Writer promotion before reading mutable authority. Caller owns rollback.
        row=db.execute(text('UPDATE printers SET id=id WHERE id=:id'),{'id':printer_id})
        if row.rowcount!=1:raise ValueError()
    elif dialect=='postgresql':
        if db.execute(text('SELECT id FROM printers WHERE id=:id FOR UPDATE NOWAIT'),{'id':printer_id}).scalar_one_or_none() is None:
            raise ValueError()
    else:raise ValueError()


def _current(db, printer_id):
    return db.execute(current_reads.select().where(current_reads.c.printer_id==printer_id).with_for_update(nowait=True)).mappings().one_or_none()


def _instant(value):
    from datetime import datetime, timezone
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if type(value) is not datetime:raise ValueError()
    # SQLite DateTime stores UTC without an offset; all writers use UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _begin_read(db, *, printer_id, runtime):
    """Internal only. No actor authorization is implied by a StartedAMSRead."""
    try:
        revalidate_runtime_identity(runtime)
        _lock_printer(db,printer_id)
        configured=load_ams_configuration(db,printer_id)
        pointer=_current(db,printer_id)
        now=sample_database_clock(db).database_now
        if pointer is not None:
            old=db.execute(generations.select().where(generations.c.generation_id==pointer['generation_id']).with_for_update(nowait=True)).mappings().one()
            if (old['printer_id']!=printer_id or old['runtime_incarnation']!=pointer['runtime_incarnation']
                    or now < _instant(old['started_at'])):raise ValueError()
            if old['state']=='acquiring':
                changed=db.execute(generations.update().where(generations.c.generation_id==old['generation_id'],
                    generations.c.state=='acquiring').values(state='failed',completed_at=now))
                if changed.rowcount!=1:raise ValueError()
            elif old['state']=='ready':
                if now < _instant(old['completed_at']):raise ValueError()
                changed=db.execute(generations.update().where(generations.c.generation_id==old['generation_id'],
                    generations.c.state=='ready').values(state='invalidated',invalidated_at=now))
                if changed.rowcount!=1:raise ValueError()
            else:raise ValueError()
            removed=db.execute(current_reads.delete().where(current_reads.c.printer_id==printer_id,
                current_reads.c.generation_id==pointer['generation_id'],
                current_reads.c.runtime_incarnation==pointer['runtime_incarnation']))
            if removed.rowcount!=1:raise ValueError()
        generation=uuid.uuid4().hex
        db.execute(generations.insert().values(generation_id=generation,printer_id=printer_id,
            owner_org_id=configured.owner_org_id,configuration_key=configured.configuration_key,
            runtime_incarnation=runtime.incarnation,started_at=now,state='acquiring'))
        db.execute(current_reads.insert().values(printer_id=printer_id,generation_id=generation,
            runtime_incarnation=runtime.incarnation))
        revalidate_runtime_identity(runtime)
        return StartedAMSRead(generation,printer_id,configured.configuration_key,
                              configured.owner_org_id,runtime.incarnation)
    except Exception:
        raise AMSTransitionUnavailable('AMS read transition unavailable; rollback required') from None


def _fail_owned_read(db, *, started, runtime):
    """A late failure may retire its own row, never change a newer pointer."""
    try:
        if type(started) is not StartedAMSRead:raise ValueError()
        revalidate_runtime_identity(runtime)
        if runtime.incarnation!=started.runtime_incarnation:raise ValueError()
        _lock_printer(db,started.printer_id)
        pointer=_current(db,started.printer_id)
        row=db.execute(generations.select().where(generations.c.generation_id==started.generation_id).with_for_update(nowait=True)).mappings().one()
        if (row['printer_id']!=started.printer_id or row['runtime_incarnation']!=started.runtime_incarnation
                or row['configuration_key']!=started.configuration_key
                or row['owner_org_id']!=started.owner_org_id):raise ValueError()
        if row['state']!='acquiring':return False
        now=sample_database_clock(db).database_now
        if now < _instant(row['started_at']):raise ValueError()
        changed=db.execute(generations.update().where(generations.c.generation_id==started.generation_id,
            generations.c.state=='acquiring').values(state='failed',completed_at=now))
        if changed.rowcount!=1:raise ValueError()
        # Terminal generations cannot remain current. Delete only the exact
        # owned pointer; a newer acquisition's pointer must remain untouched.
        if pointer is not None and pointer['generation_id']==started.generation_id:
            changed_pointer=db.execute(current_reads.delete().where(
                current_reads.c.printer_id==started.printer_id,
                current_reads.c.generation_id==started.generation_id,
                current_reads.c.runtime_incarnation==started.runtime_incarnation))
            if changed_pointer.rowcount!=1:raise ValueError()
        revalidate_runtime_identity(runtime)
        return True
    except Exception:
        raise AMSTransitionUnavailable('AMS read transition unavailable; rollback required') from None


@dataclass(frozen=True)
class _PendingAMSReport:
    started: StartedAMSRead
    packet_id: str
    received_at: object
    clock_domain: str
    provenance: str
    facts: tuple


def _freeze_read_result(db, *, started, result, runtime, selected_slots):
    """Internal service-owned conversion, once before any persistence retry."""
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_database_clock import anchor_original_receipt
    try:
        revalidate_runtime_identity(runtime)
        if (type(started) is not StartedAMSRead or type(result) is not RuntimeBoundAMSResult
                or result.success is not True or result.cleanup_confirmed is not True
                or result.error is not None or result.runtime_incarnation!=runtime.incarnation
                or started.runtime_incarnation!=runtime.incarnation
                or result.clock_domain!=runtime.clock_domain
                or type(result.snapshot) is not RawAMSSnapshot):raise ValueError()
        snapshot=result.snapshot
        if (snapshot.incarnation!=started.generation_id
                or snapshot.configuration_key!=started.configuration_key
                or snapshot.owner_org_id!=started.owner_org_id
                or type(selected_slots) not in (tuple,list) or not selected_slots
                or any(type(s) is not int or not 1<=s<=16 for s in selected_slots)
                or len(set(selected_slots))!=len(selected_slots)
                or set(f.slot_number for f in snapshot.trays)!=set(selected_slots)):raise ValueError()
        row=db.execute(generations.select().where(generations.c.generation_id==started.generation_id)).mappings().one()
        estimate=anchor_original_receipt(db,received_monotonic=snapshot.received_monotonic,
            receipt_clock_domain=result.clock_domain,runtime=runtime,generation_started_at=_instant(row['started_at']))
        return _PendingAMSReport(started,snapshot.packet_id,estimate.received_at,
                                 estimate.clock_domain,estimate.provenance_version,snapshot.trays)
    except Exception:
        raise AMSTransitionUnavailable('AMS report freeze unavailable') from None


def _persist_report(db, *, pending, runtime):
    """Private immutable persistence, never hardware/initiator authorization.

    Retry the identical pending object after rollback; no read or renewed receipt.
    """
    import re
    from core.ams_evidence_schema import packets,trays,SCHEMA_MATERIALS
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    try:
        revalidate_runtime_identity(runtime)
        if type(pending) is not _PendingAMSReport or type(pending.started) is not StartedAMSRead:raise ValueError()
        started=pending.started
        if (runtime.incarnation!=started.runtime_incarnation or runtime.clock_domain!=pending.clock_domain
                or type(pending.packet_id) is not str or not re.fullmatch('[0-9a-f]{32}',pending.packet_id)
                or pending.provenance!='db-clock-estimate-v1'
                or type(pending.facts) is not tuple or not 1<=len(pending.facts)<=16):raise ValueError()
        expected=[]
        for fact in pending.facts:
            if (type(fact) is not RawTrayFact
                    or any(type(v) is not int for v in (fact.slot_number,fact.unit_id,fact.tray_id))
                    or not 0<=fact.unit_id<=3 or not 0<=fact.tray_id<=3
                    or fact.slot_number!=fact.unit_id*4+fact.tray_id+1
                    or fact.material_state!='reported' or fact.material not in SCHEMA_MATERIALS
                    or fact.mechanically_ready is not None):raise ValueError()
            expected.append((fact.slot_number,fact.unit_id,fact.tray_id,fact.material,fact.material_state,None))
        if len({f[0] for f in expected})!=len(expected):raise ValueError()
        expected.sort()
        _lock_printer(db,started.printer_id)
        configured=load_ams_configuration(db,started.printer_id)
        if configured.configuration_key!=started.configuration_key or configured.owner_org_id!=started.owner_org_id:raise ValueError()
        pointer=_current(db,started.printer_id)
        if pointer is None or pointer['generation_id']!=started.generation_id or pointer['runtime_incarnation']!=runtime.incarnation:raise ValueError()
        generation=db.execute(generations.select().where(generations.c.generation_id==started.generation_id).with_for_update(nowait=True)).mappings().one()
        if (generation['printer_id']!=started.printer_id or generation['owner_org_id']!=started.owner_org_id
                or generation['configuration_key']!=started.configuration_key
                or generation['runtime_incarnation']!=runtime.incarnation
                or generation['state'] not in ('acquiring','ready')):raise ValueError()
        from datetime import datetime
        if (type(pending.received_at) is not datetime or pending.received_at.tzinfo is None
                or pending.received_at.utcoffset() is None):raise ValueError()
        received=_instant(pending.received_at)
        now=sample_database_clock(db).database_now
        if received < _instant(generation['started_at']) or not 0<=(now-received).total_seconds()<120:raise ValueError()
        existing=db.execute(packets.select().where(packets.c.packet_id==pending.packet_id)).mappings().one_or_none()
        if existing is not None:
            facts=db.execute(trays.select().where(trays.c.packet_sequence==existing['packet_sequence'])).mappings().all()
            actual=sorted((f['slot_number'],f['unit_id'],f['tray_id'],f['material'],f['material_state'],f['mechanically_ready']) for f in facts)
            if (generation['state']!='ready' or existing['generation_id']!=started.generation_id
                    or _instant(existing['received_at'])!=received or existing['receipt_provenance']!=pending.provenance
                    or actual!=expected or received>_instant(generation['completed_at'])):raise ValueError()
            sequence=existing['packet_sequence']
        else:
            if generation['state']!='acquiring':raise ValueError()
            if db.execute(packets.select().where(packets.c.generation_id==started.generation_id)).first() is not None:raise ValueError()
            sequence=db.execute(packets.insert().values(packet_id=pending.packet_id,generation_id=started.generation_id,
                received_at=received,receipt_provenance=pending.provenance)).inserted_primary_key[0]
            for fact in expected:
                db.execute(trays.insert().values(packet_sequence=sequence,slot_number=fact[0],unit_id=fact[1],
                    tray_id=fact[2],material=fact[3],material_state=fact[4],mechanically_ready=None))
            changed=db.execute(generations.update().where(generations.c.generation_id==started.generation_id,
                generations.c.state=='acquiring').values(state='ready',completed_at=now))
            if changed.rowcount!=1:raise ValueError()
        revalidate_runtime_identity(runtime)
        return sequence
    except Exception:
        raise AMSTransitionUnavailable('AMS report persistence unavailable; rollback required') from None
