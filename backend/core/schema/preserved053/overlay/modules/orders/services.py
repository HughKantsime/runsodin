"""Cross-module order lifecycle services; caller owns authorization and transaction."""


def lock_tenant_for_deletion(db, owner):
    """Serialize organization removal against connector and production writers."""
    from modules.orders.square_store import lock_owner
    lock_owner(db, owner)


def forget_tenant(db, owner):
    """Refuse retained production history, then remove local connector state.

    Caller must hold lock_tenant_for_deletion in this same transaction. This
    performs no commit and no external requests.
    """
    from modules.orders.square_store import forget_tenant as forget
    forget(db, owner)


def retain_component_model(db,owner,model_id):
    from modules.orders.finishing import guard_model_delete
    guard_model_delete(db,owner,model_id)


def assert_job_history_deletable(db, job_id):
    """Keep the job and its history while its production acceptance is retained."""
    from sqlalchemy import text
    from fastapi import HTTPException
    db.execute(text("UPDATE jobs SET id=id WHERE id=:id"), {"id":job_id})
    if db.execute(text('SELECT 1 FROM production_acceptances a JOIN jobs j ON j.economic_uid=a.job_uid WHERE j.id=:id LIMIT 1'),{'id':job_id}).first():
        raise HTTPException(409, 'Job has a retained production inspection; retain its timeline with that business record')
