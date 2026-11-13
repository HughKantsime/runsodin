"""ITAR mode covers the Square clients and the built-in sign-in presets.

Spec: .claude/specs/itar-coverage-and-doc-refresh.md (A and B). Found 2026-11-13: the Square
clients used requests directly with no ITAR check, and the boot audit looked only at a stored
discovery address, so an enabled Microsoft or Google preset passed boot.
"""
import socket

import pytest
from fastapi import HTTPException

from core import itar
from modules.orders import square_client, square_delivery_client
from modules.organizations.oidc_handler import OIDCHandler, effective_discovery_url


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_square_reads_are_refused_under_itar_before_any_network_use(monkeypatch, no_network):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    reader = square_client.SquareReader("production", "token")
    with pytest.raises(HTTPException) as refused:
        reader._read("GET", "/v2/merchants/me")
    assert refused.value.status_code == 409
    assert refused.value.detail == square_client.ITAR_REFUSAL


def test_square_stock_calls_are_refused_under_itar_before_any_network_use(monkeypatch, no_network):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(HTTPException) as refused:
        square_delivery_client.exchange("production", "token", "/v2/inventory/changes/batch-retrieve", "{}")
    assert refused.value.status_code == 409
    assert refused.value.detail == square_client.ITAR_REFUSAL


def test_an_unsupported_square_operation_is_still_refused_first(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(HTTPException) as refused:
        square_client.SquareReader("production", "token")._read("DELETE", "/v2/merchants/me")
    assert refused.value.status_code == 400


def test_without_itar_the_square_guard_does_nothing(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    square_client.refuse_under_itar()


@pytest.mark.parametrize("stored, provider, tenant, expected", [
    ("https://idp.internal/.well-known/openid-configuration", "generic", "", "https://idp.internal/.well-known/openid-configuration"),
    (None, "google", "", "https://accounts.google.com/.well-known/openid-configuration"),
    (None, "microsoft", "abc", "https://login.microsoftonline.com/abc/v2.0/.well-known/openid-configuration"),
    ("", "microsoft", "abc", "https://login.microsoftonline.com/abc/v2.0/.well-known/openid-configuration"),
])
def test_the_audited_address_is_the_one_the_handler_uses(stored, provider, tenant, expected):
    assert effective_discovery_url(stored, provider, tenant) == expected
    handler = OIDCHandler(client_id="c", client_secret="s", tenant_id=tenant, redirect_uri="https://odin.internal/cb",
                          discovery_url=stored, provider_type=provider)
    assert handler.discovery_url == expected


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Db:
    """Answers the boot audit's queries from a small table of fixtures."""
    def __init__(self, oidc=(), square=False):
        self.oidc, self.square = list(oidc), square

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM oidc_config" in sql:
            return _Rows(self.oidc)
        if "square_readonly:" in sql:
            return _Rows([(1,)] if self.square else [])
        return _Rows([])

    def close(self):
        pass


def _collect(monkeypatch, db):
    import core.db as core_db
    monkeypatch.setattr(core_db, "SessionLocal", lambda: db)
    return itar.collect_db_configured_urls()


def test_boot_audit_reports_a_stored_square_connection(monkeypatch):
    assert "https://connect.squareup.com" in _collect(monkeypatch, _Db(square=True))
    assert "https://connect.squareup.com" not in _collect(monkeypatch, _Db())


def test_boot_audit_reports_an_enabled_microsoft_or_google_preset(monkeypatch):
    urls = _collect(monkeypatch, _Db(oidc=[(None, "microsoft", "abc"), (None, "google", None)]))
    assert "https://login.microsoftonline.com/abc/v2.0/.well-known/openid-configuration" in urls
    assert "https://accounts.google.com/.well-known/openid-configuration" in urls


def test_boot_audit_keeps_the_idp_allowance(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setattr(itar, "allowed_idp_hosts", lambda: frozenset({"login.microsoftonline.us"}))
    stored = "https://login.microsoftonline.us/abc/v2.0/.well-known/openid-configuration"
    assert stored not in _collect(monkeypatch, _Db(oidc=[(stored, "microsoft", "abc")]))
