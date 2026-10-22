"""Exact current commerce proof after caller locks; no content or reader authority."""
from sqlalchemy import text


def prove_financial_graph(db, attempt_id):
    row=db.execute(text('''SELECT p.id,p.economic_uid,p.economic_birth_nonce,p.scheduled_job_id,
        j.economic_uid AS job_uid,j.charged_to_org_id,j.order_item_id,
        i.economic_uid AS item_uid,i.order_id,o.economic_uid AS order_uid,o.org_id,
        o.economic_global_origin_nonce AS origin_nonce
        FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id
        JOIN order_items i ON i.id=j.order_item_id JOIN orders o ON o.id=i.order_id
        WHERE p.id=:id'''),{'id':attempt_id}).mappings().first()
    if row is None or not row['economic_uid'] or not row['economic_birth_nonce'] or row['charged_to_org_id'] != row['org_id']:
        raise ValueError('Unproved current financial graph')
    owner=('global',0) if row['org_id'] is None else ('org',row['org_id'])
    if owner[0]=='org' and (type(owner[1]) is not int or owner[1]<=0):
        raise ValueError('Unproved current financial graph')
    def exact(sql,params,expected):
        proof=db.execute(text(sql),params).first()
        if proof is None or tuple(proof)!=expected:
            raise ValueError('Unproved current financial graph')
    for kind,identifier,uid in [('attempt',row['id'],row['economic_uid']),('job',row['scheduled_job_id'],row['job_uid']),('item',row['order_item_id'],row['item_uid']),('order',row['order_id'],row['order_uid'])]:
        exact('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid',{'uid':uid},(kind,identifier,*owner))
    exact('SELECT resource_kind,resource_id,state,identity_uid,owner_scope,owner_key FROM cost_source_lifetimes WHERE birth_nonce=:birth',{'birth':row['economic_birth_nonce']},('attempt',row['id'],'known',row['economic_uid'],*owner))
    exact('SELECT job_uid,order_uid,item_uid,owner_scope,owner_key FROM cost_attempt_bindings WHERE attempt_uid=:uid',{'uid':row['economic_uid']},(row['job_uid'],row['order_uid'],row['item_uid'],*owner))
    exact('SELECT order_uid,item_uid,owner_scope,owner_key FROM cost_job_bindings WHERE job_uid=:uid',{'uid':row['job_uid']},(row['order_uid'],row['item_uid'],*owner))
    for kind,uid in [('job',row['job_uid']),('item',row['item_uid'])]:
        exact('SELECT order_uid,owner_scope,owner_key FROM cost_order_memberships WHERE child_uid=:uid AND child_kind=:kind',{'uid':uid,'kind':kind},(row['order_uid'],*owner))
    if owner[0]=='global':
        exact('SELECT origin_nonce,resource_kind,owner_scope,owner_key,proof_kind FROM cost_global_order_origins WHERE order_uid=:uid',{'uid':row['order_uid']},(row['origin_nonce'],'order','global',0,'verified_platform_admin_global_creation'))
        if db.execute(text('SELECT 1 FROM cost_global_origin_exclusions WHERE order_uid=:uid'),{'uid':row['order_uid']}).first() is not None:
            raise ValueError('Unproved current financial graph')
    return {'attempt_id':row['id'],'attempt_uid':row['economic_uid'],'attempt_birth':row['economic_birth_nonce'],
            'job_id':row['scheduled_job_id'],'item_id':row['order_item_id'],'order_id':row['order_id'],
            'job_uid':row['job_uid'],'item_uid':row['item_uid'],'order_uid':row['order_uid'],
            'owner_scope':owner[0],'owner_key':owner[1]}
