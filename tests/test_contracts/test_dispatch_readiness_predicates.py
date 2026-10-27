"""Raw protocol observation gate, without transport/freshness claims."""
from copy import deepcopy
import pytest
from modules.printers.dispatch_readiness import raw_status_is_idle


@pytest.mark.parametrize('state', ['IDLE','FINISH','FAILED'])
def test_bambu_explicit_idle_with_zero_error(state):
    assert raw_status_is_idle('bambu', {'gcode_state':state,'print_error':0})


def test_bambu_failed_with_task_canceled_ack_is_idle():
    assert raw_status_is_idle('bambu', {'gcode_state':'FAILED','print_error':0x0300400C})


@pytest.mark.parametrize('state', ['IDLE','FINISH','RUNNING','PAUSE','PREPARE',None])
def test_bambu_task_canceled_ack_only_counts_in_failed(state):
    assert not raw_status_is_idle('bambu', {'gcode_state':state,'print_error':0x0300400C})


@pytest.mark.parametrize('error', [0x05FE8053, 0x0300400B, 0x0300400D, 0x0500400C, 50348044.0, '50348044'])
def test_bambu_failed_with_other_errors_refused(error):
    assert not raw_status_is_idle('bambu', {'gcode_state':'FAILED','print_error':error})


@pytest.mark.parametrize('state', [None,'RUNNING','PAUSE','PREPARE','idle','UNKNOWN'])
def test_bambu_busy_unknown_or_invalid_state_refused(state):
    assert not raw_status_is_idle('bambu', {'gcode_state':state,'print_error':0})


@pytest.mark.parametrize('state', ['IDLE','FINISH','FAILED'])
@pytest.mark.parametrize('error', [None,False,True,'0',0.0,1,-1])
def test_bambu_incomplete_or_invalid_error_refused(state,error):
    assert not raw_status_is_idle('bambu', {'gcode_state':state,'print_error':error})


def moonraker():
    return {'result':{'status':{'webhooks':{'state':'ready'},'print_stats':{'state':'standby'},'virtual_sdcard':{'is_active':False}}}}


@pytest.mark.parametrize('state', ['standby','complete','cancelled'])
def test_moonraker_fully_ready_inactive_sd(state):
    payload = moonraker(); payload['result']['status']['print_stats']['state'] = state
    assert raw_status_is_idle('moonraker', payload)


@pytest.mark.parametrize('section,value', [('webhooks',None),('print_stats',None),('virtual_sdcard',None),('webhooks',{'state':'shutdown'}),('webhooks',{'state':'startup'}),('print_stats',{'state':'printing'}),('print_stats',{'state':'paused'}),('print_stats',{'state':'ready'}),('virtual_sdcard',{'is_active':True}),('virtual_sdcard',{'is_active':'false'}),('virtual_sdcard',{'is_active':0}),('virtual_sdcard',{})])
def test_moonraker_unsafe_or_incomplete_observation_refused(section,value):
    payload = deepcopy(moonraker()); payload['result']['status'][section] = value
    assert not raw_status_is_idle('moonraker', payload)


@pytest.mark.parametrize('state', ['IDLE','READY'])
@pytest.mark.parametrize('job', [None,{}])
def test_prusalink_idle_without_active_job(state,job):
    assert raw_status_is_idle('prusalink', {'printer':{'state':state},'job':job})


@pytest.mark.parametrize('job', [False,True,[],0,'',{'id':297}])
def test_prusalink_malformed_or_active_job_refused(job):
    assert not raw_status_is_idle('prusalink', {'printer':{'state':'IDLE'},'job':job})


@pytest.mark.parametrize('state', ['PRINTING','PAUSED','BUSY','ERROR','ATTENTION','OPERATIONAL',None])
def test_prusalink_busy_unknown_legacy_state_refused(state):
    assert not raw_status_is_idle('prusalink', {'printer':{'state':state}})


@pytest.mark.parametrize('protocol', ['bambu','moonraker','prusalink','elegoo','unknown'])
@pytest.mark.parametrize('payload', [None,[],True,{},'IDLE'])
def test_no_default_state_from_malformed_or_unknown_response(protocol,payload):
    assert not raw_status_is_idle(protocol,payload)
