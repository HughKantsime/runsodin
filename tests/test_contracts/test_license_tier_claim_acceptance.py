"""Actual signed tier entitlements, not feature-workflow certification."""
import pytest
from fastapi import HTTPException
from tests.test_contracts.test_license_user_limits import signed_license


def test_community_limits_are_five_printers_one_user(signed_license):
    lm,info=signed_license(tier='community',features=[])
    assert info.valid and info.max_printers==5 and info.max_users==1
    lm.check_printer_limit(4);lm.check_user_limit(0)
    with pytest.raises(HTTPException):lm.check_printer_limit(5)
    with pytest.raises(HTTPException):lm.check_user_limit(1)


def test_pro_advertised_unlimited_users_does_not_reject_at_ten_thousand(signed_license):
    lm,info=signed_license(tier='pro',features=[])
    assert info.valid and info.has_feature('usage_reports')
    lm.check_user_limit(10000)

@pytest.mark.parametrize('tier,required',[
    ('education',{'job_approval','user_groups','print_quotas'}),
    ('enterprise',{'multi_user','rbac','orders','products','audit_export','sqlcipher'}),
])
def test_signed_tier_effective_features(signed_license,tier,required):
    _,info=signed_license(tier=tier,features=[])
    assert info.valid
    assert required<=set(info.to_public_dict()['features'])


def test_classroom_workflow_requires_explicit_signed_entitlement(signed_license):
    _,without=signed_license(tier='education',features=[])
    assert without.has_feature('education_workflows') is False
    _,with_feature=signed_license(tier='education',features=['education_workflows'])
    assert with_feature.has_feature('education_workflows') is True
