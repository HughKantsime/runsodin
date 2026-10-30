"""Narrow Inventory transport. Mutations require deployment and tenant activation."""
import os
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from fastapi import HTTPException
from modules.orders.square_client import HOSTS, VERSION, SquareReader, identifier

# Deployment switch is intentionally captured once, at process import/start.
LIVE_WRITES_ENABLED = os.environ.get('ODIN_SQUARE_STOCK_WRITES') == '1'
FIELDS = frozenset(('catalog_object_id','from_location_id','to_location_id','from_state','to_state','quantity','occurred_at','reference_id'))
HEX = re.compile(r'^[a-f0-9]{32}$')


def invalid():
    raise HTTPException(409, 'Saved Square addition is invalid. Retain it for review; do not recreate it.')


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None: invalid()
        return parsed
    except (ValueError, TypeError, AttributeError):
        invalid()


def validate_request(raw, *, allow_legacy=False):
    try:
        if not isinstance(raw,str) or len(raw)>32768: invalid()
        doc=json.loads(raw)
        if set(doc)!= {'idempotency_key','changes'} or not HEX.fullmatch(doc['idempotency_key']): invalid()
        changes=doc['changes']
        if not isinstance(changes,list) or not 1<=len(changes)<=8: invalid()
        pairs=set()
        for change in changes:
            if not isinstance(change,dict) or set(change)!= {'type','adjustment'} or change['type']!='ADJUSTMENT': invalid()
            a=change['adjustment']
            if not isinstance(a,dict): invalid()
            legacy_fields = (FIELDS - {'from_location_id','to_location_id'}) | {'location_id'}
            if set(a)==legacy_fields:
                if not allow_legacy:
                    raise HTTPException(409, 'This addition was prepared for a retired Square API shape; review it')
                location = a.pop('location_id')
                a.update(from_location_id=location, to_location_id=location)
            if set(a)!=FIELDS: invalid()
            identifier(a['catalog_object_id']);identifier(a['from_location_id']);identifier(a['to_location_id'])
            if a['from_location_id']!=a['to_location_id']: invalid()
            if (a['from_state'],a['to_state'])!=('NONE','IN_STOCK'): invalid()
            if not isinstance(a['quantity'],str) or not re.fullmatch(r'[1-9][0-9]{0,4}',a['quantity']) or int(a['quantity'])>80000: invalid()
            if a['reference_id']!=doc['idempotency_key']: invalid()
            timestamp(a['occurred_at'])
            pair=(a['catalog_object_id'],a['to_location_id'])
            if pair in pairs: invalid()
            pairs.add(pair)
        return doc
    except (ValueError,TypeError,KeyError):
        invalid()


def read_request(raw):
    """Normalize a parsed copy for inspection; saved request bytes remain evidence."""
    return validate_request(raw, allow_legacy=True)


def exchange(environment, token, path, raw, *, tenant_enabled=False):
    """Only the two Inventory endpoints, with the mutation gate below all callers."""
    if environment not in HOSTS or path not in ('/v2/inventory/changes/batch-create','/v2/inventory/changes/batch-retrieve'):
        raise HTTPException(400,'Unsupported Square inventory operation')
    if path.endswith('batch-create') and (not LIVE_WRITES_ENABLED or tenant_enabled is not True):
        raise HTTPException(503,'Square stock writes require both activation keys')
    if path.endswith('batch-create'):validate_request(raw)
    try:
        with requests.Session() as session:
            session.trust_env=False
            with session.request('POST',HOSTS[environment]+path,data=raw.encode('utf-8'),
                headers={'Authorization':'Bearer '+token,'Square-Version':VERSION,'Content-Type':'application/json'},
                allow_redirects=False,stream=True,timeout=(4,10)) as response:
                if response.status_code!=200:
                    raise HTTPException(502,'Square did not acknowledge this request. Retain the operation for review.')
                chunks=[];size=0;deadline=time.monotonic()+15
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>4*1024*1024 or time.monotonic()>deadline: raise ValueError()
                    chunks.append(chunk)
                doc=json.loads(b''.join(chunks))
                if not isinstance(doc,dict) or doc.get('errors'): raise ValueError()
                return doc
    except HTTPException: raise
    except (requests.RequestException,ValueError,TypeError):
        raise HTTPException(502,'Square outcome could not be verified. Do not create another stock addition.') from None


def valid_counts(response, request):
    counts=response.get('counts')
    if not isinstance(counts,list):return False
    expected={(c['adjustment']['catalog_object_id'],c['adjustment']['to_location_id']) for c in request['changes']}
    found=Counter()
    try:
        for c in counts:
            if not isinstance(c,dict):return False
            if c.get('state')!='IN_STOCK':continue
            pair=(c.get('catalog_object_id'),c.get('location_id'))
            if pair not in expected:continue
            q=c.get('quantity')
            if not isinstance(q,str) or len(q)>64 or not Decimal(q).is_finite():return False
            found[pair]+=1
    except (InvalidOperation,TypeError):return False
    return set(found)==expected and all(n==1 for n in found.values())


class SquareWriter:
    def __init__(self, environment, token):
        self.environment,self.token=environment,token

    def send(self, raw, *, tenant_enabled):
        if not LIVE_WRITES_ENABLED or tenant_enabled is not True:
            raise HTTPException(503,'Square stock writes require both activation keys')
        request=validate_request(raw)
        # exchange gates mutations even if this class is invoked directly.
        response=exchange(self.environment,self.token,'/v2/inventory/changes/batch-create',raw,tenant_enabled=tenant_enabled)
        return valid_counts(response,request)


class SquareInventoryProbe(SquareReader):
    def reconciled(self, raw):
        """Bounded complete history, never current balances, proves old delivery."""
        request=read_request(raw)
        body={'catalog_object_ids':sorted({c['adjustment']['catalog_object_id'] for c in request['changes']}),
              'location_ids':sorted({c['adjustment']['to_location_id'] for c in request['changes']}),
              'types':['ADJUSTMENT'],'limit':100,
              'updated_after':(min(timestamp(c['adjustment']['occurred_at']) for c in request['changes'])-timedelta(days=1)).isoformat()}
        expected=Counter(adjustment_signature(c['adjustment']) for c in request['changes'])
        found=Counter();ids=set();cursors=set()
        for _ in range(3):
            response=exchange(next(k for k,v in HOSTS.items() if v==self.host),self.token,
                '/v2/inventory/changes/batch-retrieve',json.dumps(body,separators=(',',':')))
            rows=response.get('changes',[])
            if not isinstance(rows,list) or len(rows)>100:return False
            for row in rows:
                if not isinstance(row,dict):return False
                a=row.get('adjustment')
                if not isinstance(a,dict) or a.get('reference_id')!=request['idempotency_key']:continue
                if row.get('type')!='ADJUSTMENT' or not isinstance(a.get('id'),str) or not a['id'] or a['id'] in ids:return False
                ids.add(a['id'])
                try:signature=adjustment_signature(a)
                except (KeyError,ValueError,TypeError,InvalidOperation):return False
                found[signature]+=1
            cursor=response.get('cursor')
            if not cursor:return found==expected
            if not isinstance(cursor,str) or len(cursor)>4096 or cursor in cursors:return False
            cursors.add(cursor);body['cursor']=cursor
        return False


def adjustment_signature(a):
    # Square can normalize quantity and RFC3339 timestamp formatting.
    return tuple(timestamp(a[k]) if k=='occurred_at' else Decimal(a[k]) if k=='quantity' else a[k] for k in sorted(FIELDS))
