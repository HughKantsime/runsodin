"""Owned runtime-bound reports; DB configuration/owner authority still required."""
from dataclasses import dataclass
from functools import partial
import math
import re

from core.runtime_identity import read_runtime_identity,revalidate_runtime_identity
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector,RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_acquisition_session import _network_worker,_run_owned_worker


@dataclass(frozen=True)
class RuntimeBoundAMSResult:
    success:bool
    snapshot:RawAMSSnapshot|None
    runtime_incarnation:str|None
    clock_domain:str|None
    cleanup_confirmed:bool
    error:str|None


class _BufferedResult:
    def __init__(self):
        self.value=None
        self.seen=False
    def put(self,value):
        if self.seen:
            raise ValueError('AMS result repeated')
        self.seen=True
        self.value=value


def _trusted_network_worker(channel,config,key,owner,slots,incarnation,deadline,*,runtime):
    result={'snapshot':None,'error':'AMS runtime evidence unavailable',
            'runtime_incarnation':None,'clock_domain':None}
    try:
        revalidate_runtime_identity(runtime)
        captured=_BufferedResult()
        _network_worker(captured,config,key,owner,slots,incarnation,deadline)
        revalidate_runtime_identity(runtime)
        if type(captured.value) is not dict or set(captured.value)!={'snapshot','error'}:
            raise ValueError('AMS result shape unavailable')
        result={**captured.value,'runtime_incarnation':runtime.incarnation,
                'clock_domain':runtime.clock_domain}
    except Exception:
        pass
    channel.put(result)


def read_current_selected_ams(config,*,configuration_key,owner_org_id,selected_slots,timeout=10.0,operation_id=None):
    """No caller runtime labels. Never renew the worker's original receipt.

    This binds local process/clock provenance, not the supplied configuration
    to the database or authorization to dispatch hardware.
    """
    failure=lambda cleanup:RuntimeBoundAMSResult(False,None,None,None,cleanup,
                                                 'AMS runtime evidence unavailable')
    try:
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1<=timeout<=30:
            return failure(True)
        if operation_id is not None and (type(operation_id) is not str or re.fullmatch('[0-9a-f]{32}',operation_id) is None):
            return failure(True)
        RawAMSAcquisitionCollector(topic=config.topic_report,configuration_key=configuration_key,
            owner_org_id=owner_org_id,selected_slots=selected_slots,subscribe_mid=1)
        runtime=read_runtime_identity()
    except Exception:
        return failure(True)
    result=_run_owned_worker(config,configuration_key,owner_org_id,selected_slots,timeout,
        worker=partial(_trusted_network_worker,runtime=runtime),required_runtime=runtime,acquisition_id=operation_id)
    if not result.success or not result.cleanup_confirmed:
        return failure(result.cleanup_confirmed)
    try:
        revalidate_runtime_identity(runtime)
    except Exception:
        return failure(result.cleanup_confirmed)
    return RuntimeBoundAMSResult(True,result.snapshot,runtime.incarnation,runtime.clock_domain,True,None)
