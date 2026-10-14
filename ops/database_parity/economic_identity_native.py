"""Actual owned PG transactions, locks, guards, rollback and adoption."""
import importlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
from sqlalchemy import Column,Integer,create_engine,text
from sqlalchemy.orm import DeclarativeBase,Session,attributes
import core.economic_identity as helper
M=importlib.import_module('core.schema.migrations.021_economic_identity')
F=importlib.import_module('core.schema.migrations.009_cost_foundation')
class Base(DeclarativeBase):pass
class Order(Base):
    __tablename__='orders'
    id=Column(Integer,primary_key=True)
    org_id=Column(Integer)
class Item(Base):
    __tablename__='order_items'
    id=Column(Integer,primary_key=True)
    order_id=Column(Integer,nullable=False)
class Job(Base):
    __tablename__='jobs'
    id=Column(Integer,primary_key=True)
    order_item_id=Column(Integer)
    charged_to_org_id=Column(Integer)
url='postgresql+psycopg://postgres@'+sys.argv[1]+'/postgres'
admin=create_engine(url)
schema='identity_'+uuid4().hex
with admin.begin() as c:c.exec_driver_sql('CREATE SCHEMA '+schema)
engine=create_engine(url,connect_args={'options':'-csearch_path='+schema})
receipt={'passed':False,'native_checks':[]}
def checked(name):receipt['native_checks'].append(name)
def graph(s):
    o=helper._add_created(s,Order(org_id=7),'order',owner_org_id=7)
    i=helper._add_created(s,Item(order_id=o.id),'item',owner_org_id=7)
    j=helper._add_created(s,Job(order_item_id=i.id,charged_to_org_id=7),'job',owner_org_id=7)
    return o,i,j
try:
    with engine.begin() as c:Base.metadata.create_all(c);F.apply(c);M.apply(c)
    with Session(engine) as s:
        o,i,j=graph(s);s.commit();ids=(o.id,i.id,j.id)
    with engine.begin() as c:M.apply(c)
    checked('fresh graph and complete populated adoption')
    with Session(engine) as s:graph(s);s.rollback()
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==3
    checked('caller rollback retains neither identity nor source half')
    for sql in ['UPDATE orders SET economic_uid=NULL','UPDATE orders SET org_id=9','UPDATE order_items SET order_id=100','UPDATE jobs SET order_item_id=NULL','UPDATE jobs SET charged_to_org_id=9',"INSERT INTO orders(id,org_id,economic_uid) SELECT 100,org_id,economic_uid FROM orders"]:
        with engine.connect() as c:
            try:c.execute(text(sql))
            except Exception as e:assert 'economic source' in str(e);c.rollback()
            else:raise AssertionError('source guard accepted '+sql)
    checked('direct insertion clearing replacement and ownership/linkage guards')
    entered=Event();release=Event()
    def held():
        with Session(engine) as s:
            uid=helper._validate_existing(s,'job',ids[2],owner_org_id=7);entered.set();assert release.wait(5);s.commit();return uid
    def waiting():
        assert entered.wait(5)
        with Session(engine) as s:
            start=time.monotonic();uid=helper._validate_existing(s,'job',ids[2],owner_org_id=7);elapsed=time.monotonic()-start;s.commit();return uid,elapsed
    with ThreadPoolExecutor(max_workers=2) as p:
        a=p.submit(held);b=p.submit(waiting);assert entered.wait(5);time.sleep(.25);assert not b.done();release.set();uid=a.result();other,elapsed=b.result()
    assert uid==other and elapsed>=.2
    receipt['actual_lock_wait_seconds']=elapsed
    checked('actual concurrent parent lock wait with stable existing UID replay')
    with Session(engine) as s:
        o=Order(org_id=7);cap=helper._capture_fresh(o,'order',owner_org_id=7);s.add(o);s.flush();identifier=o.id
        uid=helper._allocate_created(s,cap);s.commit()
    with engine.begin() as c:c.execute(text('DELETE FROM orders WHERE id=:id'),{'id':identifier})
    with Session(engine) as s:
        o=Order(org_id=7);cap=helper._capture_fresh(o,'order',owner_org_id=7);s.add(o);s.flush();newid=o.id
        # Assigned fresh source is explicitly moved before identification.
        s.execute(text('UPDATE orders SET id=:old WHERE id=:new'),{'old':identifier,'new':newid})
        attributes.set_committed_value(o,'id',identifier)
        fresh=helper._allocate_created(s,cap);assert fresh!=uid;s.commit()
    checked('deleted numeric source reuse receives fresh UID with retained history')
    with Session(engine) as s:
        o=helper._add_created(s,Order(org_id=7),'order',owner_org_id=7)
        other=helper._add_created(s,Order(org_id=7),'order',owner_org_id=7)
        item=Item(order_id=o.id);cap=helper._capture_fresh(item,'item',owner_org_id=7);s.add(item);s.flush()
        original=helper._lock
        def change(db,table,identifier):
            if table=='orders':db.execute(text('UPDATE order_items SET order_id=:other WHERE id=:id'),{'other':other.id,'id':item.id})
            return original(db,table,identifier)
        helper._lock=change
        try:
            try:helper._allocate_created(s,cap)
            except helper.EconomicIdentityError as e:assert 'Parent changed' in str(e)
            else:raise AssertionError('parent changed accepted')
        finally:helper._lock=original;s.rollback()
    checked('parent linkage recheck rejects raced parent')
    with engine.begin() as c:c.execute(text('DROP TRIGGER jobs_economic_guard ON jobs'))
    with engine.connect() as c:
        try:M.apply(c)
        except Exception as e:assert 'guard' in str(e);c.rollback()
        else:raise AssertionError('drift accepted')
    checked('missing exact source guard rejects adoption')
    receipt['passed']=True
finally:
    engine.dispose()
    with admin.begin() as c:c.exec_driver_sql('DROP SCHEMA '+schema+' CASCADE')
    admin.dispose()
print(json.dumps(receipt))
