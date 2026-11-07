"""D3 school receipts record typed changes, never names or recipient addresses."""
import hashlib
import json
import uuid
from sqlalchemy import text
from core.audit import log_audit
from modules.organizations.education_classes_service import actor


def audit(db, principal, action, resource, resource_id, center_id=None, revision=1, *, details=None):
    org, _ = actor(db, principal)
    event = str(uuid.uuid4())
    details = details or {}
    encoded = json.dumps({'action': action, 'resource': str(resource_id), 'revision': revision,
                          'details': details}, sort_keys=True, separators=(',', ':'))
    db.execute(text('INSERT INTO education_audit_events '
        '(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,'
        'resource_id,cost_center_id,lifecycle_revision,details_json,result_json) '
        "VALUES(:e,:o,'user',:u,:a,:e,:h,:r,:id,:c,:v,:d,'{}')"),
        {'e': event, 'o': org, 'u': str(principal['id']), 'a': action,
         'h': hashlib.sha256(encoded.encode()).hexdigest(), 'r': resource, 'id': str(resource_id),
         'c': center_id, 'v': revision, 'd': json.dumps(details, sort_keys=True)})
    log_audit(db, action, 'education_program', details={'org_id': org, 'resource_id': str(resource_id),
                                                      'revision': revision})
