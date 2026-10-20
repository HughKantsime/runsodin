"""Read-only retained fences; inability to inspect is always blocked."""
from sqlalchemy import select,or_
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_sender_schema import DispatchSenderRegistration


def unresolved_target(engine,printer_id,physical_key,*,include_claims=False):
    try:
        with engine.connect() as conn:
            if include_claims:
                claims=PrinterDispatchClaim.__table__
                if conn.execute(select(claims.c.token).where(or_(claims.c.printer_id==printer_id,
                        claims.c.physical_key==physical_key)).limit(1)).first():return True
            senders=DispatchSenderRegistration.__table__
            return conn.execute(select(senders.c.claim_token).where(or_(senders.c.printer_id==printer_id,
                senders.c.physical_key==physical_key),senders.c.state.in_(('active','cleanup_uncertain'))).limit(1)).first() is not None
    except Exception:return True
