"""One supplement per original receipt, keyed by the existing system_config primary key."""
from sqlalchemy import literal, null, select
from core.models import SystemConfig

PREFIX = 'production_supplement:'


def key(receipt_id):
    return PREFIX + receipt_id


def receipt_rows(originals):
    """Inherit job, claim and owner from the parent. Its primary key names the supplement slot."""
    config = SystemConfig.__table__
    values = config.c.value
    children = select(
        values['id'].as_string().label('id'), originals.c.owner, originals.c.job_uid,
        originals.c.claim_token, values['recipe_id'].as_string().label('recipe_id'),
        values['request_hash'].as_string().label('request_hash'),
        values['definition'].as_string().label('definition'), originals.c.id.label('supplement_of')
    ).select_from(originals.join(config, config.c.key == literal(PREFIX) + originals.c.id))
    return select(originals, null().label('supplement_of')).union_all(children).subquery('inspection_receipts')


def insert(db, parent_id, identifier, recipe_id, request_hash, definition):
    db.execute(SystemConfig.__table__.insert().values(key=key(parent_id), value={
        'id': identifier, 'recipe_id': recipe_id, 'request_hash': request_hash, 'definition': definition,
    }))
