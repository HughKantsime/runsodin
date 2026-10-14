"""Internal sealed023 physical retirement. Routes remain separately contained."""
from contextlib import contextmanager
from datetime import datetime,timezone
import time,os,copy
from sqlalchemy import text,select
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine
from core.timelapse_manifest import _root,_parts,_facts,_stable,MAX_OBJECTS
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_quarantine import quarantine_directory
from core.timelapse_staging_fs import StageFilesystem
from core.timelapse_drain_fs import DrainFilesystem
from core.timelapse_digest import MAX_BYTES
from core.timelapse_action_fs import verify_action,apply_verified_action
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_recovery_schema import parents,moves,quarantine
from core.timelapse_unlink_schema import unlinks
from core.timelapse_retirement_membership_schema import bindings,members,receipts as containers
from core.timelapse_retirement_membership_validation import seal_proof,static_binding_proof
from core.timelapse_publication_schema import lifetimes,versions,namespaces,publications,facts,retained,bindings as frame_bindings
from modules.archives.timelapse_retirement import _owned,_snapshot,_refuse,RetirementUnavailable
from modules.archives.timelapse_stage_transactions import directory_identity
from modules.archives.timelapse_action_authority import fence_authorization,fresh_action_principal
from modules.archives.timelapse_drain import RetiredMedia


def _rows(db,table,condition,*,lock=False):
    query=select(table).where(condition).order_by(*table.primary_key.columns).limit(MAX_OBJECTS+1)
    if lock:query=query.with_for_update(nowait=True)
    result=[dict(row) for row in db.execute(query).mappings()]
    if len(result)>MAX_OBJECTS:_refuse('manifest_limit')
    return result


def _load(db,tid,principal,nonce,root,*,lock=False,graph_scope=None,budget=None):
    media,owner=_owned(db,tid,principal)
    if media.status!='deleting' or media.retirement_operation_id!=nonce or media.ownership_operation_id!=nonce:_refuse('ownership_changed')
    b=_rows(db,bindings,bindings.c.operation_id==nonce,lock=lock)
    i=_rows(db,intent,intent.c.operation_id==nonce,lock=lock)
    if len(b)!=1 or len(i)!=1 or b[0]['phase']!='sealed' or i[0]['state'] not in ('pending','in_flight'):_refuse('phase_uncertain')
    if (i[0]['root_device'],i[0]['root_inode'])!=(root.device,root.inode):_refuse('ownership_changed')
    if not db.execute(text('SELECT 1 FROM '+bindings.name+' n WHERE operation_id=:op AND '+static_binding_proof('n')+' AND '+seal_proof('n',db.get_bind().dialect.name=='sqlite',admission=False)),{'op':nonce}).first():_refuse('phase_uncertain')
    lifetime=b[0]['lifetime_nonce']
    tables={
      'objects':(objects,objects.c.operation_id==nonce),'members':(members,members.c.operation_id==nonce),
      'parents':(parents,parents.c.operation_id==nonce),'moves':(moves,moves.c.operation_id==nonce),
      'quarantine':(quarantine,quarantine.c.operation_id==nonce),'unlinks':(unlinks,unlinks.c.operation_id==nonce),
      'containers':(containers,containers.c.operation_id==nonce),
      'lifetimes':(lifetimes,lifetimes.c.lifetime_nonce==lifetime),
      'versions':(versions,versions.c.lifetime_nonce==lifetime),
      'frame_bindings':(frame_bindings,frame_bindings.c.lifetime_nonce==lifetime),
      'namespaces':(namespaces,namespaces.c.lifetime_nonce==lifetime),
      'retained':(retained,retained.c.lifetime_nonce==lifetime),
      'publications':(publications,(publications.c.source_lifetime==lifetime)|(publications.c.result_lifetime==lifetime))}
    result={'snapshot':_snapshot(media),'owner':owner,'binding':b[0],'intent':i[0]}
    for key,(table,condition) in sorted(tables.items(),key=lambda pair:pair[1][0].name):result[key]=_rows(db,table,condition,lock=lock)
    opids=[r['operation_id'] for r in result['publications']]
    result['facts']=_rows(db,facts,facts.c.operation_id.in_(opids),lock=lock)
    if len(result['objects'])!=b[0]['object_count']:_refuse('phase_uncertain')
    for life in result['lifetimes']:
        if any(life['root_'+k]!=getattr(root,k) for k in ('device','inode','uid','gid','mode')):_refuse('ownership_changed')
    from modules.archives.timelapse_speed_dependencies import physical_partition
    owned,outgoing=physical_partition(db,lifetime,result['publications'],scope=graph_scope)
    result['owned_publications']=owned;result['outgoing_speed']=outgoing
    referenced={m['publication_id'] for m in result['members'] if m['publication_id'] is not None}
    if any(m['publication_id'] is not None and m['publication_id'] not in {p['operation_id'] for p in owned} for m in result['members']):_refuse('shared_workspace')
    if any(p['operation_id'] not in referenced and not (p['phase']=='admitted' and p['state']=='blocked') for p in owned):_refuse('writer_active')
    _sharing(db,result,partition=(owned,outgoing),graph_scope=graph_scope,budget=budget)
    return result


def _sharing(db,plan,*,partition=None,graph_scope=None,budget=None):
    """Conservative global-history bound: each relation overMAX_OBJECTS refuses.

    This can refuse a small retirement in a large installation. No truncated
    scan grants safety; a relevant-overlap query is a later optimization.
    """
    lifetime=plan['binding']['lifetime_nonce'];paths={r['relative_path'] for r in plan['objects']}
    opids={r['operation_id'] for r in plan['publications']}
    from modules.archives.timelapse_speed_dependencies import physical_partition
    owned,outgoing=partition if partition is not None else physical_partition(db,lifetime,plan['publications'])
    def overlaps(path):
        return path is not None and any(path==value or path.startswith(value+'/') or value.startswith(path+'/') for value in paths)
    # Related rows are not omitted from global sharing checks. Outgoing speed
    # has exactly one proven readonly source edge; all physical paths still
    # fence both lifetimes. Incoming source must be outside owned membership.
    for row in outgoing:
        if any(overlaps(row[key]) for key in ('private_directory','destination_path')):_refuse('shared_workspace')
    for row in owned:
        if row['kind']=='speed' and any(overlaps(row[key]) for key in ('source_filename',)):_refuse('shared_workspace')
    for table,condition in (
      (publications,~publications.c.operation_id.in_(opids)),
      (retained,retained.c.lifetime_nonce!=lifetime),
      (namespaces,namespaces.c.lifetime_nonce!=lifetime)):
        for row in _rows(db,table,condition):
            candidates=[row.get(k) for k in ('private_directory','destination_path','source_filename','filename','relative_path')]
            if any(path is not None and any(path==owned or path.startswith(owned+'/') or owned.startswith(path+'/') for owned in paths) for path in candidates):_refuse('shared_workspace')
    # Path checks cannot bless physical aliases under different names. Exclude
    # already retired/absent members so legitimate inode reuse after deletion
    # is not reinterpreted as ownership of a new lifetime's bytes.
    identities={(row.get('device'),row.get('inode')) for row in plan['objects']
                if row.get('state') not in ('retired','originally_absent') and row.get('inode') is not None}
    foreign=_rows(db,publications,publications.c.result_lifetime!=lifetime)
    foreign_ids=[row['operation_id'] for row in foreign]
    if foreign_ids:
        collisions=[row for row in _rows(db,facts,facts.c.operation_id.in_(foreign_ids)&facts.c.role.in_(('output','private_creation','private_root'))) if (row['device'],row['inode']) in identities]
        if collisions:
            from modules.archives.timelapse_retired_fact_proof import retired_fact_exemptions
            proved=retired_fact_exemptions(db,collisions,foreign,scope=graph_scope,budget=budget)
            if any((row['operation_id'],row['role'],row['relative_path']) not in proved for row in collisions):_refuse('shared_workspace')
    selected_spaces={r['namespace_id'] for r in plan['namespaces']}
    if _rows(db,frame_bindings,(frame_bindings.c.lifetime_nonce!=lifetime)&frame_bindings.c.namespace_id.in_(selected_spaces)):_refuse('shared_workspace')
    duplicates=db.execute(text('SELECT filename FROM timelapses WHERE id<>:id LIMIT :limit'),{'id':plan['intent']['timelapse_id'],'limit':MAX_OBJECTS+1}).scalars().all()
    if len(duplicates)>MAX_OBJECTS:_refuse('manifest_limit')
    if any(path in paths for path in duplicates):_refuse('shared_workspace')


@contextmanager
def _transaction(engine,evidence,tid,nonce,identity,expected=None,*,guard,handoff=None,budget=None):
    if getattr(engine,'_odin_timelapse_transaction_cleanup_blocked',False):_refuse('phase_uncertain')
    if handoff is not None and (type(handoff) is not list or handoff):_refuse('phase_uncertain')
    db=None;owned_connection=None;lease=None;original_connection=None;original_proxy=None;original_driver=None
    committing=False;failure=None;uncertain=False
    try:
        # Retain this exact checkout before authority/graph work or yielding.
        from modules.archives.timelapse_action_authority import _admit_action_lease,_mint_action_lease,_expire_action_lease
        owned_connection=engine.connect();db=Session(bind=owned_connection)
        lease=_admit_action_lease(db,evidence,owned_connection)
        original_connection=db.connection()
        original_proxy=original_connection.connection
        original_driver=original_proxy.driver_connection
        from modules.archives.timelapse_snapshot_graph import fence_graph_scope
        scope=fence_graph_scope(db,guard,identity,retirement_proofs=True);_mint_action_lease(lease);principal=fresh_action_principal(db,evidence)
        actual=_load(db,tid,principal,nonce,identity,lock=True,graph_scope=scope,budget=budget)
        if expected is not None and actual!=expected:_refuse('phase_uncertain')
        yield db,principal,actual
        sealed=copy.deepcopy(actual) if handoff is not None else None
        _expire_action_lease(db,lease);committing=True
        db.commit()
        if handoff is not None:
            if handoff:_refuse('phase_uncertain')
            handoff.append(_CommittedPlan(sealed,guard,tid,nonce,identity,_HANDOFF))
    except BaseException as error:
        failure=error;rollback_failed=False
        if db is not None and lease is not None:_expire_action_lease(db,lease)
        if original_driver is not None:
            try:original_driver.rollback()
            except BaseException:rollback_failed=True;uncertain=True
        # A failed COMMIT can deactivate SQLAlchemy's transaction while leaving
        # the physical driver in a transaction. Never return that checkout.
        if original_connection is not None and (committing or rollback_failed):
            discarded=False
            try:original_connection.invalidate();discarded=True
            except BaseException:uncertain=True
            if not discarded and original_proxy is not None:
                try:original_proxy.invalidate();discarded=True
                except BaseException:uncertain=True
            if not discarded and original_proxy is not None:
                try:
                    original_proxy.detach()
                    discarded=original_proxy.is_detached
                except BaseException:
                    uncertain=True
                    try:discarded=original_proxy.is_detached
                    except BaseException:pass
            if not discarded:
                # Public API replaces this engine's pool without reconnecting.
                # No claim about independently retained aliases to the old pool.
                old_pool=engine.pool
                try:engine.dispose(close=False)
                except BaseException:uncertain=True
                if engine.pool is old_pool:uncertain=True
                # Original checkout isolation was not confirmed. This engine
                # cannot admit another media mutation until independently replaced.
                engine._odin_timelapse_transaction_cleanup_blocked=True
                uncertain=True
        if uncertain:error.add_note('Timelapse transaction cleanup could not be confirmed')
        raise
    finally:
        if db is not None and lease is not None:_expire_action_lease(db,lease)
        try:
            if db is not None:db.close()
        except BaseException as close_error:
            if failure is None:
                failure=close_error
                raise
            failure.add_note('Timelapse Session cleanup could not be confirmed')
        finally:
            try:
                if owned_connection is not None:owned_connection.close()
            except BaseException:
                if failure is None:raise
                failure.add_note('Timelapse connection lease cleanup could not be confirmed')


_HANDOFF=object()

class _CommittedPlan:
    def __init__(self,plan,guard,tid,nonce,identity,proof):
        if proof is not _HANDOFF:_refuse('phase_uncertain')
        self.plan=plan;self.guard=guard;self.tid=tid;self.nonce=nonce;self.identity=identity

def _take_handoff(handoff,guard,tid,nonce,identity):
    if len(handoff)!=1:_refuse('phase_uncertain')
    item=handoff.pop()
    if type(item) is not _CommittedPlan or item.guard is not guard or item.tid!=tid or item.nonce!=nonce or item.identity!=identity:_refuse('phase_uncertain')
    plan=item.plan;item.plan=None
    if plan is None:_refuse('phase_uncertain')
    guard.assert_root_identity(identity)
    return plan

def _journal_delta(db,plan,key,values,*,index=None,insert=False):
    # Closed raw schemas have no server-generated columns or mutating triggers.
    tables={'moves':moves,'objects':objects,'unlinks':unlinks,'quarantine':quarantine}
    allowed={'moves':{'phase','verified_ctime_ns'},'objects':{'state'},'unlinks':{'phase'},'quarantine':{'device','inode','uid','gid','mode'}}
    if key not in tables or not values or not set(values)<=allowed[key]:_refuse('phase_uncertain')
    if key!='quarantine' and (type(index) is not int or index<0):_refuse('phase_uncertain')
    if key=='quarantine' and (index is not None or not insert or set(values)!=allowed[key] or any(type(v) is not int or v<0 for v in values.values()) or values['inode']<=0 or values['mode']!=16832):_refuse('phase_uncertain')
    op=plan['intent']['operation_id'];table=tables[key]
    rows=[r for r in plan[key] if index is None or r['object_index']==index]
    if insert:
        if rows or key not in ('unlinks','quarantine'):_refuse('phase_uncertain')
        if key=='unlinks' and values!={'phase':'unlink_pending'}:_refuse('phase_uncertain')
        row={'operation_id':op,**values}
        if index is not None:row['object_index']=index
        if set(row)!=set(table.c.keys()):_refuse('phase_uncertain')
        result=db.execute(table.insert().returning(*table.c),row)
        returned=result.mappings().fetchmany(2)
        if len(returned)!=1:_refuse('phase_uncertain')
        confirmed=dict(returned[0])
        if set(confirmed)!=set(row) or any(type(confirmed[k]) is not type(v) or confirmed[k]!=v for k,v in row.items()):_refuse('phase_uncertain')
        plan[key].append(row)
        plan[key].sort(key=lambda value:tuple(value[c.name] for c in table.primary_key.columns))
    else:
        if len(rows)!=1 or key not in ('moves','objects','unlinks'):_refuse('phase_uncertain')
        row=rows[0]
        transitions={'moves':{'planned':'rename_pending','rename_pending':'staged'},'objects':{'planned':'staged','staged':'unlink_confirmed','unlink_confirmed':'fsync_confirmed','fsync_confirmed':'retired'},'unlinks':{'unlink_pending':'unlink_confirmed','unlink_confirmed':'fsync_confirmed','fsync_confirmed':'retired'}}
        field='state' if key=='objects' else 'phase'
        target=values.get(field)
        if transitions[key].get(row[field])!=target and not (key=='moves' and row[field]==target=='rename_pending'):_refuse('phase_uncertain')
        if key=='moves' and target=='staged':
            if set(values)!={'phase','verified_ctime_ns'} or type(values['verified_ctime_ns']) is not int or values['verified_ctime_ns']<0:_refuse('phase_uncertain')
        elif set(values)!={field}:_refuse('phase_uncertain')
        condition=table.c.operation_id==op
        for column in table.primary_key.columns:
            condition=condition & (column==row[column.name])
        # Compare the entire prior row, including nullable immutable facts.
        for column in table.c:
            condition=condition & (column.is_(None) if row[column.name] is None else column==row[column.name])
        result=db.execute(table.update().where(condition).values(**values))
        if result.rowcount!=1:_refuse('phase_uncertain')
        row.update(values)

def _admit_batch(db,plan,kind):
    if kind=='files':
        candidates=[r for r in plan['objects'] if r['kind']=='file' and r['state']=='planned' and next(m for m in plan['moves'] if m['object_index']==r['object_index'])['phase']=='planned']
    elif kind=='unlinks':
        existing={r['object_index'] for r in plan['unlinks']}
        candidates=[r for r in plan['objects'] if r['state']!='originally_absent' and r['object_index'] not in existing]
        if any(r['state']!='staged' or next(m for m in plan['moves'] if m['object_index']==r['object_index'])['phase']!='staged' for r in candidates):_refuse('phase_uncertain')
    else:_refuse('phase_uncertain')
    if not 1<=len(candidates)<=MAX_OBJECTS:_refuse('phase_uncertain')
    for row in sorted(candidates,key=lambda r:r['object_index']):
        if kind=='files':_journal_delta(db,plan,'moves',{'phase':'rename_pending'},index=row['object_index'])
        else:_journal_delta(db,plan,'unlinks',{'phase':'unlink_pending'},index=row['object_index'],insert=True)
        _progress(db,plan)


def _refresh(engine,evidence,tid,nonce,identity,*,guard,budget=None):
    with Session(engine) as db:
        from modules.archives.timelapse_snapshot_graph import fence_graph_scope
        scope=fence_graph_scope(db,guard,identity,retirement_proofs=True)
        return _load(db,tid,fresh_action_principal(db,evidence),nonce,identity,graph_scope=scope,budget=budget)


def _progress(db,plan,*,state=None):
    row=plan['intent']
    update=db.execute(intent.update().where(intent.c.operation_id==row['operation_id'],intent.c.revision==row['revision'],intent.c.state==row['state']).values(state=state or row['state'],revision=row['revision']+1,reason_code=None))
    if update.rowcount!=1:_refuse('phase_uncertain')
    row.update(state=state or row['state'],revision=row['revision']+1,reason_code=None)


def _absence(root_fd,identity,plan):
    """Root-local absent plans only; nested absence needs staged lineage below."""
    for row in plan['objects']:
        if row['state']!='originally_absent':continue
        if '/' in row['relative_path']:_refuse('phase_uncertain')
        try:os.stat(row['relative_path'],dir_fd=root_fd,follow_symlinks=False)
        except FileNotFoundError:pass
        else:_refuse('object_changed')
    if _stable(_facts('',os.fstat(root_fd)))!=_stable(identity):_refuse('ownership_changed')


class LifetimeDrainFilesystem(DrainFilesystem):
    def __init__(self,*args,membership,**kwargs):
        plan=args[4];by_index={m['object_index']:m for m in membership}
        absent=[r for r in plan if r['state']=='originally_absent' and '/' in r['relative_path']]
        physical=[r for r in plan if r not in absent]
        for row in absent:
            member=by_index.get(row['object_index']);parent_path=row['relative_path'].rpartition('/')[0]
            parent=next((r for r in physical if r['relative_path']==parent_path),None)
            pm=by_index.get(parent['object_index']) if parent else None
            if member is None or member['role']!='private_output' or pm is None or pm['role']!='private_directory' or member['publication_id']!=pm['publication_id']:_refuse('phase_uncertain')
        amended=list(args);amended[4]=physical
        super().__init__(*amended,**kwargs)
        self.nested_absent=absent;self.all_objects=plan
    def originals_absent(self):
        super().originals_absent()
        for row in self.nested_absent:
            parent=next(r for r in self.plan if r['relative_path']==row['relative_path'].rpartition('/')[0])
            value=self.entry(self.quarantine_fd,parent['quarantine_name'],parent['relative_path'])
            if value is None:
                if self.unlinks.get(parent['object_index'],{}).get('phase') not in ('unlink_pending','unlink_confirmed','fsync_confirmed','retired'):_refuse('phase_uncertain')
                # Final receipt requires fsync/retired; pending can only reconcile exact parent.
                continue
            self.check_directory(self.quarantine_fd,parent['quarantine_name'],parent,verified=self.moves[parent['object_index']]['verified_ctime_ns'])


def _complete(db,principal,plan):
    nonce=plan['binding']['operation_id'];stamp=datetime.now(timezone.utc)
    for row in plan['objects']:
        if row['state']=='originally_absent':continue
        if row['state']!='retired' or not any(u['object_index']==row['object_index'] and u['phase']=='retired' for u in plan['unlinks']):_refuse('phase_uncertain')
    for member in plan['members']:
        row=next(r for r in plan['objects'] if r['object_index']==member['object_index'])
        if member['role'] in ('frame_namespace','private_directory') and row['state']=='retired':
            if not any(r['object_index']==row['object_index'] for r in plan['containers']):
                db.execute(containers.insert(),{'operation_id':nonce,'object_index':row['object_index'],'role':member['role'],'phase':'complete','completed_at':stamp})
        if member['role']=='frame_namespace' and row['state']=='retired':
            db.execute(namespaces.update().where(namespaces.c.namespace_id==member['namespace_id'],namespaces.c.phase=='bound').values(phase='retired'))
        if member['role']=='retained_video':
            db.execute(retained.update().where(retained.c.version_nonce==member['retained_version_nonce'],retained.c.state=='retained').values(state='retired',retire_operation_id=nonce))
    changed=db.execute(text("DELETE FROM timelapses WHERE id=:id AND status='deleting' AND ownership_operation_id=:op AND retirement_operation_id=:op"),{'id':plan['intent']['timelapse_id'],'op':nonce})
    if changed.rowcount!=1:_refuse('ownership_changed')
    db.execute(intent.update().where(intent.c.operation_id==nonce,intent.c.revision==plan['intent']['revision']).values(state='complete',completed_at=stamp,revision=plan['intent']['revision']+1,reason_code=None))
    from core.audit import log_audit
    log_audit(db,'retirement_complete','timelapse',plan['intent']['timelapse_id'],{'operation_id':nonce,'actor_id':principal['id']})


def retire_sealed_lifetime(engine,root,tid,evidence,*,expected_nonce,budget=None):
    """Own fixture-capable service; explicit nonce and actual request evidence."""
    if not isinstance(engine,Engine) or type(tid) is not int or tid<=0 or type(expected_nonce) is not str:_refuse('unknown_authority')
    from modules.archives.timelapse_action_authority import ActionCredential
    if type(evidence) is not ActionCredential or evidence.purpose!='retire':_refuse('unknown_authority')
    from core.timelapse_request_budget import VerificationBudget
    budget=budget or VerificationBudget();budget.check()
    deadline=budget.deadline;remaining=budget.remaining
    with acquire_media_guard(root,tid) as guard,_root(root) as (root_fd,identity):
        guard.assert_root_identity(identity)
        plan=_refresh(engine,evidence,tid,expected_nonce,identity,guard=guard,budget=budget)
        handoff=[]
        physical=[r for r in plan['objects'] if r['state']!='originally_absent']
        if not physical:
            _absence(root_fd,identity,plan)
            if plan['intent']['state']=='pending':
                with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):
                    guard.assert_root_identity(identity);_absence(root_fd,identity,actual)
                    _progress(db,actual,state='in_flight')
                plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,budget=budget) as (db,principal,actual):
                guard.assert_root_identity(identity);_absence(root_fd,identity,actual)
                budget.check();_complete(db,principal,actual)
            return RetiredMedia(expected_nonce)
        original=plan['quarantine'][0] if plan['quarantine'] else None
        with quarantine_directory(root,identity,directory_identity('.retirement',original) if original else None,create=original is None) as (qfd,qidentity):
            if original is None:
                with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):
                    guard.assert_root_identity(identity)
                    _journal_delta(db,actual,'quarantine',{k:getattr(qidentity,k) for k in ('device','inode','uid','gid','mode')},insert=True)
                    _progress(db,actual,state='in_flight')
                plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            elif plan['intent']['state']=='pending':
                with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):_progress(db,actual,state='in_flight')
                plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            parent_ids={r['relative_path']:directory_identity(r['relative_path'],r) for r in plan['parents']}
            ordered=lambda rows:sorted(rows,key=lambda r:(r['kind']=='directory',-r['relative_path'].count('/'),r['object_index']))
            if any(r['kind']=='file' and r['state']=='planned' and next(m for m in plan['moves'] if m['object_index']==r['object_index'])['phase']=='planned' for r in physical):
                with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):_admit_batch(db,actual,'files')
                plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            for index in [r['object_index'] for r in ordered(physical)]:
                budget.check()
                row=next(r for r in plan['objects'] if r['object_index']==index);move=next(m for m in plan['moves'] if m['object_index']==index)
                if move['phase']=='staged':continue
                if move['phase']=='planned':
                    with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):
                        _journal_delta(db,actual,'moves',{'phase':'rename_pending'},index=index);_progress(db,actual)
                    plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
                row=next(r for r in plan['objects'] if r['object_index']==index)
                fs=StageFilesystem(root_fd,qfd,identity,qidentity,plan['objects'],parent_ids,{m['object_index']:m for m in plan['moves']},guard)
                fs.deadline=min(fs.deadline,deadline);fs.remaining=min(fs.remaining,remaining)
                # Check typed originally-absent children while their parent still exists.
                for child in plan['objects']:
                    if child['state']=='originally_absent':fs.verify(child)
                with verify_action(fs,row,action='rename',snapshot=plan) as token:
                    remaining=fs.remaining;budget.remaining=remaining
                    with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):
                        budget.check();stamp=apply_verified_action(token,snapshot=actual)
                        _journal_delta(db,actual,'moves',{'phase':'staged','verified_ctime_ns':stamp},index=index)
                        _journal_delta(db,actual,'objects',{'state':'staged'},index=index);_progress(db,actual)
                plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            if any(r['object_index'] not in {u['object_index'] for u in plan['unlinks']} for r in physical):
                with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):_admit_batch(db,actual,'unlinks')
                plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            for index in [r['object_index'] for r in ordered(physical)]:
                budget.check()
                row=next(r for r in plan['objects'] if r['object_index']==index)
                receipt=next((u for u in plan['unlinks'] if u['object_index']==index),None)
                if receipt and receipt['phase']=='retired':continue
                if receipt is None:_refuse('phase_uncertain')
                fs=LifetimeDrainFilesystem(root_fd,qfd,identity,qidentity,plan['objects'],parent_ids,{m['object_index']:m for m in plan['moves']},{u['object_index']:u for u in plan['unlinks']},guard,membership=plan['members'])
                fs.deadline=min(fs.deadline,deadline);fs.remaining=min(fs.remaining,remaining)
                fs.preflight()
                if receipt['phase']=='unlink_pending':
                    with verify_action(fs,row,action='unlink',snapshot=plan) as token:
                        remaining=fs.remaining;budget.remaining=remaining
                        with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):
                            budget.check();apply_verified_action(token,snapshot=actual)
                            _journal_delta(db,actual,'unlinks',{'phase':'unlink_confirmed'},index=index)
                            _journal_delta(db,actual,'objects',{'state':'unlink_confirmed'},index=index);_progress(db,actual)
                    plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
                for before,after in (('unlink_confirmed','fsync_confirmed'),('fsync_confirmed','retired')):
                    receipt=next(u for u in plan['unlinks'] if u['object_index']==index)
                    if receipt['phase']!=before:continue
                    with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,handoff=handoff,budget=budget) as (db,_,actual):
                        fs.assert_absent(row);os.fsync(qfd);fs.assert_absent(row)
                        _journal_delta(db,actual,'unlinks',{'phase':after},index=index)
                        _journal_delta(db,actual,'objects',{'state':after},index=index);_progress(db,actual)
                    plan=_take_handoff(handoff,guard,tid,expected_nonce,identity)
            fs=LifetimeDrainFilesystem(root_fd,qfd,identity,qidentity,plan['objects'],parent_ids,{m['object_index']:m for m in plan['moves']},{u['object_index']:u for u in plan['unlinks']},guard,membership=plan['members'])
            fs.deadline=min(fs.deadline,deadline);fs.remaining=min(fs.remaining,remaining)
            fs.preflight()
            with _transaction(engine,evidence,tid,expected_nonce,identity,plan,guard=guard,budget=budget) as (db,principal,actual):
                budget.check();fs.preflight();_complete(db,principal,actual)
            return RetiredMedia(expected_nonce)


from core.timelapse_media_errors import NoCompletedReceiptYet

def sealed_lifetime_receipt(engine,evidence,*,operation_id,original_id=None):
    """Current-authorized immutable replay; never act on a reused live ID."""
    from core.timelapse_retirement_membership_validation import completion_proof
    from core.timelapse_access import immutable_timelapse_policy,_PROJECTION
    from modules.archives.timelapse_action_authority import ActionCredential
    if type(evidence) is not ActionCredential or evidence.purpose!='retire':_refuse('unknown_authority')
    with Session(engine) as db:
        fence_authorization(db);principal=fresh_action_principal(db,evidence)
        selected=_rows(db,intent,intent.c.operation_id==operation_id,lock=True)
        if selected and original_id is not None and selected[0]['timelapse_id']!=original_id:_refuse('unknown_authority')
        if not selected or selected[0]['state']!='complete':raise NoCompletedReceiptYet()
        policy,parameters=immutable_timelapse_policy(principal,mutation=True)
        if not db.execute(text('SELECT a.operation_id FROM '+_PROJECTION+' WHERE a.operation_id=:op AND '+policy),dict(parameters,op=operation_id)).first():_refuse('unknown_authority')
        if not db.execute(text('SELECT 1 FROM timelapse_retirements n WHERE operation_id=:op AND '+completion_proof('n')),{'op':operation_id}).first():_refuse('phase_uncertain')
        db.rollback()
    return RetiredMedia(operation_id)
