"""B3: "Refresh license" (reactivate) answers, earlier/expired replacements, last_refresh.

Spec: .claude/specs/odin-checkout-build.md Part B.4. Same ephemeral-signer and
isolated-device fixtures as test_paid_license_binding.py; no live portal.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import license_manager
from modules.system import license_refresh, routes_license
from tests.test_contracts.test_license_offline_import import owned_import  # noqa: F401  (fixture)
from tests.test_contracts.test_paid_license_binding import device, portal  # noqa: F401  (fixtures)

from core.schema.bootstrap import import_all_models

import_all_models()  # Every mapper registered, so the SystemConfig query works when this file runs alone.

REAL_FIND_LICENSE_FILE = license_manager._find_license_file  # Before any fixture replaces it.
KEY = "REFRESH-TEST"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta):
    return NOW - timedelta(**delta)


def ahead(**delta):
    return NOW + timedelta(**delta)


class FakeDB:
    """Just enough of a session for the one system_config row the refresh uses."""
    def __init__(self):
        self.rows = {}
        self._key = None

    def query(self, _model):
        return self

    def filter(self, clause):
        self._key = clause.right.value
        return self

    def first(self):
        return self.rows.get(self._key)

    def add(self, row):
        self.rows[row.key] = row

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def app(portal, monkeypatch):
    dev, routes, calls, response, _activate = portal
    (upload, path, _old, _audit, _mock_db, lm, sign), fields = dev
    db = FakeDB()
    monkeypatch.setattr(license_refresh, "_session", lambda: db)
    monkeypatch.setattr(routes, "get_license", lm.get_license)
    monkeypatch.setattr(routes_license, "get_license", lm.get_license)

    def make(issued, expires, **extra):
        return sign(**fields, key=KEY, issued_at=iso(issued), expires_at=iso(expires), **extra).decode()

    def restart():
        # A new process: no cached license, no cached identity, a fresh clock.
        monkeypatch.setattr(lm, "_cached_license", None)
        monkeypatch.setattr(lm, "_cache_file_identity", None)
        monkeypatch.setattr(lm, "_cached_identity", None)
        monkeypatch.setattr(lm, "_clock", None)

    def refresh(**body):
        response.clear()
        response.update(status=200, body=body)
        return asyncio.run(routes_license.reactivate_license(current_user={"id": 1}, db=db, license_mutation_allowed=None))

    return SimpleNamespace(lm=lm, routes=routes, path=path, make=make, refresh=refresh, restart=restart,
                           upload=upload, sign=sign, fields=fields, db=db, calls=calls)


def last_refresh(app):
    """The admin-only answer; the public license answer never carries it."""
    assert "last_refresh" not in app.routes.get_license_info()
    return routes_license.get_license_last_refresh(current_user={"id": 1})["last_refresh"]


@pytest.mark.parametrize("status", ["renewed", "canceled", "not_renewed_yet", "not_entitled", None])
@pytest.mark.parametrize("intervening", ["other_key", "same_key", "removed"])
def test_refresh_rechecks_disk_identity_inside_installer(app, monkeypatch, status, intervening):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    installed_key = KEY if intervening == "same_key" else "OTHER-KEY"
    replacement = app.sign(**app.fields, key=installed_key, issued_at=iso(ago(hours=2)),
                           expires_at=iso(ahead(days=40))).decode()
    original_install = routes_license.install_portal_license

    def concurrent_change(*args, **kwargs):
        # After the route's candidate checks, immediately before installation.
        if intervening == "removed":
            app.lm.quarantine_installed_license()
        else:
            app.lm.install_portal_license(replacement)
        return original_install(*args, **kwargs)

    monkeypatch.setattr(routes_license, "install_portal_license", concurrent_change)
    candidate = app.make(ago(hours=1), ago(minutes=30) if status == "not_entitled" else ahead(days=60))
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=candidate, **({"refresh_status": status} if status else {}))
    assert exc.value.status_code in (400, 409)
    if intervening == "removed":
        assert not app.path.exists()
    else:
        assert app.path.read_text() == replacement
    app.routes.log_audit.assert_not_called()
    assert last_refresh(app) is None
    app.restart()
    assert app.lm.get_license().key == ("" if intervening == "removed" else installed_key)


@pytest.mark.parametrize("status", ["not_renewed_yet", "canceled"])
@pytest.mark.parametrize("same_key", [False, True])
def test_no_file_answer_does_not_attach_to_a_concurrent_replacement(app, monkeypatch, status, same_key):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    replacement = app.sign(**app.fields, key=KEY if same_key else "OTHER-KEY",
                           issued_at=iso(ago(hours=2)), expires_at=iso(ahead(days=40))).decode()
    request = routes_license.pinned_async_request

    async def concurrent_upload(*args, **kwargs):
        app.lm.install_portal_license(replacement)
        return await request(*args, **kwargs)

    monkeypatch.setattr(routes_license, "pinned_async_request", concurrent_upload)
    with pytest.raises(HTTPException) as exc:
        app.refresh(refresh_status=status, ends_at=iso(ahead(days=2)))
    assert exc.value.status_code == 409
    assert app.path.read_text() == replacement
    app.routes.log_audit.assert_not_called()
    assert last_refresh(app) is None


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("same_key", [False, True])
def test_unactivation_cannot_remove_an_intervening_replacement(app, monkeypatch, offline, same_key):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    replacement = app.sign(**app.fields, key=KEY if same_key else "OTHER-KEY",
                           issued_at=iso(ago(hours=2)), expires_at=iso(ahead(days=40))).decode()
    if offline:
        sign = app.routes.sign_license_challenge

        def concurrent_upload(*args):
            signature = sign(*args)
            app.lm.install_portal_license(replacement)
            return signature

        monkeypatch.setattr(app.routes, "sign_license_challenge", concurrent_upload)
    else:
        request = app.routes.pinned_async_request

        async def concurrent_upload(*args, **kwargs):
            app.lm.install_portal_license(replacement)
            return await request(*args, **kwargs)

        monkeypatch.setattr(app.routes, "pinned_async_request", concurrent_upload)

    with pytest.raises(HTTPException) as exc:
        if offline:
            app.routes.get_unactivation_request(
                app.routes.OfflineUnactivationRequestBody(nonce="test-nonce"),
                current_user={"id": 1}, db=app.db, license_mutation_allowed=None)
        else:
            asyncio.run(app.routes.unactivate_license(
                current_user={"id": 1}, db=app.db, license_mutation_allowed=None))
    assert exc.value.status_code == 409
    assert app.path.read_text() == replacement
    app.routes.log_audit.assert_not_called()
    app.restart()
    assert app.lm.get_license().valid


def _lose_license_file(app, monkeypatch):
    # The fixture's finder always names the path; the real one reports a missing file.
    monkeypatch.setattr(app.lm, "_find_license_file", lambda: str(app.path) if app.path.exists() else None)
    app.path.unlink()
    app.restart()
    assert not app.path.exists() and app.lm.get_license().license_sha256 == ""


def _unactivate_with_key(app, key=KEY):
    return asyncio.run(app.routes.unactivate_license(
        app.routes.LicenseUnactivateRequest(key=key),
        current_user={"id": 1}, db=app.db, license_mutation_allowed=None))


def test_unactivation_with_a_key_succeeds_when_no_license_file_is_installed(app, monkeypatch):
    # The local file was lost or removed; the admin frees the grant by typing the key.
    audit = []
    monkeypatch.setattr("core.audit.log_audit", lambda *args, **kwargs: audit.append(args[1]))
    _lose_license_file(app, monkeypatch)
    assert _unactivate_with_key(app) == {"status": "unactivated", "tier": "community"}
    assert [body["key"] for url, body in app.calls if url.endswith("/api/v1/unactivate")] == [KEY]
    assert audit == ["license.unactivated"]
    assert not app.path.exists()
    assert not list(app.path.parent.glob(app.path.name + ".unactivated-*"))
    app.restart()
    current = app.lm.get_license()
    assert not current.valid and current.key == "" and current.tier == "community"


@pytest.mark.parametrize("same_key", [False, True])
def test_unactivation_with_a_key_keeps_a_license_installed_during_the_portal_call(app, monkeypatch, same_key):
    # Nothing installed when the request started; an upload lands while the portal answers.
    appeared = app.sign(**app.fields, key=KEY if same_key else "OTHER-KEY",
                        issued_at=iso(ago(hours=2)), expires_at=iso(ahead(days=40))).decode()
    request = app.routes.pinned_async_request

    async def concurrent_upload(*args, **kwargs):
        app.lm.install_portal_license(appeared)
        return await request(*args, **kwargs)

    monkeypatch.setattr(app.routes, "pinned_async_request", concurrent_upload)
    _lose_license_file(app, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _unactivate_with_key(app)
    assert exc.value.status_code == 409
    assert app.path.read_text() == appeared
    assert not list(app.path.parent.glob(app.path.name + ".unactivated-*"))
    app.routes.log_audit.assert_not_called()
    app.restart()
    assert app.lm.get_license().valid


@pytest.mark.parametrize("phase", ["ordering", "rollback", "removal_rollback"])
def test_concurrent_installs_serialize_checks_and_rollback(app, monkeypatch, phase):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    first = app.make(ago(hours=2), ahead(days=20))
    newest = app.make(ago(hours=1), ahead(days=40))
    paused, release, second_started, second_done = (Event() for _ in range(4))
    original = app.lm._refuse_replacement_out_of_order

    def pause():
        paused.set()
        assert release.wait(5), "test did not release the first installer"

    def check(candidate, installed):
        original(candidate, installed)
        if candidate.issued_at == iso(ago(hours=2)):
            pause()

    def fail_audit(*_args, **_kwargs):
        pause()
        raise RuntimeError("synthetic audit failure")

    if phase == "ordering":
        monkeypatch.setattr(app.lm, "_refuse_replacement_out_of_order", check)
    elif phase == "removal_rollback":
        from core import license_lock
        monkeypatch.setattr(license_lock, "enabled", lambda: False)
        monkeypatch.setattr("core.audit.log_audit", fail_audit)

    def install_second():
        second_started.set()
        try:
            return app.lm.install_portal_license(newest)
        finally:
            second_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        if phase == "removal_rollback":
            one = pool.submit(license_lock.remove_installed_license, app.db, 1, "license.removed")
        else:
            one = pool.submit(app.lm.install_portal_license, first,
                              after_install=fail_audit if phase == "rollback" else None)
        try:
            assert paused.wait(5)
            two = pool.submit(install_second)
            assert second_started.wait(5)
            overtook = second_done.wait(0.2)
        finally:
            release.set()
        if phase != "ordering":
            error = license_lock.LicenseLockError if phase == "removal_rollback" else HTTPException
            with pytest.raises(error) as exc:
                one.result(timeout=5)
            assert exc.value.status_code == 503
        else:
            one.result(timeout=5)
        assert two.result(timeout=5).valid
    assert app.path.read_text() == newest
    assert not overtook, "second installer bypassed the first install's check/write/recovery"


def _install_from_another_worker(directory, public_key, content, connection):
    """Spawned process: no inherited Python lock or test monkeypatches."""
    import license_manager as lm
    from core import db
    lm.LICENSE_DIR = directory
    lm.ODIN_PUBLIC_KEY = public_key
    db.current_engine = lambda: None  # This worker uses only its temporary clock file.
    connection.send("started")
    lm.install_portal_license(content)
    connection.send("saved")
    connection.close()


def test_installation_lock_also_serializes_other_processes(app):
    import multiprocessing

    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    first = app.make(ago(hours=2), ahead(days=20))
    newest = app.make(ago(hours=1), ahead(days=40))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    writer = context.Process(target=_install_from_another_worker,
                             args=(str(app.path.parent), app.lm.ODIN_PUBLIC_KEY, newest, child))
    overtook = []

    def audit(_candidate):
        writer.start()
        assert parent.poll(10), "second worker did not start"
        assert parent.recv() == "started"
        overtook.append(parent.poll(0.2))

    try:
        app.lm.install_portal_license(first, after_install=audit)
        assert parent.poll(10), "second worker did not finish after the installer released its lock"
        assert parent.recv() == "saved"
        writer.join(10)
        assert writer.exitcode == 0
        assert app.path.read_text() == newest
        assert overtook == [False]
    finally:
        if writer.is_alive():
            writer.terminate()
            writer.join(5)
        parent.close()
        child.close()


# ---- earlier expiry, newer issued_at ----

def test_earlier_expiry_with_newer_issued_at_is_installed(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=20)))
    replacement = app.make(ago(hours=1), ahead(days=3))
    result = app.refresh(license_file=replacement, refresh_status="canceled")
    assert app.path.read_text() == replacement
    assert result["refresh_status"] == "canceled" and result["expires_at"] == iso(ahead(days=3))
    info = app.lm.get_license()
    assert info.valid and info.expires_at == iso(ahead(days=3))


@pytest.mark.parametrize("status", ["renewed", "canceled", "not_renewed_yet", "not_entitled", None])
@pytest.mark.parametrize("key", ["OTHER-KEY", ""])
@pytest.mark.parametrize("bound", [True, False])
def test_every_refresh_status_requires_the_installed_key(app, status, key, bound):
    fields = app.fields if bound else {}
    installed = app.sign(**fields, key=KEY, issued_at=iso(ago(days=30)),
                         expires_at=iso(ahead(days=5))).decode()
    app.lm.save_license_file(installed)
    app.refresh(refresh_status="not_renewed_yet")
    previous_refresh = last_refresh(app)
    app.routes.log_audit.reset_mock()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    candidate = app.sign(**fields, key=key, issued_at=iso(now - timedelta(seconds=1)),
                         expires_at=iso(now + timedelta(minutes=5))).decode()
    assert app.lm.verify_license_text(candidate).verification_state == "verified_current"
    body = {"license_file": candidate}
    if status is not None:
        body["refresh_status"] = status
    with pytest.raises(HTTPException) as exc:
        app.refresh(**body)
    assert exc.value.status_code == 400
    assert app.path.read_text() == installed
    app.routes.log_audit.assert_not_called()
    assert last_refresh(app) == previous_refresh
    for restart in (False, True):
        if restart:
            app.restart()
        info = app.lm.get_license()
        assert info.valid and info.key == KEY and info.expires_at == iso(ahead(days=5))
        assert last_refresh(app) == previous_refresh


@pytest.mark.parametrize("expires", [ahead(days=60), ahead(days=3), ago(days=1)])
def test_older_issued_at_is_refused_whatever_the_expiry(app, expires):
    installed = app.make(ago(hours=1), ahead(days=20))
    app.lm.save_license_file(installed)
    older = app.make(ago(days=2), expires)
    # An ended file is only ever sent with not_entitled; any other answer with it is refused first (502).
    status = "not_entitled" if expires < NOW else "renewed"
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=older, refresh_status=status)
    assert exc.value.status_code == 409
    assert app.path.read_text() == installed
    assert last_refresh(app) is None


def test_uploading_an_older_file_is_refused(app):
    # Behaviour change in B3: the ordering rule applies to every install path.
    installed = app.make(ago(hours=1), ahead(days=20))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.upload(app.make(ago(days=2), ahead(days=90)).encode())
    assert exc.value.status_code == 409
    assert app.path.read_text() == installed


def test_ordering_rule_edges(app):
    lm = app.lm
    installed = app.make(ago(hours=1), ahead(days=20))
    lm.save_license_file(installed)
    # The same issue time is accepted (re-installing the same file).
    assert lm.install_portal_license(installed).valid
    # A file with no issue time cannot be shown to be newer than an installed one that has it.
    with pytest.raises(HTTPException) as exc:
        lm.install_portal_license(app.sign(**app.fields, key=KEY, expires_at=iso(ahead(days=90))).decode())
    assert exc.value.status_code == 409
    # Another key is another license: issue times are not compared.
    other = app.sign(**app.fields, key="OTHER-KEY", issued_at=iso(ago(days=400)), expires_at=iso(ahead(days=90))).decode()
    assert lm.install_portal_license(other).valid


@pytest.mark.parametrize("path", ["upload", "refresh"])
@pytest.mark.parametrize("ended", [False, True])
def test_different_file_with_equal_issue_time_cannot_restore_entitlement(app, path, ended):
    issued = ago(hours=2)
    installed = app.make(issued, ago(hours=1) if ended else ahead(days=3))
    app.lm.save_license_file(installed)
    longer = app.make(issued, ahead(days=90))
    with pytest.raises(HTTPException) as exc:
        if path == "upload":
            app.upload(longer.encode())
        else:
            app.refresh(license_file=longer, refresh_status="renewed")
    assert exc.value.status_code == 409
    assert app.path.read_text() == installed
    assert last_refresh(app) is None
    app.restart()
    assert app.lm.get_license().expires_at == iso(ago(hours=1) if ended else ahead(days=3))


@pytest.mark.parametrize("path", ["upload", "refresh"])
def test_plan_change_with_equal_issue_time_is_installed(app, path):
    """The license server re-signs a key that has no subscription with the key's stored
    issue time (odin-site-cf api/_lib/billing/key-entitlement.js `unmanaged`), so an
    upgrade arrives as a different file with the same issue time and the same end."""
    issued, end = ago(days=30), ahead(days=300)
    app.lm.save_license_file(app.make(issued, end, tier="pro"))
    assert app.lm.get_license().tier == "pro"
    upgraded = app.make(issued, end, tier="enterprise", max_printers=40)
    if path == "upload":
        app.upload(upgraded.encode())
    else:
        result = app.refresh(license_file=upgraded, refresh_status="renewed")
        assert result["valid"] is True and result["tier"] == "enterprise"
    assert app.path.read_text() == upgraded
    app.restart()
    info = app.lm.get_license()
    assert info.valid and info.tier == "enterprise" and info.max_printers == 40


def test_equal_issue_time_file_that_ends_earlier_is_installed(app):
    issued = ago(days=30)
    app.lm.save_license_file(app.make(issued, ahead(days=300)))
    shorter = app.make(issued, ahead(days=10))
    assert app.lm.install_portal_license(shorter).valid
    assert app.path.read_text() == shorter


def test_identical_file_is_accepted_and_changes_nothing(app):
    installed = app.make(ago(days=30), ahead(days=300), tier="pro")
    app.lm.save_license_file(installed)
    result = app.refresh(license_file=installed, refresh_status="renewed")
    assert result["valid"] is True and result["tier"] == "pro"
    assert app.path.read_text() == installed


@pytest.mark.parametrize("path", ["upload", "refresh", "install"])
@pytest.mark.parametrize("same_instant", [False, True])
def test_pre_refund_file_cannot_be_replayed_after_the_refund_replacement(app, path, same_instant):
    """After a refund the installed file is the ended replacement. The file held before
    the refund is refused when it is older, and also if it carried the very same issue
    time as the replacement: an equal issue time never installs a file that ends later."""
    refunded_at = ago(hours=2)
    before_refund = app.make(refunded_at if same_instant else ago(days=20), ahead(days=10), tier="enterprise")
    if not same_instant:
        app.lm.save_license_file(before_refund)
    else:
        app.lm.save_license_file(app.make(ago(days=20), ahead(days=10)))
    replacement = app.make(refunded_at, refunded_at + timedelta(seconds=1))
    assert app.refresh(license_file=replacement, refresh_status="not_entitled")["valid"] is False
    assert app.path.read_text() == replacement
    with pytest.raises(HTTPException) as exc:
        if path == "upload":
            app.upload(before_refund.encode())
        elif path == "refresh":
            app.refresh(license_file=before_refund, refresh_status="renewed")
        else:
            app.lm.install_portal_license(before_refund, allow_expired=True, expected_key=KEY)
    assert exc.value.status_code == 409
    assert app.path.read_text() == replacement
    app.restart()
    info = app.lm.get_license()
    assert not info.valid and info.max_printers == app.lm.TIERS["community"]["max_printers"]


# ---- already-expired replacement ----

def test_expired_replacement_is_installed_and_persists_across_restart(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    ended = app.make(ago(hours=2), ago(hours=1))
    result = app.refresh(license_file=ended, refresh_status="not_entitled")
    assert app.path.read_text() == ended
    assert result["refresh_status"] == "not_entitled" and result["valid"] is False and result["tier"] == "community"
    for _ in range(2):
        app.restart()
        info = app.lm.get_license()
        assert not info.valid and info.expired and info.verification_state == "verified_expired"
        assert info.max_printers == app.lm.TIERS["community"]["max_printers"]
        assert last_refresh(app)["status"] == "not_entitled"
    assert app.path.read_text() == ended


def test_refund_then_reversal_round_trip(app):
    lm = app.lm
    lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    app.refresh(license_file=app.make(ago(hours=3), ago(hours=2)), refresh_status="not_entitled")
    app.restart()
    assert not lm.get_license().valid
    result = app.refresh(license_file=app.make(ago(hours=1), ahead(days=30)), refresh_status="renewed")
    assert result["valid"] is True and result["tier"] == "education"
    app.restart()
    assert lm.get_license().valid


def test_expired_file_is_not_installed_outside_the_refresh_path(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    ended = app.make(ago(hours=2), ago(hours=1))
    with pytest.raises(HTTPException) as exc:
        app.lm.install_portal_license(ended)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        app.upload(ended.encode())
    assert exc.value.status_code == 400
    assert app.path.read_text() == installed


def test_expired_replacement_must_be_newer_and_for_the_same_key(app):
    installed = app.make(ago(hours=1), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:  # issued before the installed file
        app.lm.install_portal_license(app.make(ago(days=3), ago(days=2)), allow_expired=True)
    assert exc.value.status_code == 409
    other_key = app.sign(**app.fields, key="OTHER-KEY", issued_at=iso(ago(hours=3)), expires_at=iso(ago(hours=2))).decode()
    with pytest.raises(HTTPException) as exc:
        app.lm.install_portal_license(other_key, allow_expired=True)
    assert exc.value.status_code == 400
    assert app.path.read_text() == installed


def test_expired_replacement_needs_an_installed_license(app):
    app.path.unlink(missing_ok=True)
    with pytest.raises(HTTPException) as exc:
        app.lm.install_portal_license(app.make(ago(hours=2), ago(hours=1)), allow_expired=True)
    assert exc.value.status_code == 400
    assert not app.path.exists()


def test_expires_at_equal_to_issued_at_is_not_accepted(app):
    # Contract for the license server: a not_entitled file must end strictly after
    # its issued_at, otherwise it is not "verified_expired" (and the fleet lock
    # would keep the last-good entitlement instead of dropping to Community).
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.lm.install_portal_license(app.make(ago(hours=1), ago(hours=1)), allow_expired=True)
    assert exc.value.status_code == 400
    assert app.path.read_text() == installed


# ---- refresh_status passthrough and last_refresh ----

@pytest.mark.parametrize("status", ["not_renewed_yet", "canceled"])
def test_status_without_a_file_changes_nothing_and_is_stored(app, status):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    assert last_refresh(app) is None
    result = app.refresh(refresh_status=status, expires_at=iso(ahead(days=5)))
    assert app.path.read_text() == installed
    assert result["refresh_status"] == status and result["valid"] is True
    assert result["expires_at"] == iso(ahead(days=5))
    stored = last_refresh(app)
    assert stored == result["last_refresh"]
    assert stored["status"] == status
    assert stored["expires_at"] == iso(ahead(days=5))
    app.restart()
    assert last_refresh(app)["status"] == status


def test_an_unreadable_end_date_from_the_portal_is_ignored(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    result = app.refresh(refresh_status="canceled", expires_at="not a date")
    assert result["expires_at"] == iso(ahead(days=5))


def test_portal_without_refresh_status_is_a_renewal(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    result = app.refresh(license_file=app.make(ago(hours=1), ahead(days=35)))
    assert result["status"] == "reactivated" and result["refresh_status"] == "renewed"
    assert last_refresh(app)["status"] == "renewed"


def test_unknown_refresh_status_is_refused_and_changes_nothing(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=app.make(ago(hours=1), ahead(days=35)), refresh_status="paused")
    assert exc.value.status_code == 502
    assert app.path.read_text() == installed
    assert last_refresh(app) is None


@pytest.mark.parametrize("status", ["renewed", "not_entitled"])
def test_status_that_needs_a_file_is_refused_without_one(app, status):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.refresh(refresh_status=status)
    assert exc.value.status_code == 502 and exc.value.detail == "License server returned no license file"
    assert app.path.read_text() == installed


def test_last_refresh_is_hidden_once_another_license_file_is_installed(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    app.refresh(refresh_status="canceled", expires_at=iso(ahead(days=5)))
    assert last_refresh(app) is not None
    app.lm.save_license_file(app.make(ago(hours=1), ahead(days=40)))
    assert last_refresh(app) is None


def test_admin_refresh_answer_identifies_the_file_it_describes(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    app.refresh(refresh_status="canceled")
    result = routes_license.get_license_last_refresh(current_user={"id": 1})
    assert result["license_sha256"] == app.lm.load_license().license_sha256
    assert result["last_refresh"]["status"] == "canceled"
    app.lm.save_license_file(app.make(ago(hours=1), ahead(days=40)))
    changed = routes_license.get_license_last_refresh(current_user={"id": 1})
    assert changed["license_sha256"] == app.lm.load_license().license_sha256
    assert changed["license_sha256"] != result["license_sha256"]
    assert changed["last_refresh"] is None


# ---- review round 1: the answer and the file must agree; earlier stated end ----

def test_renewed_with_an_ended_file_is_refused_and_changes_nothing(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=app.make(ago(hours=2), ago(hours=1)), refresh_status="renewed")
    assert exc.value.status_code == 502
    assert app.path.read_text() == installed
    assert last_refresh(app) is None


def test_not_entitled_with_a_valid_file_is_refused_and_changes_nothing(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=app.make(ago(hours=1), ahead(days=35)), refresh_status="not_entitled")
    assert exc.value.status_code == 502
    assert app.path.read_text() == installed
    assert last_refresh(app) is None


def test_a_later_portal_end_date_never_outlasts_the_installed_file(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    result = app.refresh(refresh_status="canceled", expires_at=iso(ahead(days=40)))
    assert result["expires_at"] == iso(ahead(days=5))
    earlier = app.refresh(refresh_status="canceled", expires_at=iso(ahead(days=2)))
    assert earlier["expires_at"] == iso(ahead(days=2))


def test_a_candidate_with_an_unreadable_issue_time_is_refused(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    candidate = app.sign(**app.fields, key=KEY, issued_at="not-a-time", expires_at=iso(ahead(days=35))).decode()
    with pytest.raises(HTTPException):
        app.lm.install_portal_license(candidate)
    assert app.path.read_text() == installed


def test_not_entitled_file_ending_within_the_clock_allowance_is_installed(app):
    """This server's clock runs behind the portal's: the 'ended' file still has seconds to go."""
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    now = datetime.now(timezone.utc).replace(microsecond=0)  # fresh, not NOW: a long suite must not age this file past its end
    ending = app.make(now - timedelta(seconds=1), now + timedelta(minutes=5))
    result = app.refresh(license_file=ending, refresh_status="not_entitled")
    assert result["refresh_status"] == "not_entitled"
    assert app.path.read_text() == ending
    assert last_refresh(app)["status"] == "not_entitled"


@pytest.mark.parametrize("issue_offset", [0, 1])
def test_clock_allowance_refuses_unverified_timestamp_order(app, issue_offset):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
    candidate = app.make(end + timedelta(seconds=issue_offset), end)
    info = app.lm.verify_license_text(candidate)
    # Compatibility validity alone is insufficient evidence for a replacement.
    assert info.valid and info.verification_state == "verification_unavailable"
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=candidate, refresh_status="not_entitled")
    assert exc.value.status_code == 502
    assert app.path.read_text() == installed
    assert last_refresh(app) is None
    app.restart()
    assert app.lm.get_license().valid


@pytest.mark.parametrize("key", ["OTHER-KEY", ""])
def test_clock_allowance_replacement_requires_the_installed_key(app, key):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    candidate = app.sign(**app.fields, key=key, issued_at=iso(now - timedelta(seconds=1)),
                         expires_at=iso(now + timedelta(minutes=5))).decode()
    assert app.lm.verify_license_text(candidate).verification_state == "verified_current"
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=candidate, refresh_status="not_entitled")
    assert exc.value.status_code == 400
    assert app.path.read_text() == installed
    assert last_refresh(app) is None
    app.restart()
    assert app.lm.get_license().valid and app.lm.get_license().key == KEY


def test_not_entitled_file_ending_after_the_allowance_is_refused(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=app.make(ago(seconds=1), ahead(hours=2)), refresh_status="not_entitled")
    assert exc.value.status_code == 502
    assert app.path.read_text() == installed


def test_an_unreadable_file_from_the_portal_says_so(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    with pytest.raises(HTTPException) as exc:
        app.refresh(license_file=12345, refresh_status="not_entitled")
    assert exc.value.status_code == 502 and "could not be read" in exc.value.detail
    assert app.path.read_text() == installed


# ---- the license server's wire (odin-site-cf api/v1/reactivate.js) ----

def test_canceled_reads_the_portals_ends_at(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=5)))
    result = app.refresh(refresh_status="canceled", ends_at=iso(ahead(days=2)), license_file=None)
    assert result["expires_at"] == iso(ahead(days=2))
    assert last_refresh(app)["expires_at"] == iso(ahead(days=2))


def test_reactivate_sends_the_held_expiry(app):
    installed = app.make(ago(days=30), ahead(days=5))
    app.lm.save_license_file(installed)
    app.refresh(refresh_status="not_renewed_yet", license_file=None)
    url, body = next((url, body) for url, body in reversed(app.calls) if url.endswith("/api/v1/reactivate"))
    assert body["current_expires_at"] == iso(ahead(days=5))


def test_clock_allowance_uses_the_trusted_clock_not_the_wall_clock(app, monkeypatch):
    """The trusted license clock decides: a file that ends in 10 minutes on it is within the
    allowance even if the wall clock says hours remain, and one that ends 2 h after it is not."""
    from types import SimpleNamespace as NS
    trusted_now = datetime.now(timezone.utc) + timedelta(hours=3)  # trusted clock ahead of the wall clock
    monkeypatch.setattr(app.lm, "license_clock", lambda: NS(sample=lambda *a, **k: (trusted_now, False)))
    near = SimpleNamespace(valid=True, trial=False, verification_state="verified_current",
                           issued_at=iso(ago(hours=1)), expires_at=iso(trusted_now + timedelta(minutes=10)))
    far = SimpleNamespace(valid=True, trial=False, verification_state="verified_current",
                          issued_at=iso(ago(hours=1)), expires_at=iso(trusted_now + timedelta(hours=2)))
    assert license_refresh.ends_within_clock_allowance(near) is True
    assert license_refresh.ends_within_clock_allowance(far) is False


def test_clock_allowance_refuses_when_the_clock_fails(app, monkeypatch):
    def broken():
        raise RuntimeError("clock state unreadable")
    monkeypatch.setattr(app.lm, "license_clock", broken)
    ending = SimpleNamespace(valid=True, trial=False, verification_state="verified_current",
                             issued_at=iso(ago(hours=1)), expires_at=iso(ahead(seconds=30)))
    assert license_refresh.ends_within_clock_allowance(ending) is False


def test_a_license_in_force_from_a_fallback_location_can_still_be_refreshed(app, monkeypatch, tmp_path):
    """Fourth sign-off: the installer judged identity against LICENSE_DIR/odin.license only, so
    a license loaded from a supported fallback location made every refresh fail with 400."""
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=20)))
    fallback = tmp_path / "elsewhere" / "odin.license"
    fallback.parent.mkdir()
    fallback.write_text(app.path.read_text())
    app.path.unlink()                                  # the license in force is only at the fallback
    real = app.lm._find_license_file
    monkeypatch.setattr(app.lm, "_find_license_file", lambda: str(app.path) if app.path.exists() else str(fallback))
    app.restart()
    assert app.lm.get_license().valid
    replacement = app.make(ago(hours=1), ahead(days=40))
    result = app.refresh(license_file=replacement, refresh_status="renewed")
    assert app.path.read_text() == replacement and result["expires_at"] == iso(ahead(days=40))
    # ...and the same rules still hold against it: an older file for that key is refused.
    monkeypatch.setattr(app.lm, "_find_license_file", real)
    with pytest.raises(HTTPException) as refused:
        app.refresh(license_file=app.make(ago(hours=5), ahead(days=90)), refresh_status="renewed")
    assert refused.value.status_code == 409 and app.path.read_text() == replacement


@pytest.mark.parametrize("status", ["renewed", "not_entitled"])
def test_a_refresh_is_refused_when_the_license_path_is_set_by_configuration(app, monkeypatch, tmp_path, status):
    """Fifth sign-off: with ODIN_LICENSE_FILE outside the license folder the replacement was
    saved where the loader never reads, and the refresh still reported success."""
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=20)))
    pinned = tmp_path / "pinned" / "odin.license"
    pinned.parent.mkdir()
    pinned.write_text(app.path.read_text())
    before = app.path.read_text()
    monkeypatch.setenv("ODIN_LICENSE_FILE", str(pinned))
    app.restart()
    ends = ago(minutes=1) if status == "not_entitled" else ahead(days=40)
    with pytest.raises(HTTPException) as refused:
        app.refresh(license_file=app.make(ago(hours=1), ends), refresh_status=status)
    assert refused.value.status_code == 409 and "ODIN_LICENSE_FILE" in refused.value.detail
    assert pinned.read_text() == before and app.path.read_text() == before
    assert app.lm.get_license().valid


@pytest.mark.parametrize("action", ["renewed", "not_entitled", "upload"])
def test_a_license_folder_link_to_the_configured_file_is_refused_not_replaced(app, monkeypatch, tmp_path, action):
    """Review finding: LICENSE_DIR/odin.license was a link to the file ODIN_LICENSE_FILE selects.
    The paths compared equal, the save replaced the link and not that file, and the loader kept
    reading the old license while the refresh (for example after a refund) reported success."""
    import os
    before = app.make(ago(days=30), ahead(days=20))
    external = tmp_path / "managed-elsewhere" / "customer.license"
    external.parent.mkdir()
    external.write_text(before)
    if app.path.exists():
        app.path.unlink()
    app.path.symlink_to(external)
    monkeypatch.setenv("ODIN_LICENSE_FILE", str(external))
    monkeypatch.setattr(app.lm, "_find_license_file", REAL_FIND_LICENSE_FILE)  # the real loader
    app.restart()
    in_force = app.lm.get_license()
    assert in_force.valid and in_force.key == KEY and in_force.expires_at == iso(ahead(days=20))

    def folder():
        return sorted(name for name in os.listdir(app.path.parent) if name != ".odin-license.lock")

    listed = folder()
    app.routes.log_audit.reset_mock()
    ends = ago(minutes=1) if action == "not_entitled" else ahead(days=40)
    candidate = app.make(ago(hours=1), ends)
    with pytest.raises(HTTPException) as refused:  # No answer is returned, so nothing claims success.
        if action == "upload":
            app.upload(candidate.encode())
        else:
            app.refresh(license_file=candidate, refresh_status=action)
    assert refused.value.status_code == 409
    assert refused.value.detail == app.lm.LICENSE_MANAGED_OUTSIDE
    assert "managed outside" in refused.value.detail and "ODIN_LICENSE_FILE" in refused.value.detail
    # Nothing was written: the link is still that link, the file it points to is untouched,
    # and no staging or recovery file was left in the license folder.
    assert app.path.is_symlink() and os.readlink(app.path) == str(external)
    assert external.read_text() == before and folder() == listed
    app.routes.log_audit.assert_not_called()
    assert last_refresh(app) is None
    # The loader still reads what it read before, in this process and after a restart.
    for _ in range(2):
        now_in_force = app.lm.get_license()
        assert now_in_force.valid and now_in_force.license_sha256 == in_force.license_sha256
        assert app.lm.load_license().license_sha256 == in_force.license_sha256
        app.restart()


def test_a_configured_file_that_is_the_license_folder_file_is_still_replaced(app, monkeypatch):
    """The ordinary layouts are unchanged: ODIN_LICENSE_FILE naming the license folder's own
    file (no link) installs as before, and the loader reads the new file."""
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=20)))
    monkeypatch.setenv("ODIN_LICENSE_FILE", str(app.path))
    monkeypatch.setattr(app.lm, "_find_license_file", REAL_FIND_LICENSE_FILE)
    app.restart()
    replacement = app.make(ago(hours=1), ahead(days=40))
    result = app.refresh(license_file=replacement, refresh_status="renewed")
    assert result["status"] == "reactivated" and result["expires_at"] == iso(ahead(days=40))
    assert not app.path.is_symlink() and app.path.read_text() == replacement
    app.restart()
    assert app.lm.get_license().expires_at == iso(ahead(days=40))
