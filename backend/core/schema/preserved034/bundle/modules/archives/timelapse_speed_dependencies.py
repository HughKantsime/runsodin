"""Exact completed speed read dependencies, distinct from physical ownership."""
from sqlalchemy import select
from core.timelapse_publication_schema import publications,facts
from core.timelapse_publication_validation import operation,STABLE
from modules.archives.timelapse_retirement import _refuse

SOURCE_KEYS=STABLE+('link_count','byte_count','mtime_ns','digest_sha256')

def _completed_speed_source(db,op,validate):
    """Prove immutable source after physical retirement; never reopen old IDs."""
    validate(op)
    if op['kind']!='speed' or op['source_lifetime']==op['result_lifetime']:_refuse('shared_workspace')
    if op['phase']!='complete' or op['state']!='complete':_refuse('writer_active')
    prior=[dict(r) for r in db.execute(select(publications).where(publications.c.result_nonce==op['source_nonce'],publications.c.kind!='frame').order_by(publications.c.operation_id).limit(2).with_for_update(read=True,nowait=True)).mappings()]
    if len(prior)!=1 or prior[0]['phase']!='complete' or prior[0]['state']!='complete' or prior[0]['result_lifetime']!=op['source_lifetime'] or prior[0]['destination_path']!=op['source_filename']:_refuse('phase_uncertain')
    validate(prior[0])
    source=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==op['operation_id'],facts.c.role=='source').order_by(*facts.primary_key.columns).with_for_update(read=True,nowait=True)).mappings()]
    output=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==prior[0]['operation_id'],facts.c.role=='output').order_by(*facts.primary_key.columns).with_for_update(read=True,nowait=True)).mappings()]
    if len(source)!=1 or len(output)!=1 or source[0]['relative_path']!=op['source_filename'] or any(source[0][k]!=output[0][k] for k in SOURCE_KEYS) or source[0]['ctime_ns']!=prior[0]['published_ctime_ns']:_refuse('object_changed')
    # operation() proves ancestor paths and exact root identities; compare both
    # chains explicitly so a qualified complete source cannot bless aliases.
    incoming=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==op['operation_id'],facts.c.role.in_(('root','source_parent'))).order_by(*facts.primary_key.columns).with_for_update(read=True,nowait=True)).mappings()]
    original=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==prior[0]['operation_id'],facts.c.role.in_(('root','output_parent'))).order_by(*facts.primary_key.columns).with_for_update(read=True,nowait=True)).mappings()]
    def chain(rows):
        result={}
        for row in rows:
            key=row['relative_path'];value=tuple(row[k] for k in STABLE)
            if key in result and result[key]!=value:_refuse('object_changed')
            result[key]=value
        return result
    if chain(incoming)!=chain(original):_refuse('object_changed')
    return source[0]

def completed_speed_source(db,op):
    return _completed_speed_source(db,op,lambda row:operation(db,row))

def _physical_partition(db,lifetime,ops):
    """Keep every related row fenced; return only exact physical owners."""
    owned=[];outgoing=[];validated={}
    def validate(row):
        # One owned call/snapshot only: no session/global cache or reuse after
        # transaction/refresh. Shared prior publications still get row locks.
        identifier=row['operation_id']
        if identifier in validated:
            if validated[identifier]!=row:_refuse('phase_uncertain')
            return
        operation(db,row);validated[identifier]=dict(row)
    for op in ops:
        validate(op)
        if op['source_lifetime']==op['result_lifetime']==lifetime:
            owned.append(op)
        elif op['kind']=='speed' and op['result_lifetime']==lifetime:
            _completed_speed_source(db,op,validate);owned.append(op)
        elif op['kind']=='speed' and op['source_lifetime']==lifetime:
            _completed_speed_source(db,op,validate);outgoing.append(op)
        else:_refuse('shared_workspace')
    return owned,outgoing


def physical_partition(db,lifetime,ops,*,scope=None):
    from modules.archives.timelapse_snapshot_graph import SnapshotGraphReadView,GraphFenceScope
    if type(scope) is not GraphFenceScope:_refuse('unknown_authority')
    scope.check(db)
    if not ops:return [],[]
    view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=lifetime)
    try:return _physical_partition(view,lifetime,ops)
    finally:view.close()
