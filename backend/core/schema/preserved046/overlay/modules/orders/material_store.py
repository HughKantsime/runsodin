"""Encrypted purchase evidence and explicit custody of existing stock rows."""
import json,uuid,hashlib
from fastapi import HTTPException
from cryptography.fernet import InvalidToken
from sqlalchemy import select,update,cast,String
from sqlalchemy.exc import IntegrityError
from core.models import SystemConfig
from modules.orders import square_store as square,batch_production as batches
from modules.inventory.models import Consumable

PREFIX='material_purchases:'
IDENTITY='material_identity:'
def key(owner):return PREFIX+str(owner)
def digest(value):return hashlib.sha256(batches.packed(value).encode()).hexdigest()
def empty(owner):return {'version':1,'owner':owner,'revision':None,'materials':{},'mappings':{},'lines':{},'events':{}}
def identity(db,cid):
    raw=db.execute(select(SystemConfig.value).where(SystemConfig.key==IDENTITY+str(cid))).scalar_one_or_none()
    if raw is None:return None
    try:
        doc=json.loads(raw)
        if doc['version']!=1 or type(doc['owner']) is not int or doc['owner']<0 or not isinstance(doc['uid'],str) or len(doc['uid'])!=32 or not isinstance(doc['snapshot'],dict):raise ValueError()
        return doc
    except (ValueError,KeyError,TypeError):raise HTTPException(503,'Material identity needs administrator review; retain its records.') from None

def snapshot(item):return {'id':item.id,'name':item.name,'sku':item.sku,'unit':item.unit,'vendor':item.vendor,'created_at':str(item.created_at)}
def guard(db,item,owner,*,write=False):
    doc=identity(db,item.id)
    if doc is None:return None
    if doc['owner']!=owner:raise HTTPException(404,'Material not found')
    if doc['snapshot']!=snapshot(item):raise HTTPException(409,'Managed material identity changed. Retain it for review.')
    return doc

def access(db,item,user,*,locked=False):
    if locked:
        if db.execute(update(Consumable).where(Consumable.id==item.id).values(id=Consumable.id)).rowcount!=1:raise HTTPException(404,'Consumable not found')
        db.refresh(item)
    doc=identity(db,item.id)
    if doc is not None:guard(db,item,square.tenant(user))
    return doc

def load(db,owner):
    raw=db.execute(select(SystemConfig.value).where(SystemConfig.key==key(owner))).scalar_one_or_none()
    if raw is None:return empty(owner),None
    try:
        if not isinstance(raw,str) or len(raw)>4000000:raise ValueError()
        doc=json.loads(square.cipher().decrypt(raw.encode()))
        if doc['version']!=1 or doc['owner']!=owner:raise ValueError()
        for name,limit in [('materials',200),('mappings',200),('lines',500),('events',1000)]:
            if not isinstance(doc[name],dict) or len(doc[name])>limit:raise ValueError()
        return doc,raw
    except (InvalidToken,ValueError,TypeError,KeyError):raise HTTPException(503,'Purchase evidence cannot be decrypted or validated. Retain records and restore the server key.') from None

def reserve(db,owner,user=None):
    """Common mutex before any material row, including legacy-only finishing."""
    square.lock_owner(db,owner,user)
    marker='material_mutex:'+str(owner)
    if db.execute(select(SystemConfig.key).where(SystemConfig.key==marker)).first() is None:
        try:db.add(SystemConfig(key=marker,value={'version':1,'owner':owner}));db.flush()
        except IntegrityError:db.rollback();raise HTTPException(409,'Material reservation changed. Retry after reload.') from None
    else:db.execute(update(SystemConfig).where(SystemConfig.key==marker).values(value=SystemConfig.value).execution_options(synchronize_session=False))

def lock(db,owner,user=None):
    reserve(db,owner,user)
    return load(db,owner)

def save(db,owner,doc,raw):
    doc['revision']=uuid.uuid4().hex;plain=batches.packed(doc)
    if len(plain)>2800000:raise HTTPException(409,'Material evidence capacity reached. Retain history for review.')
    encoded=square.cipher().encrypt(plain.encode()).decode()
    if raw is None:
        try:db.add(SystemConfig(key=key(owner),value=encoded));db.flush()
        except IntegrityError:db.rollback();raise HTTPException(409,'Material state changed. Reload before continuing.') from None
    elif db.execute(update(SystemConfig).where(SystemConfig.key==key(owner),cast(SystemConfig.value,String)==json.dumps(raw)).values(value=encoded).execution_options(synchronize_session=False)).rowcount!=1:raise HTTPException(409,'Material state changed. Reload before continuing.')

def own_material(db,owner,cid,*,locked=False):
    if locked:
        if db.execute(update(Consumable).where(Consumable.id==cid).values(id=Consumable.id)).rowcount!=1:raise HTTPException(404,'Material not found')
    item=db.execute(select(Consumable).where(Consumable.id==cid).execution_options(populate_existing=True)).scalar_one_or_none()
    if item is None:raise HTTPException(404,'Material not found')
    doc=guard(db,item,owner)
    if doc is None:raise HTTPException(409,'Choose an explicitly managed material before purchase receiving.')
    return item,doc

def guard_tenant(db,owner):
    doc,_=load(db,owner)
    if any(doc[n] for n in ('materials','mappings','lines','events')):raise HTTPException(409,'Retain organizations with material purchase or physical stock evidence.')
    # Refuse even if encrypted state was lost, rather than orphaning custody.
    identities=db.execute(select(SystemConfig.value).where(SystemConfig.key.startswith(IDENTITY))).scalars()
    if any(json.loads(raw).get('owner')==owner for raw in identities):raise HTTPException(409,'Retain organizations with managed material identities.')
