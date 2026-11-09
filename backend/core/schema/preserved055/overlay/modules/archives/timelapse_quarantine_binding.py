"""Confirm quarantine identity only. No media move, deletion or endpoint wiring."""
from dataclasses import dataclass
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from core.timelapse_manifest import ObjectIdentity,_root
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_quarantine import quarantine_directory
from core.timelapse_recovery_schema import quarantine
from modules.archives.timelapse_recovery_evidence import STABLE
from modules.archives.timelapse_retirement import (_floor,_actor,_owned,_snapshot,
    _persisted,_write_boundary,RetirementUnavailable)


class QuarantineBindingUnavailable(RuntimeError):
    def __init__(self,reason='file_io'):
        self.reason=reason
        super().__init__('Quarantine binding could not be confirmed')


@dataclass(frozen=True)
class BoundQuarantine:
    operation_id:str
    created:bool


def _refuse(reason='phase_uncertain'):
    raise QuarantineBindingUnavailable(reason) from None


def _binding(db,operation):
    row=db.execute(quarantine.select().where(quarantine.c.operation_id==operation)).mappings().first()
    return dict(row) if row is not None else None


def _identity(row):
    return ObjectIdentity('.retirement','directory',**{name:row[name] for name in STABLE})


def bind_quarantine(engine,root,timelapse_id,principal):
    """Bind a complete existing plan to one synced protected directory identity."""
    if not isinstance(engine,Engine) or type(timelapse_id) is not int or timelapse_id<=0:_refuse('unknown_authority')
    try:
        _floor(principal)
        with acquire_media_guard(root,timelapse_id) as guard:
            with _root(root) as (_,root_identity):
                guard.assert_root_identity(root_identity)
            with Session(engine) as reader:
                _actor(reader,principal)
                media,owner=_owned(reader,timelapse_id,principal)
                if media.retirement_operation_id is None:_refuse()
                _persisted(reader,media,owner,root_identity)
                before=_snapshot(media);operation=media.retirement_operation_id
                original=_binding(reader,operation)
            expected=_identity(original) if original is not None else None
            # No reader transaction/row locks span directory creation or fsync.
            with quarantine_directory(root,root_identity,expected,create=original is None) as (_,captured):
                with Session(engine) as db:
                    _write_boundary(db);_actor(db,principal,lock=True)
                    media,owner=_owned(db,timelapse_id,principal,lock=True)
                    if _snapshot(media)!=before:_refuse('ownership_changed')
                    _persisted(db,media,owner,root_identity)
                    if _binding(db,operation)!=original:_refuse()
                    guard.assert_root_identity(root_identity)
                    # Confirm the same path/directory again before DB acknowledgment.
                    with quarantine_directory(root,root_identity,captured):pass
                    if original is not None:
                        db.rollback()
                        return BoundQuarantine(operation,False)
                    db.execute(quarantine.insert(),{'operation_id':operation,**{name:getattr(captured,name) for name in STABLE}})
                    from core.audit import log_audit
                    log_audit(db,'retirement_quarantine','timelapse',timelapse_id,{'operation_id':operation,'actor_id':principal['id']})
                    db.commit()
                guard.assert_owned()
                return BoundQuarantine(operation,True)
    except QuarantineBindingUnavailable:raise
    except RetirementUnavailable as error:_refuse(error.reason)
    except Exception:_refuse('file_io')
