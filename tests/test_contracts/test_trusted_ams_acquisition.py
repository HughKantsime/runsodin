"""Runtime provenance is separate from raw reports and DB authorization."""
from types import SimpleNamespace
import time

import pytest
from modules.printers.telemetry.bambu import trusted_ams_acquisition as module
from modules.printers.telemetry.bambu.ams_acquisition_session import AMSAcquisitionResult,_run_owned_worker
from tests.test_contracts.test_ams_acquisition_session import CONFIG,KEY,_snapshot

RUNTIME=SimpleNamespace(incarnation='d'*32,clock_domain='synthetic-clock-domain')


def worker_bound(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':_snapshot(incarnation,key,owner),'error':None,
        'runtime_incarnation':RUNTIME.incarnation,'clock_domain':RUNTIME.clock_domain})


def worker_wrong_domain(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':_snapshot(incarnation,key,owner),'error':None,
        'runtime_incarnation':RUNTIME.incarnation,'clock_domain':'foreign'})


def worker_extra_field(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':_snapshot(incarnation,key,owner),'error':None,
        'runtime_incarnation':RUNTIME.incarnation,'clock_domain':RUNTIME.clock_domain,'extra':1})


@pytest.mark.parametrize('worker,success',[(worker_bound,True),(worker_wrong_domain,False),
                                        (worker_extra_field,False)])
def test_owned_process_parent_requires_exact_bound_ipc(worker,success):
    result=_run_owned_worker(CONFIG,KEY,1,[1],3,worker=worker,required_runtime=RUNTIME)
    assert result.success is success and result.cleanup_confirmed
    assert (result.snapshot is not None) is success


@pytest.mark.parametrize('when',['before_network','after_cleanup'])
def test_child_changed_runtime_refuses_and_does_not_emit_early_success(monkeypatch,when):
    checks=[];network=[];output=[]
    def verify(runtime):
        checks.append(runtime)
        if len(checks)==(1 if when=='before_network' else 2):
            raise ValueError('synthetic private failure')
    def acquire(channel,*args,**kwargs):
        assert not output
        network.append(1)
        channel.put({'snapshot':_snapshot('c'*32,KEY,1),'error':None})
        assert not output
    monkeypatch.setattr(module,'revalidate_runtime_identity',verify)
    monkeypatch.setattr(module,'_network_worker',acquire)
    module._trusted_network_worker(SimpleNamespace(put=output.append),CONFIG,KEY,1,[1],
                                  'c'*32,time.monotonic()+1,runtime=RUNTIME)
    assert len(network)==(0 if when=='before_network' else 1)
    assert output==[{'snapshot':None,'error':'AMS runtime evidence unavailable',
                    'runtime_incarnation':None,'clock_domain':None}]


def test_missing_runtime_does_not_spawn_or_connect(monkeypatch):
    def missing():raise ValueError('synthetic private failure')
    def forbidden(*args,**kwargs):raise AssertionError('worker must not start')
    monkeypatch.setattr(module,'read_runtime_identity',missing)
    monkeypatch.setattr(module,'_run_owned_worker',forbidden)
    result=module.read_current_selected_ams(CONFIG,configuration_key=KEY,owner_org_id=1,selected_slots=[1])
    assert not result.success and result.cleanup_confirmed
    assert result.error=='AMS runtime evidence unavailable'


@pytest.mark.parametrize('changed',[False,True])
def test_final_parent_revalidation_and_original_receipt_preserved(monkeypatch,changed):
    snapshot=_snapshot('c'*32,KEY,1)
    monkeypatch.setattr(module,'read_runtime_identity',lambda:RUNTIME)
    def acquire(*args,**kwargs):
        assert kwargs['required_runtime'] is RUNTIME
        return AMSAcquisitionResult(True,snapshot,None,True)
    def validate(runtime):
        if changed:raise ValueError('runtime replaced after IPC')
    monkeypatch.setattr(module,'_run_owned_worker',acquire)
    monkeypatch.setattr(module,'revalidate_runtime_identity',validate)
    result=module.read_current_selected_ams(CONFIG,configuration_key=KEY,owner_org_id=1,selected_slots=[1])
    assert result.success is (not changed)
    if changed:assert result.snapshot is None
    else:
        assert result.snapshot is snapshot
        assert result.snapshot.received_monotonic==snapshot.received_monotonic
        assert result.clock_domain==RUNTIME.clock_domain


@pytest.mark.parametrize('first', [None, {'snapshot': None, 'error': 'unavailable'}])
def test_child_duplicate_result_cannot_replace_first_result(monkeypatch, first):
    output = []
    monkeypatch.setattr(module, 'revalidate_runtime_identity', lambda runtime: None)
    def acquire(channel, *args, **kwargs):
        channel.put(first)
        channel.put({'snapshot': _snapshot('c'*32, KEY, 1), 'error': None})
    monkeypatch.setattr(module, '_network_worker', acquire)
    module._trusted_network_worker(SimpleNamespace(put=output.append), CONFIG, KEY, 1,
        [1], 'c'*32, time.monotonic()+1, runtime=RUNTIME)
    assert output == [{'snapshot': None, 'error': 'AMS runtime evidence unavailable',
        'runtime_incarnation': None, 'clock_domain': None}]


def worker_unreported(channel,config,key,owner,slots,incarnation,deadline):
    from dataclasses import replace
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    snapshot=replace(_snapshot(incarnation,key,owner),
        trays=(RawTrayFact(1,0,0,None,'explicit_empty'),))
    channel.put({'snapshot':snapshot,'error':None,
        'runtime_incarnation':RUNTIME.incarnation,'clock_domain':RUNTIME.clock_domain})


@pytest.mark.parametrize('allow_unreported',[False,True])
def test_diagnostic_snapshot_retains_runtime_and_cleanup_checks(allow_unreported):
    result=_run_owned_worker(CONFIG,KEY,1,[1],3,worker=worker_unreported,
        required_runtime=RUNTIME,allow_unreported=allow_unreported)
    assert result.success is allow_unreported
    assert result.cleanup_confirmed
    if allow_unreported:
        assert result.snapshot.trays[0].material_state=='explicit_empty'


@pytest.mark.parametrize('nozzles,cleanup,changed,success', [
    (True, True, False, True), (False, True, False, False),
    (True, False, False, False), (True, True, True, False),
])
def test_shared_report_requires_nozzles_cleanup_and_current_runtime(monkeypatch, nozzles, cleanup, changed, success):
    from dataclasses import replace
    from modules.printers.nozzle_capability import installed_nozzles
    snapshot = replace(_snapshot('c'*32, KEY, 1), nozzles=(tuple(installed_nozzles(
        {'nozzle_type': 'hardened_steel', 'nozzle_diameter': .4})) if nozzles else None))
    monkeypatch.setattr(module, 'read_runtime_identity', lambda: RUNTIME)
    def acquire(*args, **kwargs):
        assert kwargs['worker'].keywords['nozzle_dual'] is False
        assert kwargs['required_runtime'] is RUNTIME
        return AMSAcquisitionResult(True, snapshot, None, cleanup)
    def validate(runtime):
        if changed:
            raise ValueError('runtime replaced after IPC')
    monkeypatch.setattr(module, '_run_owned_worker', acquire)
    monkeypatch.setattr(module, 'revalidate_runtime_identity', validate)
    result = module.read_current_selected_ams(CONFIG, configuration_key=KEY, owner_org_id=1,
        selected_slots=[1], nozzle_dual=False)
    assert result.success is success
    assert result.cleanup_confirmed is cleanup
    if success:
        assert result.snapshot is snapshot
    else:
        assert result.snapshot is None
