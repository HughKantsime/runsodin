"""B3: "Refresh license" (reactivate) answers, earlier/expired replacements, last_refresh.

Spec: .claude/specs/odin-checkout-build.md Part B.4. Same ephemeral-signer and
isolated-device fixtures as test_paid_license_binding.py; no live portal.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from modules.system import license_refresh
from tests.test_contracts.test_license_offline_import import owned_import  # noqa: F401  (fixture)
from tests.test_contracts.test_paid_license_binding import device, portal  # noqa: F401  (fixtures)

from core.schema.bootstrap import import_all_models

import_all_models()  # Every mapper registered, so the SystemConfig query works when this file runs alone.

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
        return asyncio.run(routes.reactivate_license(current_user={"id": 1}, db=db, license_mutation_allowed=None))

    return SimpleNamespace(lm=lm, routes=routes, path=path, make=make, refresh=refresh, restart=restart,
                           upload=upload, sign=sign, fields=fields, db=db, calls=calls)


def last_refresh(app):
    """The admin-only answer; the public license answer never carries it."""
    assert "last_refresh" not in app.routes.get_license_info()
    return app.routes.get_license_last_refresh(current_user={"id": 1})["last_refresh"]


# ---- earlier expiry, newer issued_at ----

def test_earlier_expiry_with_newer_issued_at_is_installed(app):
    app.lm.save_license_file(app.make(ago(days=30), ahead(days=20)))
    replacement = app.make(ago(hours=1), ahead(days=3))
    result = app.refresh(license_file=replacement, refresh_status="canceled")
    assert app.path.read_text() == replacement
    assert result["refresh_status"] == "canceled" and result["expires_at"] == iso(ahead(days=3))
    info = app.lm.get_license()
    assert info.valid and info.expires_at == iso(ahead(days=3))


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
