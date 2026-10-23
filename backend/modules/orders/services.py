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
