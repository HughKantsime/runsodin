"""Strict MFA policy decoding across PostgreSQL JSON and legacy text."""
import pytest
from fastapi import HTTPException
from core.mfa_login import read_required_mfa


class PolicyDatabase:
    def __init__(self,value):self.value=value
    def execute(self,*args,**kwargs):return self
    def fetchone(self):return (self.value,)


@pytest.mark.parametrize('value,required',[(True,True),(False,False),('true',True),('false',False)])
def test_canonical_json_and_exact_legacy_values(value,required):
    assert read_required_mfa(PolicyDatabase(value)) is required


@pytest.mark.parametrize('value',[None,0,1,'TRUE','False','',{},[]])
def test_malformed_policy_refuses_decision(value):
    with pytest.raises(HTTPException) as error:
        read_required_mfa(PolicyDatabase(value))
    assert error.value.status_code==503


def test_absent_setting_defaults_to_optional():
    class Absent(PolicyDatabase):
        def fetchone(self):return None
    assert read_required_mfa(Absent(None)) is False


def test_database_failure_refuses_policy_decision():
    class Unavailable:
        def execute(self,*args,**kwargs):raise RuntimeError('synthetic database unavailable')
    with pytest.raises(HTTPException) as error:
        read_required_mfa(Unavailable())
    assert error.value.status_code==503
