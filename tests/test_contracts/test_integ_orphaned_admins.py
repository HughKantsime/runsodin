"""Review fix O4: removing an admin's organization must not make them a platform superadmin.

A platform superadmin is an admin with no organization (core.rbac). Deleting an org or
group cleared group_id for every member, so its admins became superadmins; clearing an
admin's group through PATCH /users did the same. These now return 409 naming the admins,
who must be reassigned (or given another role) first."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

SUPER = {"id": 100, "role": "admin", "group_id": None, "is_active": True}


def _seed(db, admin_role="admin"):
    db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(70,'Doomed org',TRUE)"))
    db.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,group_id) VALUES"
                    "(100,'root','root@example.test','x','admin',TRUE,NULL),"
                    "(71,'org-admin','oa@example.test','x',:role,TRUE,70),"
                    "(72,'org-viewer','ov@example.test','x','viewer',TRUE,70)"), {"role": admin_role})
    db.commit()


def _superadmins(db):
    return sorted(r[0] for r in db.execute(text("SELECT username FROM users WHERE role='admin' AND group_id IS NULL")))


def test_deleting_an_org_with_admins_is_refused_and_names_them(review_db):  # noqa: F811
    from modules.organizations.routes import delete_org
    with Session(review_db.get_bind()) as db:
        _seed(db)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(delete_org(70, SUPER, db))
        assert refused.value.status_code == 409
        assert [a["username"] for a in refused.value.detail["admins"]] == ["org-admin"]
        db.rollback()
        assert _superadmins(db) == ["root"]
        assert db.execute(text("SELECT COUNT(*) FROM groups WHERE id=70")).scalar_one() == 1


def test_deleting_a_group_with_admins_is_refused(review_db, monkeypatch):  # noqa: F811
    from modules.organizations import routes_users
    monkeypatch.setattr(routes_users, "require_feature", lambda feature: None)
    with Session(review_db.get_bind()) as db:
        _seed(db)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(routes_users.delete_group(70, SUPER, db))
        assert refused.value.status_code == 409 and [a["id"] for a in refused.value.detail["admins"]] == [71]
        db.rollback()
        assert _superadmins(db) == ["root"]


def test_deleting_an_org_without_admins_still_unlinks_members(review_db):  # noqa: F811
    from modules.organizations.routes import delete_org
    with Session(review_db.get_bind()) as db:
        _seed(db, admin_role="operator")
        assert asyncio.run(delete_org(70, SUPER, db)) == {"status": "ok"}
        assert _superadmins(db) == ["root"]
        assert db.execute(text("SELECT group_id FROM users WHERE id=71")).scalar_one() is None


def test_clearing_an_admins_group_through_patch_is_refused(review_db):  # noqa: F811
    from modules.organizations.routes_users import UserUpdateRequest, update_user
    with Session(review_db.get_bind()) as db:
        _seed(db)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(update_user(71, UserUpdateRequest(group_id=None), SUPER, None, db))
        assert refused.value.status_code == 409
        db.rollback()
        assert _superadmins(db) == ["root"]
        # Demoting in the same request is allowed: the user is no longer an admin.
        asyncio.run(update_user(71, UserUpdateRequest(group_id=None, role="operator"), SUPER, None, db))
        assert _superadmins(db) == ["root"]
