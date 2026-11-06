"""Coalesced license-lapse observations with chained opening and closing records.

Counters are operational state, not authenticated audit payload. Updating them
never rehashes old audit rows. The append lock serializes every open/extend/close.
Closing snapshots the final totals into immutable audit content for SIEM delivery.
"""
from core.audit_integrity import hash_expression


def chain_gap_sql():
    """SQLite nested triggers may be disabled: chain each synthetic insert here."""
    return f"""UPDATE audit_logs SET prev_hash=(SELECT head_hash FROM audit_chain_state WHERE id=1),
        chain_seq=(SELECT head_seq+1 FROM audit_chain_state WHERE id=1),hash_salt=lower(hex(randomblob(32)))
        WHERE id=last_insert_rowid() AND id!=NEW.id AND action IN ('siem.gap','siem.gap_closed') AND chain_seq=-1;
      UPDATE audit_logs SET row_hash=(SELECT {hash_expression(False, 'a.')} FROM audit_logs a WHERE a.id=last_insert_rowid())
        WHERE id=last_insert_rowid() AND id!=NEW.id AND action IN ('siem.gap','siem.gap_closed')
          AND row_hash IS NULL AND chain_seq=(SELECT head_seq+1 FROM audit_chain_state WHERE id=1);
      UPDATE audit_chain_state SET head_hash=(SELECT row_hash FROM audit_logs WHERE id=last_insert_rowid()),
        head_seq=(SELECT chain_seq FROM audit_logs WHERE id=last_insert_rowid())
        WHERE id=1 AND EXISTS(SELECT 1 FROM audit_logs WHERE id=last_insert_rowid() AND id!=NEW.id
          AND action IN ('siem.gap','siem.gap_closed') AND chain_seq>audit_chain_state.head_seq);"""


def gap_sql(pg):
    prefix = 'public.' if pg else ''
    now = 'extract(epoch FROM clock_timestamp())' if pg else "CAST(strftime('%s','now') AS REAL)"
    lapsed = (f'(SELECT siem_until FROM {prefix}audit_chain_state WHERE id=1)<={now} '
              f'AND EXISTS(SELECT 1 FROM {prefix}audit_siem_destinations WHERE enabled=1)')
    ordinary = "NEW.action NOT IN ('siem.gap','siem.gap_closed')"
    if pg:
        return f"""WITH closed AS (
   UPDATE public.audit_siem_gaps SET closed=1 WHERE closed=0 AND {ordinary} AND NOT ({lapsed})
   RETURNING gap_id,first_id,last_id,skipped)
 INSERT INTO public.audit_logs(action,entity_type,details)
   SELECT 'siem.gap_closed','audit_log',json_build_object('reason','license_unavailable',
     'state','closed','gap_id',gap_id,'first_id',first_id,'last_id',last_id,'skipped',skipped) FROM closed;
 IF {ordinary} AND {lapsed} THEN
   IF NOT EXISTS(SELECT 1 FROM public.audit_siem_gaps WHERE closed=0) THEN
     INSERT INTO public.audit_logs(action,entity_type,details)
       VALUES('siem.gap','audit_log',json_build_object('reason','license_unavailable','first_id',NEW.id,'state','open'))
       RETURNING id INTO gap_record;
     INSERT INTO public.audit_siem_gaps(gap_id,first_id,last_id,skipped,closed)
       VALUES(gap_record,NEW.id,NEW.id,0,0);
   END IF;
   UPDATE public.audit_siem_gaps SET skipped=skipped+1,last_id=NEW.id WHERE closed=0;
 END IF;"""
    return f"""INSERT INTO audit_logs(action,entity_type,chain_seq,details)
   SELECT 'siem.gap_closed','audit_log',-1,json_object('reason','license_unavailable',
     'state','closed','gap_id',gap_id,'first_id',first_id,'last_id',last_id,'skipped',skipped)
   FROM audit_siem_gaps WHERE closed=0 AND {ordinary} AND NOT ({lapsed});
 {chain_gap_sql()}
 INSERT INTO audit_siem_outbox(destination_id,audit_id)
   SELECT id,last_insert_rowid() FROM audit_siem_destinations WHERE enabled=1
   AND (SELECT siem_until FROM audit_chain_state WHERE id=1)>{now}
   AND EXISTS(SELECT 1 FROM audit_logs WHERE id=last_insert_rowid() AND id!=NEW.id
     AND action='siem.gap_closed') ON CONFLICT DO NOTHING;
 UPDATE audit_siem_gaps SET closed=1 WHERE closed=0 AND {ordinary} AND NOT ({lapsed});
 INSERT INTO audit_logs(action,entity_type,chain_seq,details)
   SELECT 'siem.gap','audit_log',-1,json_object('reason','license_unavailable','first_id',NEW.id,'state','open')
   WHERE {ordinary} AND {lapsed}
     AND NOT EXISTS(SELECT 1 FROM audit_siem_gaps WHERE closed=0);
 INSERT INTO audit_siem_gaps(gap_id,first_id,last_id,skipped,closed)
   SELECT id,NEW.id,NEW.id,0,0 FROM audit_logs
   WHERE id=last_insert_rowid() AND id!=NEW.id AND action='siem.gap' AND chain_seq=-1
     AND json_extract(details,'$.reason')='license_unavailable';
 {chain_gap_sql()}
 UPDATE audit_siem_gaps SET skipped=skipped+1,last_id=NEW.id
   WHERE closed=0 AND {ordinary} AND {lapsed};"""
