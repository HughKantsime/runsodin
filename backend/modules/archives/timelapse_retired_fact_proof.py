"""Fresh canonical completion proof for exact historical inode-collision facts.

This never grants filesystem access, adopts bytes, or opens a historical path.
"""
from sqlalchemy import select,func,case,cast,LargeBinary,String,text
from core.timelapse_manifest import MAX_OBJECTS
from core.timelapse_publication_schema import publications,facts,lifetimes,versions,namespaces,retained,bindings as frame_bindings
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_retirement_membership_schema import bindings,members
from core.timelapse_retirement_membership_validation import completion_proof,static_binding_proof,FILE
from core.timelapse_publication_validation import STABLE
from core.timelapse_request_budget import VerificationBudget
from modules.archives.timelapse_snapshot_graph import GraphFenceScope,SnapshotGraphReadView,MAX_ROWS,MAX_BYTES,CHUNK
from modules.archives.timelapse_speed_dependencies import _physical_partition,SOURCE_KEYS
from modules.archives.timelapse_retirement import _refuse

_HISTORY=(intent,bindings,objects,members,publications)


def _charge(row):
    return sum(len(value.encode('utf8'))+16 if isinstance(value,str) else len(value)+16 if isinstance(value,bytes) else 64 for value in row.values())


def _history_rows(db,table,condition,allowance,budget):
    if not any(table is known for known in _HISTORY):_refuse('phase_uncertain')
    budget.check();charges=[]
    for column in table.columns:
        if isinstance(column.type,String):
            size=func.length(func.convert_to(column,'UTF8')) if db.get_bind().dialect.name=='postgresql' else func.length(cast(column,LargeBinary))
            charges.append(case((column.is_(None),64),else_=size+16))
        else:charges.append(64)
    count,total=db.execute(select(func.count(),func.coalesce(func.sum(sum(charges)),0)).select_from(table).where(condition)).one()
    if type(count) is not int or type(total) is not int or count<0 or total<0 or count>allowance[0] or total>allowance[1]:_refuse('manifest_limit')
    budget.check();cursor=db.execute(select(table).where(condition).order_by(*table.primary_key.columns).with_for_update(read=True,nowait=True).execution_options(stream_results=True,max_row_buffer=CHUNK));result=[]
    try:
        while True:
            budget.check();chunk=cursor.mappings().fetchmany(CHUNK)
            if not chunk:break
            for raw in chunk:
                row=dict(raw);charge=_charge(row)
                if allowance[0]<1 or allowance[1]<charge:_refuse('manifest_limit')
                allowance[0]-=1;allowance[1]-=charge;result.append(row)
    finally:cursor.close()
    if len(result)!=count:_refuse('phase_uncertain')
    return result


def _file_member_proves(op,output,member,object_row,operations,old_facts,lifetime,retirement):
    if object_row['state']!='retired' or object_row['kind']!='file':return False
    if member['role'] in ('current_video','completed_frame'):
        return (member['publication_id']==op['operation_id'] and member['fact_role']=='output'
                and member['fact_path']==output['relative_path'] and object_row['relative_path']==op['destination_path']
                and all(object_row[key]==output[key] for key in FILE) and object_row['ctime_ns']==op['published_ctime_ns'])
    if member['role']!='retained_video' or member['retained_version_nonce']!=op['result_nonce'] or member['version_nonce']!=op['result_nonce']:return False
    trim=operations.get(member['publication_id'])
    if trim is None or trim['kind']!='trim' or trim['source_nonce']!=op['result_nonce'] or trim['source_lifetime']!=lifetime or trim['source_filename']!=op['destination_path']:return False
    source=[row for row in old_facts if row['operation_id']==trim['operation_id'] and row['role']=='source' and row['relative_path']==member['fact_path']]
    return (len(source)==1 and member['fact_role']=='source' and object_row['relative_path']==op['destination_path']
            and all(source[0][key]==output[key] for key in SOURCE_KEYS) and source[0]['ctime_ns']==op['published_ctime_ns']
            and all(object_row[key]==source[0][key] for key in FILE) and object_row['ctime_ns']==source[0]['ctime_ns'])


def _never_created_gap_proves(db,op,lifetime,view,old_facts,old_objects,old_members,budget):
    if (op['kind'],op['phase'],op['state'])!=('frame','admitted','blocked') or op['source_lifetime']!=lifetime or op['result_lifetime']!=lifetime:return False
    if any(row['operation_id']==op['operation_id'] and row['role'] in ('private_root','private_creation','output') for row in old_facts):return False
    space=view.rows.get(namespaces.name,{}).get((op['frame_namespace_id'],))
    binding=view.rows.get(frame_bindings.name,{}).get((lifetime,))
    if (space is None or binding is None or space['phase']!='retired' or space['lifetime_nonce']!=lifetime
        or binding['namespace_id']!=space['namespace_id'] or not 1<=op['frame_number']<binding['next_number']):return False
    budget.check()
    count=db.execute(select(func.count()).select_from(publications).where(publications.c.kind=='frame',publications.c.frame_namespace_id==op['frame_namespace_id'],publications.c.frame_number==op['frame_number'])).scalar_one()
    budget.check()
    if count!=1:return False
    by_index={row['object_index']:row for row in old_objects}
    return any(member['role']=='frame_namespace' and member['namespace_id']==space['namespace_id']
               and by_index[member['object_index']]['state']=='retired' and by_index[member['object_index']]['kind']=='directory'
               and all(by_index[member['object_index']][key]==space[key] for key in STABLE) for member in old_members)


def retired_fact_exemptions(db,collisions,foreign_publications,*,scope,budget):
    """Return exact proven fact PKs, only inside this fenced caller transaction."""
    if (type(scope) is not GraphFenceScope or scope.retirement_proofs is not True
        or type(budget) is not VerificationBudget):_refuse('shared_workspace')
    scope.check(db);budget.check()
    if len(collisions)>MAX_OBJECTS or len(foreign_publications)>MAX_OBJECTS:_refuse('manifest_limit')
    foreign={row['operation_id']:row for row in foreign_publications}
    if len(foreign)!=len(foreign_publications):_refuse('phase_uncertain')
    grouped={}
    for fact in collisions:
        op=foreign.get(fact['operation_id'])
        if op is None or op['phase']!='complete' or op['state']!='complete':_refuse('shared_workspace')
        grouped.setdefault(op['result_lifetime'],[]).append(fact)
    allowance=[MAX_ROWS,MAX_BYTES];exempt=set()
    for lifetime in sorted(grouped):
        scope.check(db);budget.check()
        binding_rows=_history_rows(db,bindings,bindings.c.lifetime_nonce==lifetime,allowance,budget)
        if len(binding_rows)!=1 or binding_rows[0]['phase']!='sealed':_refuse('shared_workspace')
        binding=binding_rows[0];operation_id=binding['operation_id']
        intents=_history_rows(db,intent,intent.c.operation_id==operation_id,allowance,budget)
        if len(intents)!=1 or intents[0]['state']!='complete' or intents[0]['completed_at'] is None or intents[0]['owner_state'] not in ('known','shared'):_refuse('shared_workspace')
        if not db.execute(text('SELECT 1 FROM timelapse_retirement_lifetime_bindings n WHERE n.operation_id=:op AND '+static_binding_proof('n')),{'op':operation_id}).first():_refuse('shared_workspace')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments: static_binding_proof()/completion_proof() constant builders over a literal alias; values are bound
        if not db.execute(text("SELECT 1 FROM timelapse_retirements i WHERE i.operation_id=:op AND i.state='complete' AND "+completion_proof('i')),{'op':operation_id}).first():_refuse('shared_workspace')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments: static_binding_proof()/completion_proof() constant builders over a literal alias; values are bound
        budget.check()
        # Nonce relations, never numeric-ID adoption. Predicate fences block new references.
        if db.execute(text('SELECT 1 FROM timelapses t WHERE t.retirement_operation_id=:op OR t.ownership_operation_id IN (SELECT version_nonce FROM timelapse_media_versions WHERE lifetime_nonce=:life) LIMIT 1'),{'op':operation_id,'life':lifetime}).first():_refuse('shared_workspace')
        ops=_history_rows(db,publications,(publications.c.source_lifetime==lifetime)|(publications.c.result_lifetime==lifetime),allowance,budget)
        if not ops or len(ops)>MAX_OBJECTS or any((row['phase'],row['state'])!=('complete','complete') and (row['kind'],row['phase'],row['state'])!=('frame','admitted','blocked') for row in ops):_refuse('shared_workspace')
        view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=lifetime,row_limit=allowance[0],byte_limit=allowance[1])
        try:
            budget.check();owned,_outgoing=_physical_partition(view,lifetime,ops);budget.check()
            allowance[0]-=view.used_rows;allowance[1]-=view.used_bytes
            old_life=view.rows.get(lifetimes.name,{}).get((lifetime,))
            if old_life is None or any(old_life['root_'+key]!=getattr(scope.identity,key) for key in STABLE):_refuse('shared_workspace')
            old_facts=list(view.rows.get(facts.name,{}).values());operations={row['operation_id']:row for row in owned}
            old_objects=_history_rows(db,objects,objects.c.operation_id==operation_id,allowance,budget)
            old_members=_history_rows(db,members,members.c.operation_id==operation_id,allowance,budget)
            by_index={row['object_index']:row for row in old_objects}
            for operation in ops:
                if operation['phase']!='complete' and not _never_created_gap_proves(db,operation,lifetime,view,old_facts,old_objects,old_members,budget):_refuse('shared_workspace')
            for space in view.rows.get(namespaces.name,{}).values():
                if space['lifetime_nonce']!=lifetime:continue
                if space['inode'] is not None:
                    if space['phase']!='retired':_refuse('shared_workspace')
                elif not any(member['role']=='frame_namespace' and member['namespace_id']==space['namespace_id'] and by_index[member['object_index']]['state']=='originally_absent' for member in old_members):_refuse('shared_workspace')
            for old_version in view.rows.get(retained.name,{}).values():
                if old_version['lifetime_nonce']==lifetime and (old_version['state']!='retired' or old_version['retire_operation_id']!=operation_id):_refuse('shared_workspace')
            for fact in grouped[lifetime]:
                scope.check(db);budget.check();op=operations.get(fact['operation_id'])
                if op is None:_refuse('shared_workspace')
                recorded=[row for row in old_facts if (row['operation_id'],row['role'],row['relative_path'])==(fact['operation_id'],fact['role'],fact['relative_path'])]
                if len(recorded)!=1 or dict(recorded[0])!=fact:_refuse('shared_workspace')
                proved=False
                if fact['role']=='private_root':
                    for member in old_members:
                        obj=by_index.get(member['object_index'])
                        if (obj is not None and member['role']=='private_directory' and member['publication_id']==op['operation_id']
                            and member['fact_role']=='private_root' and member['fact_path']==fact['relative_path']
                            and obj['state']=='retired' and obj['kind']=='directory' and obj['relative_path']==op['private_directory']
                            and all(obj[key]==fact[key] for key in STABLE)):proved=True
                elif fact['role'] in ('output','private_creation'):
                    outputs=[row for row in old_facts if row['operation_id']==op['operation_id'] and row['role']=='output']
                    if len(outputs)!=1:_refuse('shared_workspace')
                    output=outputs[0]
                    if fact['role']=='private_creation' and any(fact[key]!=output[key] for key in STABLE):_refuse('shared_workspace')
                    for member in old_members:
                        obj=by_index.get(member['object_index'])
                        if obj is not None and _file_member_proves(op,output,member,obj,operations,old_facts,lifetime,operation_id):proved=True
                if not proved:_refuse('shared_workspace')
                exempt.add((fact['operation_id'],fact['role'],fact['relative_path']))
        finally:view.close()
    scope.check(db);budget.check()
    return frozenset(exempt)
