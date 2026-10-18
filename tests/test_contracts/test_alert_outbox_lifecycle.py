"""Alert outbox rows (migration 034): user deletion, dead letters and retention.

Rows are transient: dispatch deletes them. A deleted user leaves no id behind (the
actor becomes NULL; the row still delivers its job's alert). A row that keeps failing
stops being retried after MAX_ATTEMPTS and is pruned after the retention period.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_account_deletion_token_lifetime import deletion_case  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_user_limits import signed_license  # noqa: F401


def test_user_deletion_leaves_no_actor_id_in_the_outbox(deletion_case):  # noqa: F811
    client, owner, db, _ = deletion_case
    db.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(4401,'outbox','pending')"))
    db.execute(text("INSERT INTO alert_outbox(job_id,alert_kind,actor_user_id) VALUES(4401,'queue_added',31),(4401,'queue_added',3)"))
    db.commit()
    assert client.delete("/users/31", headers=owner).status_code == 200
    with Session(db.get_bind()) as fresh:
        actors = fresh.execute(text("SELECT actor_user_id FROM alert_outbox ORDER BY id")).scalars().all()
    assert 31 not in actors and 3 in actors


def test_failing_rows_stop_retrying_and_are_pruned_after_retention(review_db, monkeypatch):  # noqa: F811
    from modules.notifications import alert_dispatcher, alert_outbox
    engine = review_db.get_bind()
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,submitted_by) VALUES(4402,'dead','pending',1)"))
    review_db.execute(text("INSERT INTO alert_outbox(job_id,alert_kind,actor_user_id) VALUES(4402,'queue_added',1)"))
    review_db.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic persistent failure")
    monkeypatch.setattr(alert_dispatcher, "stage_alert", fail)
    for _ in range(alert_outbox.MAX_ATTEMPTS + 2):
        assert alert_outbox.sweep(engine, older_than_seconds=0) == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT attempts FROM alert_outbox")).scalar_one() == alert_outbox.MAX_ATTEMPTS
    old = datetime.now(timezone.utc) - timedelta(days=alert_outbox.DEAD_LETTER_RETENTION_DAYS + 1)
    with engine.begin() as c:
        c.execute(text("UPDATE alert_outbox SET created_at=:old"), {"old": old})
    alert_outbox.sweep(engine, older_than_seconds=0)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM alert_outbox")).scalar_one() == 0


def test_an_idle_sweep_writes_nothing(review_db):  # noqa: F811
    from sqlalchemy import event
    from modules.notifications import alert_outbox
    engine = review_db.get_bind()
    writes = []
    event.listen(engine, "before_cursor_execute",
                 lambda c, cur, statement, *a: writes.append(statement) if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) else None)
    assert alert_outbox.sweep(engine, older_than_seconds=0) == 0
    assert writes == []
