"""GDPR erase covers quiet-hours deferred deliveries (migration 032).

The erased user's own rows are deleted. Copies of an alert held for other recipients
and for webhooks are kept for delivery, but the erased account's identifiers are
scrubbed from them, as erase_user_data does for the alerts table.
"""
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_privacy_replay_erasure_behavior import replay_erasure  # noqa: F401

TABLE = "quiet_hours_deferred_deliveries"


def _row(db, row_id, user_id, channel, title, message):
    db.execute(text(f"INSERT INTO {TABLE}(id,user_id,org_id,scope_key,channel,alert_type,severity,title,message) "
                    "VALUES(:i,:u,NULL,'system',:c,'job_submitted','info',:t,:m)"),
               {"i": row_id, "u": user_id, "c": channel, "t": title, "m": message})


def test_erase_deletes_own_rows_and_scrubs_copies_for_others(replay_erasure, review_db):  # noqa: F811
    assert inspect(review_db.get_bind()).has_table(TABLE)
    review_db.execute(text("UPDATE users SET username='erase-me-synthetic', email='erase-me@example.invalid' WHERE id=1"))
    _row(review_db, "own", 1, "email", "Your print finished", "erase-me-synthetic, your job is done")
    _row(review_db, "reviewer", 3, "email", "erase-me-synthetic submitted a job", "Review erase-me@example.invalid's job")
    _row(review_db, "hook", None, "webhook", "erase-me-synthetic submitted a job", None)
    _row(review_db, "other", 3, "push", "Printer 2 idle", "Nothing personal")
    review_db.commit()
    assert replay_erasure.delete("/users/1/erase").status_code == 200
    with Session(review_db.get_bind()) as db:
        rows = {r.id: (r.title, r.message) for r in db.execute(text(f"SELECT id,title,message FROM {TABLE}"))}
    assert set(rows) == {"reviewer", "hook", "other"}
    assert rows["reviewer"] == ("[deleted-user] submitted a job", "Review [deleted-user]'s job")
    assert rows["hook"] == ("[deleted-user] submitted a job", None)
    assert rows["other"] == ("Printer 2 idle", "Nothing personal")


def test_erase_without_the_table_is_unaffected(replay_erasure, review_db):  # noqa: F811
    review_db.execute(text(f"DROP TABLE {TABLE}")); review_db.commit()
    assert replay_erasure.delete("/users/1/erase").status_code == 200
