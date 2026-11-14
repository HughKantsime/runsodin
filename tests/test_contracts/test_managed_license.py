"""Externally managed sandbox licenses reject mutation intentionally."""

import inspect

import pytest
from fastapi import HTTPException

from license_manager import license_is_managed_externally, require_license_mutation_enabled
from modules.system import routes_health


def test_managed_license_flag_is_reported_and_mutation_is_forbidden(monkeypatch):
    monkeypatch.setenv("ODIN_LICENSE_READ_ONLY", "1")
    assert license_is_managed_externally() is True
    with pytest.raises(HTTPException) as exc_info:
        require_license_mutation_enabled()
    assert exc_info.value.status_code == 403
    assert "externally managed" in str(exc_info.value.detail).lower()


def test_normal_license_mode_allows_mutation_dependency(monkeypatch):
    monkeypatch.delenv("ODIN_LICENSE_READ_ONLY", raising=False)
    assert license_is_managed_externally() is False
    assert require_license_mutation_enabled() is None


@pytest.mark.parametrize(
    "endpoint",
    [
        routes_health.upload_license,
        routes_health.remove_license,
        routes_health.activate_license,
        routes_health.unactivate_license,
        routes_health.reactivate_license,
    ],
)
def test_every_license_mutation_route_declares_managed_license_guard(endpoint):
    signature = inspect.signature(endpoint)
    guarded = [
        parameter
        for parameter in signature.parameters.values()
        if "license_mutation" in str(parameter.default)
        or parameter.name == "license_mutation_allowed"
    ]
    assert guarded, f"{endpoint.__name__} is missing the managed-license dependency"
