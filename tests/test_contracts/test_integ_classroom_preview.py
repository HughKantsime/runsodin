"""Google Classroom preview: the teacher/student roster and its diff against the mapped
cost center, read-only (FEATURES.md 4.11). Import itself: test_classroom_poc.py."""
import asyncio
from uuid import uuid4

from sqlalchemy import text

from tests.test_contracts.test_classroom_poc import _configure, _roster, classroom_db  # noqa: F401

ADMIN = {"id": 1, "role": "admin", "group_id": 1, "is_active": True}


def _counts(db):
    return tuple(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                 for table in ("users", "education_cost_center_grants", "classroom_course_mappings"))


def test_preview_shows_roster_and_diff_and_changes_nothing(classroom_db, monkeypatch):  # noqa: F811
    from modules.organizations import routes_classroom
    from modules.organizations.routes_classroom import (
        ClassroomImportInput, classroom_course_preview, import_classroom_course,
    )
    _configure(classroom_db)
    roster = {"value": _roster(include_student=True)}

    async def fake_roster(db, org_id, course_id):
        return roster["value"]
    monkeypatch.setattr(routes_classroom, "get_roster", fake_roster)

    before = _counts(classroom_db)
    first = asyncio.run(classroom_course_preview("course-123", None, ADMIN, classroom_db))
    assert [t["email"] for t in first["teachers"]] == ["teacher@ctechigh.org"]
    assert [s["email"] for s in first["students"]] == ["student@ctechigh.org"]
    assert first["mapping"] is None
    assert first["diff"] == {"added_or_changed": ["student@ctechigh.org", "teacher@ctechigh.org"],
                             "removed": [], "unchanged": 0}
    assert _counts(classroom_db) == before  # preview writes nothing

    asyncio.run(import_classroom_course("course-123", ClassroomImportInput(command_id=uuid4()), ADMIN, classroom_db))
    after_import = asyncio.run(classroom_course_preview("course-123", None, ADMIN, classroom_db))
    assert after_import["mapping"]["cost_center_id"] is not None
    assert after_import["diff"] == {"added_or_changed": [], "removed": [], "unchanged": 2}

    roster["value"] = _roster(include_student=False)
    counts = _counts(classroom_db)
    dropped = asyncio.run(classroom_course_preview("course-123", None, ADMIN, classroom_db))
    assert dropped["diff"] == {"added_or_changed": [], "removed": ["student@ctechigh.org"], "unchanged": 1}
    assert _counts(classroom_db) == counts
    assert classroom_db.execute(text(
        "SELECT g.state FROM education_cost_center_grants g JOIN users u ON u.id=g.user_id "
        "WHERE u.email='student@ctechigh.org'")).scalar_one() == "active"  # still active until a confirmed import
