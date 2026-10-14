"""Per-snapshot query/operation proof; no cross-snapshot authorization cache."""
import json,time
from pathlib import Path
from sqlalchemy import event,select
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_snapshot_graph import graph_session
from tests.test_contracts.test_timelapse_edit_publication import edit,run,prepared_source,encoder,frame_writer,review_db,owned_runtime
from core.timelapse_recipes import make_recipe
from core.timelapse_publication_schema import publications

def test_exact_duplicate_walk_removed_but_each_fresh_snapshot_revalidates(edit,monkeypatch):
    import modules.archives.timelapse_speed_dependencies as dependencies
    from modules.archives.timelapse_lifetime_action import _sharing
    service,engine,root,tid,nonce,evidence=edit
    for index,speed in enumerate((.5,1,1.5,2,4,8)):
        run(edit,identifier=f'{index+1:032x}',recipe=make_recipe('speed',multiplier=speed))
    actual=dependencies.operation;calls=[];queries=[]
    def validate(db,row):calls.append(row['operation_id']);return actual(db,row)
    def query(c,cur,statement,*a):queries.append(statement.split(None,1)[0])
    monkeypatch.setattr(dependencies,'operation',validate);event.listen(engine,'before_cursor_execute',query)
    try:
        with graph_session(edit) as (db,scope):
            life=db.execute(select(publications.c.source_lifetime).where(publications.c.operation_id==f'{1:032x}')).scalar_one()
            ops=[dict(r) for r in db.execute(select(publications).where((publications.c.source_lifetime==life)|(publications.c.result_lifetime==life))).mappings()]
            plan={'binding':{'lifetime_nonce':life},'objects':[],'publications':ops,'namespaces':[],'intent':{'timelapse_id':tid}}
            def old_partition():
                # Exact original duplicated validation behavior, readonly.
                for row in ops:
                    dependencies.operation(db,row)
                    if row['kind']=='speed':dependencies.completed_speed_source(db,row)
            calls.clear();queries.clear();start=time.monotonic()
            old_partition();old_partition()
            old={'operations':len(calls),'queries':len(queries),'seconds':time.monotonic()-start}
            calls.clear();queries.clear();start=time.monotonic()
            partition=dependencies.physical_partition(db,life,ops,scope=scope)
            _sharing(db,plan,partition=partition)
            new={'operations':len(calls),'queries':len(queries),'seconds':time.monotonic()-start}
            assert len(calls)==len(set(calls))==len(ops)
            first=set(calls);calls.clear()
            dependencies.physical_partition(db,life,ops,scope=scope)
            assert set(calls)==first and len(calls)==len(first)
            assert new['operations']<old['operations'] and new['queries']<old['queries']
        import tempfile
        output=Path(tempfile.gettempdir())/'timelapse-editor-retirement-graph-profile.json'
        output.write_text(json.dumps({'scope':'Real SQLite same-snapshot query counts; before is exact duplicate-walk emulation, after includes current sharing checks. No PG timing inference.',
                                     'related_operations':len(ops),'before':old,'after':new,'fresh_snapshot_revalidates':True},indent=2)+'\n')
    finally:event.remove(engine,'before_cursor_execute',query)
