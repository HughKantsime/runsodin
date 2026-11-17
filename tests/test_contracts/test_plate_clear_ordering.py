"""The batched selector and write route retain the former single-printer SQL order."""
from datetime import datetime

from sqlalchemy import text

from tests.test_contracts.test_plate_clear_state import plate_client  # noqa: F401
from tests.test_contracts.test_plate_clear_confirmation import floor  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401


def assert_release_order(db, release):
    from modules.printers.plate_clear import select_plate_clear
    early, late = datetime(2026, 1, 1), datetime(2026, 1, 2)
    # Each successive winner exercises a tie-breaker: priority, ID, creation
    # time, position, set before null position, then priority again.
    rows = [(810, 1, 99, late), (811, 2, 1, late), (813, 2, 1, early),
            (812, 2, 1, early), (814, 2, 2, early), (815, 2, None, early),
            (816, 3, 0, early), (817, None, 0, early), (818, 2, 1, None)]
    for jid, priority, position, created in rows:
        db.execute(text('INSERT INTO jobs(id,item_name,printer_id,status,priority,queue_position,created_at) '
                        "VALUES(:id,'Ordering fixture',199,'waiting_plate_clear',:priority,:position,:created)"),
                   dict(id=jid, priority=priority, position=position, created=created))
    # A different printer must not interfere with the batched selection.
    db.execute(text("INSERT INTO printers(id,name) VALUES(198,'Other ordering printer')"))
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status,priority) "
                    "VALUES(819,'Other printer',198,'waiting_plate_clear',0)"))
    db.commit()
    dialect = db.get_bind().dialect.name
    # Native defaults differ for nullable priority/created_at: SQLite NULLS FIRST,
    # PostgreSQL NULLS LAST. Both the old and new selector must keep those defaults.
    expected_order = ([817, 810, 818, 812, 813, 811, 814, 815, 816] if dialect == 'sqlite'
                      else [810, 812, 813, 811, 818, 814, 815, 816, 817])
    for expected in expected_order:
        old = db.execute(text("SELECT id FROM jobs WHERE printer_id=199 AND status='waiting_plate_clear' "
                              "ORDER BY priority ASC, (queue_position IS NULL) ASC, queue_position ASC, created_at ASC, id ASC LIMIT 1")).scalar_one()
        assert old == expected
        selected, _, _ = select_plate_clear(db, [198, 199])
        assert selected == {198: 819, 199: old}
        before = dict(db.execute(text('SELECT id,status FROM jobs WHERE printer_id IN (198,199)')).all())
        assert release()['message'] == f'Job {old} released for dispatch'
        after = dict(db.execute(text('SELECT id,status FROM jobs WHERE printer_id IN (198,199)')).all())
        assert after == {**before, old: 'pending'}


def test_sqlite_release_matches_every_legacy_ordering_key(plate_client):
    client, issue, db = plate_client
    headers = issue(['write:printers'])

    def release():
        response = client.post('/printers/199/plate-cleared', headers=headers)
        assert response.status_code == 200
        return response.json()

    assert_release_order(db, release)
