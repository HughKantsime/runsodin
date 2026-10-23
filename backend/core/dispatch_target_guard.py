"""Read-only retained fences; inability to inspect is always blocked."""
from contextlib import nullcontext
from sqlalchemy import select,or_,text
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_sender_schema import DispatchSenderRegistration


def unresolved_target(engine,printer_id,physical_key,*,include_claims=False, power_owner=None, connection=None):
    try:
        with (nullcontext(connection) if connection is not None else engine.begin()) as conn:
            from core.power_target_operations import _history
            _history(conn)
            # Only the creating process's exact private owner can exclude its
            # own row. Uncertainty has no TTL and survives restart/source deletion.
            from core.power_target_operations import _StorageOwner, _SEAL
            import os
            owner = (power_owner if type(power_owner) is _StorageOwner
                     and power_owner.seal is _SEAL and power_owner.process_id == os.getpid() else None)
            query = "SELECT operation_nonce,owner_nonce FROM power_target_operations WHERE state<>'complete' AND (printer_id=:pid OR target_key=:key)"
            for row in conn.execute(text(query), {'pid':printer_id,'key':physical_key}):
                if owner is None or tuple(row) != (owner.operation_nonce,owner.owner_nonce):
                    return True
            if include_claims:
                claims=PrinterDispatchClaim.__table__
                if conn.execute(select(claims.c.token).where(or_(claims.c.printer_id==printer_id,
                        claims.c.physical_key==physical_key)).limit(1)).first():return True
            senders=DispatchSenderRegistration.__table__
            return conn.execute(select(senders.c.claim_token).where(or_(senders.c.printer_id==printer_id,
                senders.c.physical_key==physical_key),senders.c.state.in_(('active','cleanup_uncertain'))).limit(1)).first() is not None
    except Exception:return True
