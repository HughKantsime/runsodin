"""Education quota routes must enforce the print_quotas entitlement."""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import license_manager
from modules.organizations import routes_sessions


class _FailIfTouchedDB:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("database touched before license feature gate")

    def flush(self):
        raise AssertionError("database mutated before license feature gate")

    def commit(self):
        raise AssertionError("database committed before license feature gate")


def _deny_print_quotas(feature: str):
    assert feature == "print_quotas"
    raise HTTPException(status_code=403, detail="Feature 'print_quotas' requires an Education or Enterprise license")


@pytest.mark.parametrize("tier,valid", [("community", False), ("pro", True)])
def test_non_education_tiers_deny_print_quotas(tier, valid):
    license_info = license_manager.LicenseInfo()
    license_info.valid = valid
    license_info.tier = tier
    with patch.object(license_manager, "get_license", return_value=license_info), \
         pytest.raises(HTTPException) as exc_info:
        license_manager.require_feature("print_quotas")
    assert exc_info.value.status_code == 403


def test_my_quota_checks_feature_before_usage_lookup():
    with patch.object(routes_sessions, "require_feature", side_effect=_deny_print_quotas, create=True), \
         patch.object(routes_sessions, "_get_quota_usage") as usage:
        try:
            asyncio.run(routes_sessions.get_my_quota(current_user={"id": 7}, db=_FailIfTouchedDB()))
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("quota route did not enforce print_quotas")
        usage.assert_not_called()


def test_admin_quota_list_checks_feature_before_query():
    with patch.object(routes_sessions, "require_feature", side_effect=_deny_print_quotas, create=True):
        try:
            asyncio.run(routes_sessions.admin_list_quotas(
                current_user={"id": 1, "role": "admin", "group_id": None},
                db=_FailIfTouchedDB(),
            ))
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("admin quota list did not enforce print_quotas")


def test_admin_quota_update_checks_feature_before_mutation():
    body = routes_sessions.QuotaUpdateRequest(quota_jobs=5)
    with patch.object(routes_sessions, "require_feature", side_effect=_deny_print_quotas, create=True):
        try:
            asyncio.run(routes_sessions.admin_set_quota(
                9,
                body,
                current_user={"id": 1, "role": "admin", "group_id": None},
                db=_FailIfTouchedDB(),
            ))
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("quota update did not enforce print_quotas")


@pytest.mark.parametrize("tier", ["education", "enterprise"])
def test_education_tiers_allow_quota_route_access(tier):
    license_info = license_manager.LicenseInfo()
    license_info.valid = True
    license_info.tier = tier
    usage = {
        "grams_used": 0.0,
        "hours_used": 0.0,
        "jobs_used": 0,
        "period_key": "monthly:2026-09",
    }
    current_user = {"id": 7, "role": "viewer", "quota_period": "monthly"}

    with patch.object(license_manager, "get_license", return_value=license_info), \
         patch.object(routes_sessions, "_get_quota_usage", return_value=usage):
        result = asyncio.run(routes_sessions.get_my_quota(
            current_user=current_user,
            db=object(),
        ))

    assert result["period_key"] == "monthly:2026-09"


def test_quota_routes_are_registered_under_both_api_prefixes():
    from fastapi import FastAPI
    from modules.organizations import register

    class _Registry:
        def register_provider(self, *_args, **_kwargs):
            return None

    app = FastAPI()
    register(app, _Registry())
    paths = {route.path for route in app.routes}
    for suffix in ("/quotas", "/admin/quotas", "/admin/quotas/{user_id}"):
        assert f"/api{suffix}" in paths
        assert f"/api/v1{suffix}" in paths
