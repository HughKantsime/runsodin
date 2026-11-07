"""Cache only actual Square count reads, scoped to the existing owner mapping."""
from datetime import datetime, timezone
from sqlalchemy import text
from core.job_timeline import utc


def record(db, owner, variation, location, result, *, read_at=None):
    at = read_at or datetime.now(timezone.utc)
    if result.get('state') != 'known' or result.get('calculated_at') is None:
        # An unknown read invalidates earlier evidence instead of leaving a fresh
        # but no longer trustworthy value available for ranking.
        db.execute(text('DELETE FROM stock_snapshots WHERE owner_scope=:scope AND owner_key=:owner AND variation=:variation AND location=:location'),
                   dict(scope='org' if owner else 'global', owner=owner, variation=variation, location=location))
        return
    try: calculated = utc(result['calculated_at'])
    except (TypeError, ValueError): return
    db.execute(text('INSERT INTO stock_snapshots(owner_scope,owner_key,variation,location,quantity,calculated_at,read_at) VALUES(:scope,:owner,:variation,:location,:quantity,:calculated,:read) ON CONFLICT(owner_scope,owner_key,variation,location) DO UPDATE SET quantity=:quantity,calculated_at=:calculated,read_at=:read'),
        dict(scope='org' if owner else 'global', owner=owner, variation=variation, location=location,
             quantity=result['quantity'], calculated=calculated, read=at))


def best_effort(engine, owner, reads):
    """Cache remote read results in a short, independent transaction."""
    try:
        from sqlalchemy.orm import Session
        with Session(engine) as cache:
            with cache.begin():
                for variation, location, result, read_at in reads:
                    record(cache, owner, variation, location, result, read_at=read_at)
    except Exception:
        import logging
        logging.getLogger('odin.stock_snapshots').warning('Stock snapshot cache write failed')
