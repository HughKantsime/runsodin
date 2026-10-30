"""Insert-once JSON records on the caller-owned connection."""
import json
from sqlalchemy import text, bindparam, JSON

def insert(conn, key, value):
    param = bindparam('value', type_=JSON) if conn.dialect.name == 'postgresql' else bindparam('value')
    return conn.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) ON CONFLICT(key) DO NOTHING').bindparams(param),
                        {'key': key, 'value': json.dumps(value, sort_keys=True)}).rowcount == 1

