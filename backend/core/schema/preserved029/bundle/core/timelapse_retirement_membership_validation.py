"""Exact storage membership guards and retained checks; never touch media."""
from sqlalchemy import text
from core.schema.migrator import MigrationError
from core.timelapse_retirement_membership_schema import bindings,members,receipts
B=bindings.name;M=members.name;R=receipts.name
STABLE=('device','inode','uid','gid','mode')
FILE=STABLE+('link_count','byte_count','mtime_ns')
def binding_proof(n='NEW'):
 return f"""EXISTS(SELECT 1 FROM timelapse_retirements i JOIN timelapse_media_versions v ON v.version_nonce={n}.version_nonce AND v.lifetime_nonce={n}.lifetime_nonce JOIN timelapse_media_lifetimes l ON l.lifetime_nonce=v.lifetime_nonce JOIN timelapse_ownership_anchors a ON a.operation_id=v.version_nonce JOIN timelapses t ON t.id=i.timelapse_id WHERE i.operation_id={n}.operation_id AND i.state='pending' AND i.operation_id=v.version_nonce AND t.ownership_operation_id=v.version_nonce AND t.retirement_operation_id=i.operation_id AND t.status='deleting' AND i.printer_id=t.printer_id AND i.filename=t.filename AND a.timelapse_id=t.id AND a.printer_id=t.printer_id AND a.filename=t.filename AND a.owner_state=i.owner_state AND a.owner_org_id IS NOT DISTINCT FROM i.owner_org_id AND i.root_device=l.root_device AND i.root_inode=l.root_inode)"""
def member_proof(n='NEW'):
 stable=' AND '.join(f'o.{k}=f.{k}' for k in STABLE)
 file=' AND '.join(f'o.{k}=f.{k}' for k in FILE)
 ns=' AND '.join(f'o.{k}=s.{k}' for k in STABLE)
 return f"""EXISTS(SELECT 1 FROM {B} b JOIN timelapse_retirement_objects o ON o.operation_id=b.operation_id AND o.object_index={n}.object_index LEFT JOIN timelapse_frame_namespaces s ON s.namespace_id={n}.namespace_id LEFT JOIN timelapse_publications p ON p.operation_id={n}.publication_id LEFT JOIN timelapse_publication_facts f ON f.operation_id=p.operation_id AND f.role={n}.fact_role AND f.relative_path={n}.fact_path LEFT JOIN timelapse_retained_versions r ON r.version_nonce={n}.retained_version_nonce WHERE b.operation_id={n}.operation_id AND (
 ({n}.role='frame_namespace' AND s.lifetime_nonce=b.lifetime_nonce AND o.kind='directory' AND o.relative_path=s.relative_path AND ((o.state='originally_absent' AND s.phase IN ('planned','blocked') AND s.inode IS NULL) OR (o.state<>'originally_absent' AND (s.phase='bound' OR (s.phase='retired' AND EXISTS(SELECT 1 FROM {R} q WHERE q.operation_id=b.operation_id AND q.object_index=o.object_index AND q.role='frame_namespace'))) AND {ns}))) OR
 ({n}.role='completed_frame' AND s.lifetime_nonce=b.lifetime_nonce AND p.kind='frame' AND p.frame_namespace_id=s.namespace_id AND p.source_lifetime=b.lifetime_nonce AND p.phase='complete' AND p.state='complete' AND o.kind='file' AND o.state<>'originally_absent' AND o.relative_path=p.destination_path AND {file} AND o.ctime_ns=p.published_ctime_ns) OR
 ({n}.role='current_video' AND {n}.version_nonce=b.version_nonce AND p.result_nonce=b.version_nonce AND p.result_lifetime=b.lifetime_nonce AND p.kind IN ('encode','trim','speed') AND p.phase='complete' AND p.state='complete' AND o.kind='file' AND o.state<>'originally_absent' AND o.relative_path=p.destination_path AND {file} AND o.ctime_ns=p.published_ctime_ns) OR
 ({n}.role='retained_video' AND r.version_nonce={n}.version_nonce AND r.lifetime_nonce=b.lifetime_nonce AND (r.state='retained' OR (r.state='retired' AND r.retire_operation_id=b.operation_id)) AND r.retained_by_operation=p.operation_id AND p.kind='trim' AND p.source_nonce=r.version_nonce AND p.source_lifetime=b.lifetime_nonce AND p.phase='complete' AND p.state='complete' AND o.kind='file' AND o.state<>'originally_absent' AND o.relative_path=r.filename AND f.relative_path=r.filename AND {file} AND o.ctime_ns=f.ctime_ns) OR
 ({n}.role='private_directory' AND (p.source_lifetime=b.lifetime_nonce OR p.result_lifetime=b.lifetime_nonce) AND p.phase='complete' AND p.state='complete' AND o.kind='directory' AND o.state<>'originally_absent' AND o.relative_path=p.private_directory AND {stable}) OR
 ({n}.role='private_output' AND (p.source_lifetime=b.lifetime_nonce OR p.result_lifetime=b.lifetime_nonce) AND p.phase='complete' AND p.state='complete' AND o.kind='file' AND o.relative_path=p.private_directory||'/'||p.private_name AND o.state='originally_absent')
 ))"""
def zero_proof(n,sqlite=False):
 # Mutable audit authority is consumed once at building→sealed admission only.
 details=("json_valid(a.details) AND json_extract(a.details,'$.reason')='insufficient_frames' AND json_extract(a.details,'$.ownership_nonce')="+n+".version_nonce AND json_type(a.details,'$.frame_count')='integer' AND json_extract(a.details,'$.frame_count')=0") if sqlite else ("(a.details::jsonb)->>'reason'='insufficient_frames' AND (a.details::jsonb)->>'ownership_nonce'="+n+".version_nonce AND jsonb_typeof((a.details::jsonb)->'frame_count')='number' AND (a.details::jsonb)->>'frame_count'='0'")
 return f"(SELECT count(*) FROM audit_logs a JOIN timelapse_retirements i ON i.timelapse_id=a.entity_id WHERE i.operation_id={n}.operation_id AND a.action='timelapse_encode_failed' AND a.entity_type='timelapse' AND {details})=1"
def seal_proof(n='NEW',sqlite=False,admission=True):
 # Once sealed, immutable binding/member/object counts are the zero-history proof.
 # Audit retention and metadata deletion/ID reuse cannot revoke that admitted history.
 zero=f"NOT EXISTS(SELECT 1 FROM timelapse_frame_namespaces s WHERE s.lifetime_nonce={n}.lifetime_nonce) AND NOT EXISTS(SELECT 1 FROM timelapse_publications p WHERE p.source_lifetime={n}.lifetime_nonce OR p.result_lifetime={n}.lifetime_nonce)"
 if admission:zero+=f" AND {zero_proof(n,sqlite)} AND EXISTS(SELECT 1 FROM timelapses t WHERE t.retirement_operation_id={n}.operation_id AND t.status='deleting' AND t.frame_count=0 AND t.completed_at IS NOT NULL AND t.duration_seconds IS NULL AND t.file_size_mb IS NULL)"
 return f"(SELECT count(*) FROM {M} WHERE operation_id={n}.operation_id)={n}.object_count AND (SELECT count(*) FROM timelapse_retirement_objects WHERE operation_id={n}.operation_id)={n}.object_count AND NOT EXISTS(SELECT 1 FROM {M} z WHERE z.operation_id={n}.operation_id AND (z.object_index>={n}.object_count OR NOT ({member_proof('z')}))) AND ({n}.object_count>0 OR ({zero}))"
def static_binding_proof(n='NEW'):
 return f"EXISTS(SELECT 1 FROM timelapse_retirements i JOIN timelapse_media_versions v ON v.version_nonce={n}.version_nonce AND v.lifetime_nonce={n}.lifetime_nonce JOIN timelapse_media_lifetimes l ON l.lifetime_nonce=v.lifetime_nonce JOIN timelapse_ownership_anchors a ON a.operation_id=v.version_nonce WHERE i.operation_id={n}.operation_id AND i.operation_id=v.version_nonce AND i.timelapse_id=a.timelapse_id AND i.printer_id=a.printer_id AND i.filename=a.filename AND i.owner_state=a.owner_state AND i.owner_org_id IS NOT DISTINCT FROM a.owner_org_id AND i.root_device=l.root_device AND i.root_inode=l.root_inode)"
def receipt_proof(n='NEW'):
 return f"""EXISTS(SELECT 1 FROM {B} b JOIN {M} m ON m.operation_id=b.operation_id JOIN timelapse_retirement_objects o ON o.operation_id=m.operation_id AND o.object_index=m.object_index JOIN timelapse_retirement_moves v ON v.operation_id=o.operation_id AND v.object_index=o.object_index JOIN timelapse_retirement_unlinks u ON u.operation_id=o.operation_id AND u.object_index=o.object_index WHERE b.operation_id={n}.operation_id AND b.phase='sealed' AND m.object_index={n}.object_index AND m.role={n}.role AND o.kind='directory' AND o.state='retired' AND v.kind='directory' AND v.phase='staged' AND v.verified_ctime_ns IS NOT NULL AND u.phase='retired')"""
def completion_proof(n='NEW'):
 return f"""EXISTS(SELECT 1 FROM {B} b WHERE b.operation_id={n}.operation_id AND b.phase='sealed' AND {static_binding_proof('b')} AND (SELECT count(*) FROM {M} WHERE operation_id=b.operation_id)=b.object_count AND (SELECT count(*) FROM timelapse_retirement_objects WHERE operation_id=b.operation_id)=b.object_count AND NOT EXISTS(SELECT 1 FROM {M} z WHERE z.operation_id=b.operation_id AND (z.object_index>=b.object_count OR NOT ({member_proof('z')}))) AND NOT EXISTS(SELECT 1 FROM timelapse_retirement_objects o WHERE o.operation_id=b.operation_id AND o.state NOT IN ('retired','originally_absent')) AND NOT EXISTS(SELECT 1 FROM {M} m JOIN timelapse_retirement_objects o ON o.operation_id=m.operation_id AND o.object_index=m.object_index WHERE m.operation_id=b.operation_id AND o.state='retired' AND (NOT EXISTS(SELECT 1 FROM timelapse_retirement_moves v JOIN timelapse_retirement_unlinks u ON u.operation_id=v.operation_id AND u.object_index=v.object_index WHERE v.operation_id=o.operation_id AND v.object_index=o.object_index AND v.phase='staged' AND v.verified_ctime_ns IS NOT NULL AND u.phase='retired') OR (m.role IN ('frame_namespace','private_directory') AND NOT EXISTS(SELECT 1 FROM {R} r WHERE r.operation_id=m.operation_id AND r.object_index=m.object_index AND r.role=m.role AND r.phase='complete')))) AND NOT EXISTS(SELECT 1 FROM {R} r WHERE r.operation_id=b.operation_id AND NOT ({receipt_proof('r')})))"""
def conditions(sqlite=False):
 immutable=' OR '.join(f'NEW.{k} IS DISTINCT FROM OLD.{k}' for k in ('operation_id','version_nonce','lifetime_nonce','object_count','created_at'))
 object_identity=' OR '.join(f'NEW.{k} IS DISTINCT FROM OLD.{k}' for k in ('operation_id','object_index','relative_path','kind','device','inode','uid','gid','mode','link_count','byte_count','mtime_ns','ctime_ns','quarantine_name'))
 intent_identity=' OR '.join(f'NEW.{k} IS DISTINCT FROM OLD.{k}' for k in ('operation_id','timelapse_id','printer_id','owner_state','owner_org_id','filename','root_device','root_inode','created_at'))
 sealed_new=f"EXISTS(SELECT 1 FROM {B} WHERE operation_id=NEW.operation_id AND phase='sealed')"
 sealed_old=f"EXISTS(SELECT 1 FROM {B} WHERE operation_id=OLD.operation_id AND phase='sealed')"
 return {
 B:{'INSERT':f"EXISTS(SELECT 1 FROM {B} WHERE operation_id=NEW.operation_id) OR NEW.phase<>'building' OR NOT ({binding_proof()})",'UPDATE':f"OLD.phase<>'building' OR NEW.phase<>'sealed' OR {immutable} OR NOT ({binding_proof()}) OR NOT ({seal_proof(sqlite=sqlite)})",'DELETE':'1=1'},
 M:{'INSERT':f"EXISTS(SELECT 1 FROM {M} WHERE operation_id=NEW.operation_id AND object_index=NEW.object_index) OR NOT EXISTS(SELECT 1 FROM {B} b JOIN timelapse_retirements i ON i.operation_id=b.operation_id WHERE b.operation_id=NEW.operation_id AND b.phase='building' AND i.state='pending') OR NOT ({member_proof()})",'UPDATE':'1=1','DELETE':'1=1'},
 R:{'INSERT':f"EXISTS(SELECT 1 FROM {R} WHERE operation_id=NEW.operation_id AND object_index=NEW.object_index) OR NOT ({receipt_proof()})",'UPDATE':'1=1','DELETE':'1=1'},
 # Frozen015 declarations are unchanged. These additive guards affect only sealed023 plans.
 'timelapse_retirement_objects':{'INSERT':sealed_new,'UPDATE':f"{sealed_old} AND ({object_identity} OR (OLD.state='originally_absent' AND (NEW.state IS DISTINCT FROM OLD.state OR NEW.reason_code IS DISTINCT FROM OLD.reason_code)))",'DELETE':sealed_old},
 'timelapse_retirements':{'UPDATE':f"({sealed_old} AND ({intent_identity})) OR (NEW.state='complete' AND EXISTS(SELECT 1 FROM {B} WHERE operation_id=NEW.operation_id) AND NOT ({completion_proof()}))"}}

def sqlite_triggers():
 return {table+'_membership_'+event.lower():f"CREATE TRIGGER {table}_membership_{event.lower()} BEFORE {event} ON {table} BEGIN SELECT CASE WHEN {condition.replace('IS DISTINCT FROM','IS NOT').replace('IS NOT DISTINCT FROM','IS')} THEN RAISE(ABORT,'retirement membership differs') END; END" for table,events in conditions(sqlite=True).items() for event,condition in events.items()}
def pg_functions():
 return {table+'_membership_guard':"BEGIN\n"+'\n'.join(f" IF TG_OP='{event}' THEN IF {condition} THEN RAISE EXCEPTION 'retirement membership differs' USING ERRCODE='23514'; END IF; END IF;" for event,condition in events.items())+"\n IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;\nEND;" for table,events in conditions().items()}
def pg_triggers():
 return {table+'_membership_guard':f"CREATE TRIGGER {table}_membership_guard BEFORE "+' OR '.join(events)+f" ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.{table}_membership_guard()" for table,events in conditions().items()}
def validate_retained(c):
 for b in c.execute(bindings.select()).mappings():
  if not c.execute(text(f'SELECT 1 FROM {B} n WHERE operation_id=:op AND '+static_binding_proof('n')),{'op':b['operation_id']}).first():raise MigrationError('Retirement binding authority differs')
  if b['phase']!='sealed':raise MigrationError('Retirement membership incomplete')
  count=c.execute(text(f'SELECT count(*) FROM {M} WHERE operation_id=:op'),{'op':b['operation_id']}).scalar_one()
  if count!=b['object_count']:raise MigrationError('Retirement membership count differs')
  i=c.execute(text('SELECT state FROM timelapse_retirements WHERE operation_id=:op'),{'op':b['operation_id']}).scalar_one()
  if i=='complete':
   if not c.execute(text('SELECT 1 FROM timelapse_retirements n WHERE operation_id=:op AND '+completion_proof('n')),{'op':b['operation_id']}).first():raise MigrationError('Retirement completion differs')
  elif i=='pending':
   if not c.execute(text(f'SELECT 1 FROM {B} n WHERE operation_id=:op AND '+seal_proof('n',c.dialect.name=='sqlite',admission=False)),{'op':b['operation_id']}).first():raise MigrationError('Retirement membership authority differs')
 for r in c.execute(receipts.select()).mappings():
  if not c.execute(text(f'SELECT 1 FROM {R} n WHERE operation_id=:op AND object_index=:idx AND '+receipt_proof('n')),{'op':r['operation_id'],'idx':r['object_index']}).first():raise MigrationError('Retirement container evidence differs')
