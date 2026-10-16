"""Cert B: the capture-time tenancy fact (migration 033) follows media through edits
(trim/speed) and encode, so derived media of a single-tenant capture never become
install-wide once organizations exist (no timestamp inference)."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_timelapse_edit_http import http_edit  # noqa: F401
from tests.test_contracts.test_timelapse_edit_publication import edit, prepared_source, encoder, frame_writer, review_db, owned_runtime  # noqa: F401


def _fact(engine, nonce):
    with Session(engine) as db:
        return db.execute(text("SELECT captured_single_tenant FROM timelapse_capture_tenancy WHERE operation_id=:n"), {"n": nonce}).scalar()


def _visible(engine, principal):
    from core.timelapse_access import timelapse_visibility
    condition, params = timelapse_visibility(principal)
    with Session(engine) as db:
        return set(db.execute(text("SELECT id FROM timelapses WHERE " + condition), params).scalars())


def _make_single_tenant_shared(engine, nonce):
    """Mark the source as captured while the install had no organizations (the anchor's
    other facts are immutable and checked by the publication authority)."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM timelapse_capture_tenancy WHERE operation_id=:n"), {"n": nonce})
        c.execute(text("INSERT INTO timelapse_capture_tenancy(operation_id,captured_single_tenant) VALUES(:n,TRUE)"), {"n": nonce})


def test_platform_admin_trim_of_a_single_tenant_capture_stays_out_of_other_orgs(http_edit):
    from core.auth import create_access_token
    client, engine, tid, nonce, _ = http_edit
    _make_single_tenant_shared(engine, nonce)
    with engine.begin() as c:  # a platform admin (no organization) edits, after organizations exist
        c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(90,'platform','p@example.test','x','admin',1)"))
    token = create_access_token({"sub": "platform", "role": "admin"})
    response = client.post(f"/timelapses/{tid}/trim", headers={"Authorization": "Bearer " + token, "If-Match": '"' + nonce + '"'},
                           json={"operation_id": "c" * 32, "start_seconds": 0, "end_seconds": .09})
    assert response.status_code == 200, response.text
    derived = response.json()["result_nonce"]
    assert _fact(engine, derived) in (1, True)  # copied from the source, not inferred from the edit's time
    foreign = {"id": 5, "role": "viewer", "group_id": 2}
    with Session(engine) as db:
        ids = set(db.execute(text("SELECT timelapse_id FROM timelapse_ownership_anchors WHERE operation_id IN (:a,:b)"),
                             {"a": nonce, "b": derived}).scalars())
    assert _visible(engine, foreign) & ids == set()
    assert ids <= _visible(engine, {"id": 90, "role": "admin", "group_id": None})


def test_speed_edit_copies_the_fact(http_edit):
    client, engine, tid, nonce, token = http_edit
    with engine.begin() as c:
        c.execute(text("DELETE FROM timelapse_capture_tenancy WHERE operation_id=:n"), {"n": nonce})
        c.execute(text("INSERT INTO timelapse_capture_tenancy(operation_id,captured_single_tenant) VALUES(:n,FALSE)"), {"n": nonce})
    response = client.post(f"/timelapses/{tid}/speed", headers={"Authorization": "Bearer " + token, "If-Match": '"' + nonce + '"'},
                           json={"operation_id": "d" * 32, "multiplier": 2})
    assert response.status_code == 200, response.text
    assert _fact(engine, response.json()["result_nonce"]) in (0, False)


from tests.test_contracts.test_timelapse_encoder_publication import encoder as encode_fixture, encode  # noqa: E402


def test_encode_copies_the_fact_to_the_published_anchor(frame_writer, review_db, monkeypatch, request):
    value = request.getfixturevalue("encode_fixture")
    writer, tid, root, nonce, _, _ = value
    review_db.execute(text("DELETE FROM timelapse_capture_tenancy WHERE operation_id=:n"), {"n": nonce})
    review_db.execute(text("INSERT INTO timelapse_capture_tenancy(operation_id,captured_single_tenant) VALUES(:n,TRUE)"), {"n": nonce})
    review_db.commit()
    assert encode(value)
    published = review_db.execute(text("SELECT ownership_operation_id FROM timelapses WHERE id=:id"), {"id": tid}).scalar_one()
    assert published != nonce
    assert review_db.execute(text("SELECT captured_single_tenant FROM timelapse_capture_tenancy WHERE operation_id=:n"),
                             {"n": published}).scalar() in (1, True)
