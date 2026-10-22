"""Bounded owned-process read-only AMS acquisition; no durable admission authority."""
import json
import math
import multiprocessing
import queue
import re
import time
import uuid
from dataclasses import dataclass
from threading import Event, RLock
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector, RawAMSSnapshot, safe_status_fields
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
from core.material_identity import canonical_reported_material


@dataclass(frozen=True)
class AMSAcquisitionResult:
    success: bool
    snapshot: RawAMSSnapshot | None
    error: str | None
    cleanup_confirmed: bool


def _code(value):
    value=getattr(value,'value',value)
    return int(value) if isinstance(value,int) and not isinstance(value,bool) else None


class AMSSessionController:
    """Paho callbacks bound to one subscription and one completed acquisition."""
    def __init__(self,config,configuration_key,owner_org_id,selected_slots,incarnation):
        self.config=config;self.key=configuration_key;self.owner=owner_org_id
        self.selected=tuple(selected_slots);self.incarnation=incarnation
        self.done=Event();self._lock=RLock();self.collector=None
        self._pending_ack={};self._connected=False;self._failed=False
        self._closing=False;self._requested=False

    def _refuse(self):
        self._failed=True
        if self.collector:self.collector.retire()
        self.done.set()

    def on_connect(self,client,userdata,flags,reason_code,properties=None):
        with self._lock:
            if self._closing or self._failed:return
            if self._connected or _code(reason_code)!=0:self._refuse();return
            self._connected=True
            try:
                result=client.subscribe(self.config.topic_report,qos=0)
                if len(result)!=2 or _code(result[0])!=0:raise ValueError()
                self.collector=RawAMSAcquisitionCollector(topic=self.config.topic_report,
                    configuration_key=self.key,owner_org_id=self.owner,
                    selected_slots=self.selected,subscribe_mid=result[1],incarnation=self.incarnation)
                pending=self._pending_ack.pop(result[1],None)
                if pending is not None:self.on_subscribe(client,userdata,result[1],pending)
            except Exception:self._refuse()

    def on_subscribe(self,client,userdata,mid,reason_codes,properties=None):
        with self._lock:
            if self._failed or self._closing:return
            if type(mid) is not int or not 1<=mid<=65535:self._refuse();return
            if self.collector is None:
                if not self._connected or len(self._pending_ack)>=8:self._refuse();return
                self._pending_ack[mid]=reason_codes;return
            codes=[_code(c) for c in reason_codes] if isinstance(reason_codes,(list,tuple)) else []
            if codes!=[0]:self._refuse();return
            if not self.collector.acknowledge_subscription(self.incarnation,mid,codes):return
            if self._requested:return
            self._requested=True
            try:
                published=client.publish(self.config.topic_request,
                    json.dumps({'pushing':{'sequence_id':'0','command':'pushall'}}),qos=0,retain=False)
                if _code(published.rc)!=0:self._refuse()
            except Exception:self._refuse()

    def on_message(self,client,userdata,msg):
        received_at=time.monotonic()
        with self._lock:
            if self._closing or self._failed or self.collector is None:return
            try:
                # Let the collector gate readiness before decoding any packet.
                if not self._requested:return
                if getattr(msg,"topic",None)!=self.config.topic_report or getattr(msg,"retain",None) is not False:
                    self._refuse();return
                if type(msg.payload) is not bytes or len(msg.payload)>1024*1024:raise ValueError()
                raw=json.loads(msg.payload)
                if not isinstance(raw,dict):raise ValueError()
                if 'print' not in raw and isinstance(raw.get('info'),dict):return
                packet=raw.get('print')
                accepted=self.collector.observe(incarnation=self.incarnation,
                    topic=getattr(msg,'topic',None),retained=getattr(msg,'retain',None),packet=packet,
                    received_monotonic=received_at)
                if self.collector.retired:self._refuse()
                elif accepted:self.done.set()
            except Exception:self._refuse()

    def on_disconnect(self,client,userdata,*args,**kwargs):
        with self._lock:
            if not self._closing:self._refuse()

    def freeze(self):
        with self._lock:
            snapshot=None if self._failed or self.collector is None else self.collector.snapshot(
                configuration_key=self.key,owner_org_id=self.owner)
            # Boundary ends acquisition; later expected shutdown is not a lease.
            self._closing=True
            if self.collector:self.collector.retire()
            return snapshot


def _network_worker(channel,config,key,owner,slots,incarnation,deadline):
    import logging
    logging.disable(logging.CRITICAL)
    adapter=None;snapshot=None;error='AMS acquisition unavailable'
    try:
        from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
        from modules.printers.telemetry.bambu.adapter import BambuTelemetryAdapter
        import paho.mqtt.client as mqtt
        from core.itar import enforce_host_destination
        enforce_host_destination(config.host, scheme="mqtt")
        policy=resolve_bambu_broker_config(config.host,printer_id='ams-read-acquisition')
        if config.port!=policy.port or config.use_tls is not policy.use_tls:raise ValueError()
        controller=AMSSessionController(config,key,owner,slots,incarnation)
        adapter=BambuTelemetryAdapter(config,lambda event:None)
        def factory():
            client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                               protocol=mqtt.MQTTv311,reconnect_on_failure=False)
            client.on_subscribe=controller.on_subscribe
            return client
        adapter._client_factory=factory
        adapter._on_connect=controller.on_connect
        adapter._on_disconnect=controller.on_disconnect
        adapter._on_message=controller.on_message
        # Reuses the existing adapter's credentials/TLS/configuration policy.
        adapter.start()
        controller.done.wait(max(0,deadline-time.monotonic()))
        snapshot=controller.freeze()
        error=None if snapshot is not None else error
    except Exception:pass
    finally:
        try:
            if adapter:adapter.stop()
        except Exception:snapshot=None;error='AMS acquisition cleanup failed'
    # IPC success is delivered only after worker network cleanup.
    channel.put({'snapshot':snapshot,'error':error})


def _valid_snapshot(snapshot,*,key,owner,slots,incarnation,started,deadline,now):
    if type(snapshot) is not RawAMSSnapshot or now > deadline:return False
    if (type(snapshot.incarnation) is not str or type(snapshot.configuration_key) is not str
        or snapshot.incarnation!=incarnation or snapshot.configuration_key!=key
        or snapshot.owner_org_id!=owner or type(snapshot.owner_org_id) is not type(owner)
        or type(snapshot.packet_id) is not str or not re.fullmatch('[0-9a-f]{32}',snapshot.packet_id)
        or type(snapshot.local_receipt_order) is not int or snapshot.local_receipt_order<1
        or type(snapshot.received_monotonic) not in (int,float)
        or not math.isfinite(snapshot.received_monotonic)
        or not started<=snapshot.received_monotonic<=min(now,deadline)
        or now-snapshot.received_monotonic>=120
        or type(snapshot.idle_without_error) is not bool
        or type(snapshot.trays) is not tuple
        or len(snapshot.trays)!=len(slots)):return False
    diagnostics = safe_status_fields({
        'gcode_state': snapshot.gcode_state, 'print_error': snapshot.print_error,
        'hms': None if snapshot.active_hms_codes is None else []})
    if (diagnostics['gcode_state'] != snapshot.gcode_state
        or diagnostics['print_error'] != snapshot.print_error
        or (snapshot.active_hms_codes is not None and
            (type(snapshot.active_hms_codes) is not tuple or any(
                type(code) is not str or re.fullmatch('[0-9A-F]{8}_[0-9A-F]{8}', code) is None
                for code in snapshot.active_hms_codes)))):
        return False
    if tuple(f.slot_number for f in snapshot.trays if type(f) is RawTrayFact)!=tuple(sorted(slots)):return False
    for fact in snapshot.trays:
        if (type(fact) is not RawTrayFact or type(fact.slot_number) is not int
            or type(fact.unit_id) is not int or type(fact.tray_id) is not int
            or not 0<=fact.unit_id<=3 or not 0<=fact.tray_id<=3
            or fact.slot_number!=fact.unit_id*4+fact.tray_id+1
            or fact.material_state!='reported' or fact.mechanically_ready is not None
            or type(fact.material) is not str or canonical_reported_material(fact.material) is None
            or canonical_reported_material(fact.material)!=fact.material):return False
    return True


def _cleanup_owned(process,channel):
    """Attempt every remaining cleanup step; uncertainty can never keep success."""
    certain=True
    try:started=process.pid is not None
    except Exception:started=True;certain=False
    if started:
        try:alive=process.is_alive()
        except Exception:alive=True;certain=False
        if alive:
            try:process.terminate()
            except Exception:certain=False
        try:process.join(1)
        except Exception:certain=False
        try:alive=process.is_alive()
        except Exception:alive=True;certain=False
        if alive:
            try:process.kill()
            except Exception:certain=False
            try:process.join(1)
            except Exception:certain=False
        try:reaped=not process.is_alive() and process.exitcode is not None
        except Exception:reaped=False;certain=False
        certain=certain and reaped
    try:channel.close()
    except Exception:certain=False
    try:channel.join_thread()
    except Exception:certain=False
    try:process.close()
    except Exception:certain=False
    return certain


def _run_owned_worker(config,key,owner,slots,timeout,*,worker=_network_worker,required_runtime=None,acquisition_id=None):
    if acquisition_id is not None and (type(acquisition_id) is not str or re.fullmatch('[0-9a-f]{32}',acquisition_id) is None):
        return AMSAcquisitionResult(False,None,'AMS acquisition identity unavailable',True)
    started=time.monotonic();deadline=started+timeout;incarnation=acquisition_id or uuid.uuid4().hex
    ctx=multiprocessing.get_context('spawn');channel=ctx.Queue(maxsize=1)
    process=ctx.Process(target=worker,args=(channel,config,key,owner,tuple(slots),incarnation,deadline),daemon=True)
    received=None;normal_exit=False;cleanup=False;error='AMS acquisition unavailable';snapshot=None
    try:
        process.start()
        while time.monotonic()<deadline:
            try:received=channel.get(timeout=min(.1,max(.001,deadline-time.monotonic())));break
            except queue.Empty:
                if not process.is_alive():break
        process.join(max(0,deadline-time.monotonic()))
        normal_exit=not process.is_alive() and process.exitcode==0
        if normal_exit and isinstance(received,dict) and received.get('error') is None:
            if required_runtime is not None:
                if (type(received) is not dict
                    or set(received)!={'snapshot','error','runtime_incarnation','clock_domain'}
                    or type(received.get('runtime_incarnation')) is not str
                    or type(received.get('clock_domain')) is not str
                    or received['runtime_incarnation']!=required_runtime.incarnation
                    or received['clock_domain']!=required_runtime.clock_domain):
                    raise ValueError('AMS runtime binding unavailable')
            candidate=received.get('snapshot')
            if _valid_snapshot(candidate,key=key,owner=owner,slots=slots,
                incarnation=incarnation,started=started,deadline=deadline,now=time.monotonic()):
                snapshot=candidate;error=None
            else:error='AMS acquisition evidence unavailable'
    except Exception:pass
    finally:
        cleanup=_cleanup_owned(process,channel)
    success=normal_exit and cleanup and snapshot is not None
    error_message=None if success else (error or 'AMS acquisition unavailable')
    return AMSAcquisitionResult(success,snapshot if success else None,error_message,cleanup)


def read_selected_ams(config,*,configuration_key,owner_org_id,selected_slots,timeout=10.0):
    """Return evidence from a completed read-only acquisition, never a live lease."""
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1<=timeout<=30:
        return AMSAcquisitionResult(False,None,'AMS acquisition timeout invalid',True)
    try:
        # Validate bindings before spawning or touching any network.
        RawAMSAcquisitionCollector(topic=config.topic_report,configuration_key=configuration_key,
            owner_org_id=owner_org_id,selected_slots=selected_slots,subscribe_mid=1)
    except Exception:return AMSAcquisitionResult(False,None,'AMS acquisition configuration unavailable',True)
    return _run_owned_worker(config,configuration_key,owner_org_id,selected_slots,timeout)
