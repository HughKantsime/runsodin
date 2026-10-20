"""Prepare-only exact lifetime membership. Never stage, unlink or activate API."""
from dataclasses import asdict
from datetime import datetime,timezone
import json,os
from sqlalchemy import select,text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root,_parent,_parts,_facts,_owned as owned_object,_names,MediaManifest,ObjectIdentity,MAX_OBJECTS,MAX_DEPTH
from core.timelapse_publication_schema import lifetimes,versions,namespaces,bindings as frame_bindings,publications,facts as publication_facts,retained
from core.timelapse_retirement_membership_schema import bindings,members
from core.timelapse_retirement_membership_validation import validate_retained
from core.timelapse_retirement_schema import intent,objects,facts as object_facts
from core.timelapse_recovery_schema import parents,moves
from modules.archives.timelapse_retirement import _floor,_actor,_owned,_snapshot,_write_boundary,_quarantine,_refuse,PreparedRetirement,RetirementUnavailable
from modules.archives.timelapse_recovery_evidence import begin_capture_deadline,check_deadline,capture_evidence,validate_evidence
from modules.archives.timelapse_action_authority import fence_authorization,fresh_action_principal,lock_media_sources
STABLE=('device','inode','uid','gid','mode')
FILE=STABLE+('link_count','byte_count','mtime_ns')

def _bounded(db,table,condition=None):
 q=select(table)
 if condition is not None:q=q.where(condition)
 rows=[dict(r) for r in db.execute(q.limit(MAX_OBJECTS+1)).mappings()]
 if len(rows)>MAX_OBJECTS:_refuse('manifest_limit')
 return sorted(rows,key=lambda r:tuple(str(r[k]) for k in table.primary_key.columns.keys()))
def _inventory(db,media,owner,*,graph_scope=None):
 nonce=media.ownership_operation_id
 version=db.execute(select(versions).where(versions.c.version_nonce==nonce)).mappings().first()
 life=dict(db.execute(select(lifetimes).where(lifetimes.c.lifetime_nonce==version['lifetime_nonce'])).mappings().one()) if version else None
 lifetime=life['lifetime_nonce'] if life else nonce
 ops=_bounded(db,publications,(publications.c.source_lifetime==lifetime)|(publications.c.result_lifetime==lifetime))
 opids=[r['operation_id'] for r in ops]
 selected_facts=_bounded(db,publication_facts,publication_facts.c.operation_id.in_(opids))
 from modules.archives.timelapse_speed_dependencies import physical_partition
 owned_ops,outgoing_speed=physical_partition(db,lifetime,ops,scope=graph_scope)
 other_live=[dict(r) for r in db.execute(text('SELECT id,filename,ownership_operation_id FROM timelapses WHERE id<>:id LIMIT :limit'),{'id':media.id,'limit':MAX_OBJECTS+1}).mappings()]
 if len(other_live)>MAX_OBJECTS:_refuse('manifest_limit')
 zero=media.status=='failed' and type(media.frame_count) is int and media.frame_count==0 and not ops
 if life is None and not zero:_refuse('phase_uncertain')
 if life and (life['original_timelapse_id'],life['original_printer_id'])!=(owner['timelapse_id'],owner['printer_id']):_refuse('ownership_changed')
 # A failed zero result must have the encoder's exact original audited terminal authority.
 if zero:
  if media.completed_at is None or media.duration_seconds is not None or media.file_size_mb is not None:_refuse('phase_uncertain')
  expected={'reason':'insufficient_frames','ownership_nonce':nonce,'frame_count':0}
  audited=db.execute(text("SELECT details FROM audit_logs WHERE action='timelapse_encode_failed' AND entity_type='timelapse' AND entity_id=:id LIMIT :limit"),{'id':media.id,'limit':MAX_OBJECTS+1}).scalars().all()
  if len(audited)>MAX_OBJECTS:_refuse('manifest_limit')
  if sum((json.loads(x) if type(x) is str else x)==expected for x in audited)!=1:_refuse('phase_uncertain')
 return dict(lifetime_versions=_bounded(db,versions,versions.c.lifetime_nonce==lifetime),snapshot=_snapshot(media),owner=owner,version=dict(version) if version else None,life=life,lifetime=lifetime,zero=zero,ops=ops,owned_ops=owned_ops,outgoing_speed=outgoing_speed,facts=selected_facts,
  spaces=_bounded(db,namespaces,namespaces.c.lifetime_nonce==lifetime),binding=_bounded(db,frame_bindings,frame_bindings.c.lifetime_nonce==lifetime),retained=_bounded(db,retained,retained.c.lifetime_nonce==lifetime),other_live=other_live,
  all_spaces=_bounded(db,namespaces) if zero else [],all_lifetimes=_bounded(db,lifetimes) if zero else [],all_bindings=_bounded(db,frame_bindings) if zero else [],other_retained=_bounded(db,retained,retained.c.lifetime_nonce!=lifetime))

def _links(inv):
 links={};expected={}
 grouped={}
 for f in inv['facts']:grouped.setdefault(f['operation_id'],{}).setdefault(f['role'],[]).append(f)
 def add(path,role,*,version=None,old=None,space=None,op=None,fact=None,absent=False,kind=None):
  _parts(path)
  if path in links:_refuse('object_changed')
  links[path]=dict(role=role,version_nonce=version,retained_version_nonce=old,namespace_id=space,publication_id=op['operation_id'] if op else None,fact_role=fact['role'] if fact else None,fact_path=fact['relative_path'] if fact else None)
  expected[path]=dict(fact=fact,op=op,absent=absent,kind=kind)
 def one(op,role):
  values=grouped.get(op['operation_id'],{}).get(role,[])
  if len(values)!=1:_refuse('phase_uncertain')
  return values[0]
 current=[p for p in inv['ops'] if p['result_nonce']==inv['snapshot'][6] and p['kind']!='frame']
 if inv['snapshot'][4]=='ready':
  if len(current)!=1 or current[0]['phase']!='complete' or current[0]['state']!='complete':_refuse('writer_active')
  p=current[0]
  if p['destination_path']!=inv['snapshot'][3]:_refuse('ownership_changed')
  add(p['destination_path'],'current_video',version=p['result_nonce'],op=p,fact=one(p,'output'),kind='file')
 elif current:_refuse('phase_uncertain')
 for r in inv['retained']:
  if r['state']!='retained':_refuse('phase_uncertain')
  p=next((p for p in inv['ops'] if p['operation_id']==r['retained_by_operation']),None)
  if p is None or p['phase']!='complete' or p['kind']!='trim':_refuse('phase_uncertain')
  add(r['filename'],'retained_video',version=r['version_nonce'],old=r['version_nonce'],op=p,fact=one(p,'source'),kind='file')
 for s in inv['spaces']:
  if s['relative_path']!='frames_'+s['namespace_id']:_refuse('phase_uncertain')
  if s['phase'] not in ('bound','planned','blocked'):_refuse('phase_uncertain')
  if s['inode'] is not None and s['phase']!='bound':_refuse('phase_uncertain')
  add(s['relative_path'],'frame_namespace',space=s['namespace_id'],fact=None,absent=s['inode'] is None,kind='directory')
  expected[s['relative_path']]['space']=s
 for p in inv.get('owned_ops',inv['ops']):
  if p['private_directory']!='publish_'+p['operation_id'] or p['private_name']!=('output.jpg' if p['kind']=='frame' else 'output.mp4'):_refuse('phase_uncertain')
  if p['phase']!='complete' or p['state']!='complete':
   # Never-created blocked attempts may leave a counter gap, but no private evidence may be adopted.
   if p['phase']=='admitted' and p['state']=='blocked' and not grouped.get(p['operation_id'],{}).get('private_root'):
    expected[p['private_directory']]=dict(check_only_absent=True)
    continue
   _refuse('writer_active')
  if p['kind']=='frame':
   space=next((v for v in inv['spaces'] if v['namespace_id']==p['frame_namespace_id']),None)
   binding=next((v for v in inv['binding'] if v['namespace_id']==p['frame_namespace_id']),None)
   number=p['frame_number']
   if space is None or binding is None or type(number) is not int or not 1<=number<binding['next_number'] or p['destination_path']!=space['relative_path']+'/frame_'+str(number).zfill(6)+'.jpg':_refuse('phase_uncertain')
   output=one(p,'output')
   if p['payload_byte_count']!=output['byte_count'] or p['payload_digest_sha256']!=output['digest_sha256']:_refuse('phase_uncertain')
   add(p['destination_path'],'completed_frame',space=p['frame_namespace_id'],op=p,fact=output,kind='file')
  add(p['private_directory'],'private_directory',op=p,fact=one(p,'private_root'),kind='directory')
  add(p['private_directory']+'/'+p['private_name'],'private_output',op=p,fact=one(p,'output'),absent=True,kind='file')
 return links,expected

def _capture(root,identity,inv,links,expected,deadline):
 results={};parent_facts={}
 with _root(root) as (fd,again):
  if tuple(getattr(again,k) for k in STABLE)!=tuple(getattr(identity,k) for k in STABLE):_refuse('object_changed')
  if inv['life'] and any(inv['life']['root_'+k]!=getattr(identity,k) for k in STABLE):_refuse('object_changed')
  if inv['zero']:
   recorded={s['relative_path']:s for s in inv['all_spaces']}
   for name in _names(fd,MAX_OBJECTS):
    if name.startswith('frames_'):
     s=recorded.get(name)
     if s is None or s['inode'] is None or s['phase']!='bound':_refuse('phase_uncertain')
     life=next((l for l in inv['all_lifetimes'] if l['lifetime_nonce']==s['lifetime_nonce']),None)
     binding=next((b for b in inv['all_bindings'] if b['namespace_id']==s['namespace_id']),None)
     if life is None or binding is None or binding['lifetime_nonce']!=s['lifetime_nonce'] or any(life['root_'+k]!=getattr(identity,k) for k in STABLE):_refuse('object_changed')
     st=_facts(name,os.stat(name,dir_fd=fd,follow_symlinks=False))
     if st.kind!='directory' or any(getattr(st,k)!=s[k] for k in STABLE):_refuse('object_changed')
  try:os.stat(str(inv['snapshot'][0]),dir_fd=fd,follow_symlinks=False)
  except FileNotFoundError:pass
  else:_refuse('phase_uncertain')
  if inv['snapshot'][4]=='failed' and inv['snapshot'][3] not in links:
   opened=[];current=fd
   try:
    parts=_parts(inv['snapshot'][3])
    for index,part in enumerate(parts):
     try:st=os.stat(part,dir_fd=current,follow_symlinks=False)
     except FileNotFoundError:break
     if index==len(parts)-1:_refuse('phase_uncertain')
     saved=_facts('/'.join(parts[:index+1]),st);owned_object(saved,identity)
     child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current);opened.append(child)
     if _facts(saved.relative_path,os.fstat(child))!=saved:_refuse('object_changed')
     current=child
   finally:
    for child in reversed(opened):os.close(child)
  def visit(path):
   check_deadline(deadline)
   if path in results:return
   e=expected.get(path)
   if e is None:_refuse('phase_uncertain')
   with _parent(fd,_parts(path),identity,parent_facts) as (parent,name):
    try:before=_facts(path,os.stat(name,dir_fd=parent,follow_symlinks=False))
    except FileNotFoundError:
     if not e.get('absent') and not e.get('check_only_absent'):_refuse('object_changed')
     if not e.get('check_only_absent'):results[path]=ObjectIdentity(path,'absent')
     return
    if e.get('absent') or e.get('check_only_absent'):_refuse('phase_uncertain')
    owned_object(before,identity)
    if before.kind!=e['kind']:_refuse('object_changed')
    saved=e.get('space') or e.get('fact')
    fields=STABLE if before.kind=='directory' else FILE
    if saved is None or any(getattr(before,k)!=saved[k] for k in fields):_refuse('object_changed')
    if before.kind=='file':
     stamp=e['fact']['ctime_ns'] if links[path]['role']=='retained_video' else e['op']['published_ctime_ns']
     if before.ctime_ns!=stamp:_refuse('object_changed')
    child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|(os.O_DIRECTORY if before.kind=='directory' else 0),dir_fd=parent)
    try:
     if _facts(path,os.fstat(child))!=before:_refuse('object_changed')
     results[path]=before
     if len(results)>MAX_OBJECTS:_refuse('manifest_limit')
     if before.kind=='directory':
      names=_names(child,MAX_OBJECTS-len(results))
      for n in names:visit(path+'/'+n)
      if _names(child,MAX_OBJECTS)!=names:_refuse('object_changed')
     if _facts(path,os.fstat(child))!=before or _facts(path,os.stat(name,dir_fd=parent,follow_symlinks=False))!=before:_refuse('object_changed')
    finally:os.close(child)
  for path in sorted(expected):visit(path)
  for path,v in results.items():
   with _parent(fd,_parts(path),identity,parent_facts) as (parent,name):
    try:now=_facts(path,os.stat(name,dir_fd=parent,follow_symlinks=False))
    except FileNotFoundError:
     if v.kind!='absent':_refuse('object_changed')
    else:
     if now!=v:_refuse('object_changed')
  if set(results)!=set(links):_refuse('phase_uncertain')
  if any(r['filename'] in links for r in inv['other_live']+inv['other_retained']):_refuse('object_changed')
  manifest=MediaManifest(identity,tuple(parent_facts[k] for k in sorted(parent_facts)),tuple(results[k] for k in sorted(results)))
 return manifest

def prepare_lifetime_retirement(engine,root,tid,principal,*,expected_nonce,_evidence=None,_budget=None):
 """Own one preparation transaction/guard; no filesystem mutation beyond existing guard."""
 if not isinstance(engine,Engine) or type(tid) is not int or tid<=0 or type(expected_nonce) is not str:_refuse('unknown_authority')
 _floor(principal)
 try:
  with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity):
   with Session(engine) as db:
    from modules.archives.timelapse_snapshot_graph import fence_graph_scope
    graph_scope=fence_graph_scope(db,guard,identity,preparation=True,retirement_proofs=True)
    if _evidence is not None:
     principal=fresh_action_principal(db,_evidence)
    _actor(db,principal);media,owner=_owned(db,tid,principal)
    if media.ownership_operation_id!=expected_nonce:_refuse('ownership_changed')
    if media.retirement_operation_id is not None:
     b=db.execute(select(bindings).where(bindings.c.operation_id==expected_nonce)).mappings().first()
     if b is None:_refuse('phase_uncertain') # explicit legacy dispatch; never reinterpret old plans
     validate_retained(db.connection())
     rows=_bounded(db,objects,objects.c.operation_id==expected_nonce)
     validate_evidence(db,expected_nonce,rows,identity)
     return PreparedRetirement(expected_nonce,'pending',False)
    inv=_inventory(db,media,owner,graph_scope=graph_scope)
    if _evidence is not None:_preparation_sharing(db,inv,tid,graph_scope=graph_scope,budget=_budget)
   deadline=_budget.deadline if _budget else begin_capture_deadline();links,expected=_links(inv)
   manifest=_capture(root,identity,inv,links,expected,deadline)
   if _budget:_budget.consume(sum(v.byte_count for v in manifest.objects if v.kind=='file'))
   directory_rows,movement=capture_evidence(root,manifest,expected_nonce,deadline)
   for row in movement:
    path=manifest.objects[row['object_index']].relative_path
    f=expected[path].get('fact')
    if f and row['kind']=='file' and row['digest_sha256']!=f['digest_sha256']:_refuse('object_changed')
   with Session(engine) as db:
    from modules.archives.timelapse_snapshot_graph import fence_graph_scope
    graph_scope=fence_graph_scope(db,guard,identity,preparation=True,retirement_proofs=True)
    if _evidence is not None:
     principal=fresh_action_principal(db,_evidence)
    _actor(db,principal,lock=True);media,owner=_owned(db,tid,principal,lock=True)
    if media.ownership_operation_id!=expected_nonce or _inventory(db,media,owner,graph_scope=graph_scope)!=inv:_refuse('ownership_changed')
    if _evidence is not None:
     lock_media_sources(db,((lifetimes,[inv['life']] if inv['life'] else []),(versions,inv['lifetime_versions']),(publications,inv['ops']),(publication_facts,inv['facts'])))
     _preparation_sharing(db,inv,tid,graph_scope=graph_scope,budget=_budget)
    guard.assert_root_identity(manifest.root)
    if inv['life'] is None:
     db.execute(lifetimes.insert(),dict(lifetime_nonce=expected_nonce,original_timelapse_id=tid,original_printer_id=media.printer_id,**{'root_'+k:getattr(identity,k) for k in STABLE}))
     db.execute(versions.insert(),dict(version_nonce=expected_nonce,lifetime_nonce=expected_nonce))
    media.status='deleting';media.retirement_operation_id=expected_nonce;db.flush()
    db.execute(intent.insert(),dict(operation_id=expected_nonce,timelapse_id=tid,printer_id=media.printer_id,owner_state=owner['owner_state'],owner_org_id=owner['owner_org_id'],filename=media.filename,root_device=identity.device,root_inode=identity.inode,state='pending',revision=1))
    db.execute(bindings.insert(),dict(operation_id=expected_nonce,version_nonce=expected_nonce,lifetime_nonce=inv['lifetime'],phase='building',object_count=len(manifest.objects),created_at=datetime.now(timezone.utc)))
    payload=[];membership=[]
    for index,value in enumerate(manifest.objects):
     absent=value.kind=='absent';path=value.relative_path
     payload.append(dict(operation_id=expected_nonce,object_index=index,relative_path=path,kind=expected[path]['kind'] if absent else value.kind,**{k:getattr(value,k) for k in object_facts},state='originally_absent' if absent else 'planned',quarantine_name=None if absent else _quarantine(expected_nonce,index)))
     membership.append(dict(operation_id=expected_nonce,object_index=index,**links[path]))
    if payload:db.execute(objects.insert(),payload)
    db.execute(parents.insert(),directory_rows)
    if movement:db.execute(moves.insert(),movement)
    if membership:db.execute(members.insert(),membership)
    db.execute(bindings.update().where(bindings.c.operation_id==expected_nonce,bindings.c.phase=='building').values(phase='sealed',sealed_at=datetime.now(timezone.utc)))
    validate_retained(db.connection())
    from core.audit import log_audit
    log_audit(db,'retirement_prepare','timelapse',tid,{'operation_id':expected_nonce,'actor_id':principal['id'],'lifetime_nonce':inv['lifetime']})
    check_deadline(deadline);db.commit()
    return PreparedRetirement(expected_nonce,'pending',True)
 except RetirementUnavailable:raise
 except Exception as error:_refuse(getattr(error,'reason','phase_uncertain'))


def prepare_request_lifetime_retirement(engine,root,tid,evidence,*,expected_nonce,budget):
 from modules.archives.timelapse_action_authority import ActionCredential
 if type(evidence) is not ActionCredential or evidence.purpose!='retire':_refuse('unknown_authority')
 with Session(engine) as db:
  fence_authorization(db,preparation=True);principal=fresh_action_principal(db,evidence)
 return prepare_lifetime_retirement(engine,root,tid,principal,expected_nonce=expected_nonce,_evidence=evidence,_budget=budget)


def _preparation_sharing(db,inv,tid,*,graph_scope=None,budget=None):
 from modules.archives.timelapse_lifetime_action import _sharing
 links,expected=_links(inv)
 selected=[]
 for path in links:
  value=expected[path]
  physical=value.get('fact') or value.get('space') or {}
  selected.append(dict(physical,relative_path=path,state='originally_absent' if value.get('absent') else 'planned'))
 _sharing(db,{'binding':{'lifetime_nonce':inv['lifetime']},'objects':selected,
 'publications':inv['ops'],'namespaces':inv['spaces'],'intent':{'timelapse_id':tid}},partition=(inv['owned_ops'],inv['outgoing_speed']),graph_scope=graph_scope,budget=budget)
