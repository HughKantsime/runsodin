"""Fail closed when an adopted enrollment table lacks authority boundaries."""
import re
from sqlalchemy import inspect, text

COLUMNS={'jti','user_id','expires_at','state','encrypted_pending_secret','created_at','consumed_at'}


def normalized_check(value):
    # Preserve literals; only normalize SQL syntax outside quoted segments.
    parts=re.split(r"('(?:[^']|'')*')",str(value))
    for index in range(0,len(parts),2):
        item=parts[index].lower()
        item=re.sub(r'::(?:text|character varying|double precision|numeric|bigint|integer)', '', item)
        item=item.replace('= any (array[', 'in (').replace('])', ')')
        parts[index]=re.sub(r'[\s"]', '', item)
    return ''.join(parts)


def validate_enrollment_shape(connection):
    inspector=inspect(connection)
    columns={c['name']:c for c in inspector.get_columns('mfa_enrollments')}
    valid=set(columns)==COLUMNS
    numeric_type='DOUBLE PRECISION' if connection.dialect.name=='postgresql' else 'REAL'
    canonical_types={'jti':'TEXT','user_id':'INTEGER','expires_at':numeric_type,
                     'state':'TEXT','encrypted_pending_secret':'TEXT','created_at':numeric_type,
                     'consumed_at':numeric_type}
    valid=valid and all(columns[name]['type'].compile(dialect=connection.dialect).upper()==expected
                         for name,expected in canonical_types.items())
    for name in ('jti','user_id','expires_at','state','created_at'):
        valid=valid and columns.get(name,{}).get('nullable') is False
    valid=valid and inspector.get_pk_constraint('mfa_enrollments').get('constrained_columns')==['jti']
    fks=inspector.get_foreign_keys('mfa_enrollments')
    valid=valid and len(fks)==1
    for name in COLUMNS-{'state'}:
        valid=valid and columns.get(name,{}).get('default') is None
    if connection.dialect.name == 'sqlite':
        foreign_rows=connection.exec_driver_sql('PRAGMA foreign_key_list(mfa_enrollments)').fetchall()
        valid=valid and len(foreign_rows)==1 and any(r[2]=='users' and r[3]=='user_id' and r[4]=='id' and r[6].upper()=='CASCADE' for r in foreign_rows)
    else:
        valid=valid and any(f.get('constrained_columns')==['user_id']
            and f.get('referred_table')=='users' and f.get('referred_columns')==['id']
            and str(f.get('options',{}).get('ondelete','')).upper()=='CASCADE' for f in fks)
    for name in ('encrypted_pending_secret','consumed_at'):
        valid=valid and columns.get(name,{}).get('nullable') is True
    valid=valid and normalized_check(columns.get('state',{}).get('default',''))=="'pending'"
    expected={
        "mfa_enrollment_jti_nonempty":"length(jti)>0",
        "mfa_enrollment_expiry_bound":"expires_at>0andexpires_at<=253402300799",
        "mfa_enrollment_state":"statein('pending','consumed','cancelled')",
        "mfa_enrollment_terminal_secret":"state='pending'orencrypted_pending_secretisnull",
    }
    if connection.dialect.name == 'postgresql':
        expected["mfa_enrollment_expiry_bound"]="expires_at>0andexpires_at<='253402300799'"
    actual={c.get('name'):normalized_check(c.get('sqltext','')) for c in inspector.get_check_constraints('mfa_enrollments')}
    valid=valid and set(actual)==set(expected) and all(actual.get(name)==value for name,value in expected.items())
    if valid and connection.dialect.name == 'postgresql':
        valid=connection.execute(text("""
            SELECT COUNT(*)=0 FROM pg_constraint
            WHERE conrelid=to_regclass('mfa_enrollments')
              AND contype IN ('c','f') AND NOT convalidated
        """)).scalar_one()
        valid=valid and connection.execute(text("""
            SELECT COUNT(*)=1 FROM pg_constraint
            WHERE conrelid=to_regclass(format('%I.mfa_enrollments',current_schema()))
              AND contype='f'
              AND confrelid=to_regclass(format('%I.users',current_schema()))
        """)).scalar_one()
    if not valid:
        raise RuntimeError('Invalid MFA enrollment authority schema')
    invalid_rows=connection.execute(text("""
        SELECT COUNT(*) FROM mfa_enrollments e LEFT JOIN users u ON u.id=e.user_id
        WHERE u.id IS NULL OR length(e.jti)=0
          OR NOT (e.expires_at>0 AND e.expires_at<=253402300799)
          OR NOT (e.created_at>=0 AND e.created_at<=253402300799)
          OR (e.consumed_at IS NOT NULL AND NOT (e.consumed_at>=0 AND e.consumed_at<=253402300799))
          OR e.state NOT IN ('pending','consumed','cancelled')
          OR (e.state!='pending' AND e.encrypted_pending_secret IS NOT NULL)
    """)).scalar_one()
    if invalid_rows:
        raise RuntimeError('Invalid MFA enrollment authority rows')
