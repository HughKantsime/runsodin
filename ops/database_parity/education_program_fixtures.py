"""D3 050 retained reporting evidence, seeded after the D2 049 fixture."""
from sqlalchemy import text


def seed(connection):
    connection.execute(text("INSERT INTO education_program_marks(class_id,org_id,user_id,participant,concentrator,marked_by,revision) VALUES('roundtrip049-class',990049,990049,TRUE,TRUE,990049,2)"))
    connection.execute(text("INSERT INTO education_instructor_hours(id,class_id,org_id,instructor_id,activity_date,hours,approved_by,revision) VALUES('roundtrip050-hours','roundtrip049-class',990049,990049,'2026-10-01',2.5,990049,2)"))


def verify(connection):
    assert connection.execute(text("SELECT participant,concentrator,marked_by,revision FROM education_program_marks WHERE class_id='roundtrip049-class' AND user_id=990049")).one() == (True, True, 990049, 2)
    assert connection.execute(text("SELECT class_id,instructor_id,activity_date,hours,approved_by,revision FROM education_instructor_hours WHERE id='roundtrip050-hours'")).one() == ('roundtrip049-class', 990049, '2026-10-01', 2.5, 990049, 2)
