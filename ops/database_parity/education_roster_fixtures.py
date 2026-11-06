"""D4 retained graph for the native backup/restore proof; requires the D2 seed."""
from sqlalchemy import text


def seed(connection):
    connection.execute(text("INSERT INTO education_roster_connections(id,org_id,provider,enabled,config_encrypted,created_by,connected_by) "
        "VALUES('roundtrip051',990049,'google',FALSE,'opaque-config-fixture',990049,990049)"))
    connection.execute(text("INSERT INTO education_roster_courses(connection_id,org_id,course_id,class_id) "
        "VALUES('roundtrip051',990049,'external-course','roundtrip049-class')"))
    connection.execute(text("INSERT INTO education_roster_identities(connection_id,org_id,subject,user_id) "
        "VALUES('roundtrip051',990049,'external-subject',990049)"))
    connection.execute(text("INSERT INTO education_roster_tombstones(connection_id,subject_hash) "
        "VALUES('roundtrip051','erased-subject-hash-fixture')"))
    connection.execute(text("INSERT INTO education_roster_grant_links(connection_id,org_id,subject,user_id) "
        "VALUES('roundtrip051',990049,'grant-only-subject',990049)"))
    connection.execute(text("INSERT INTO education_roster_receipts(id,connection_id,actor_id,kind,payload_encrypted,expires_at) "
        "VALUES('roundtrip051-receipt','roundtrip051',990049,'preview','opaque-preview-fixture',1)"))


def verify(connection):
    assert connection.execute(text("SELECT org_id,enabled,config_encrypted,connected_by FROM education_roster_connections WHERE id='roundtrip051'")).one() == (990049, False, 'opaque-config-fixture', 990049)
    assert connection.execute(text("SELECT class_id FROM education_roster_courses WHERE connection_id='roundtrip051'")).scalar_one() == 'roundtrip049-class'
    assert connection.execute(text("SELECT subject,user_id FROM education_roster_identities WHERE connection_id='roundtrip051'")).one() == ('external-subject', 990049)
    assert connection.execute(text("SELECT subject_hash FROM education_roster_tombstones WHERE connection_id='roundtrip051'")).scalar_one() == 'erased-subject-hash-fixture'
    assert connection.execute(text("SELECT subject,user_id FROM education_roster_grant_links WHERE connection_id='roundtrip051'")).one() == ('grant-only-subject', 990049)
    assert connection.execute(text("SELECT payload_encrypted,expires_at FROM education_roster_receipts WHERE id='roundtrip051-receipt'")).one() == ('opaque-preview-fixture', 1)
