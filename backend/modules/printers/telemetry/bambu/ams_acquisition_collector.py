"""Owned raw tray collector, not network acquisition or durable admission authority."""
import math
import re
import time
import uuid
from dataclasses import dataclass, replace
from threading import RLock
from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts, RawTrayFact


@dataclass(frozen=True)
class RawAMSSnapshot:
    incarnation: str
    packet_id: str
    local_receipt_order: int
    received_monotonic: float
    configuration_key: str
    owner_org_id: int | None
    trays: tuple[RawTrayFact, ...]
    # Only explicit idle/no-error fields in this same AMS-bearing packet qualify.
    idle_without_error: bool = False

    # Diagnostic-only values, never used to grant or refuse acquisition.
    gcode_state: str | None = None
    print_error: int | None = None
    active_hms_codes: tuple[str, ...] | None = None
    # Normalized nozzle facts from this exact tray-bearing packet, never merged.
    nozzles: tuple[dict, ...] | None = None


def safe_status_fields(packet):
    """Copy only protocol enums and numeric codes, never arbitrary device text."""
    packet = packet if type(packet) is dict else {}
    state = packet.get('gcode_state')
    if type(state) is not str or state not in {
        'IDLE', 'FINISH', 'FAILED', 'RUNNING', 'PAUSE', 'PREPARE',
        'SLICING', 'PENDING', 'UNKNOWN', 'OFFLINE'}:
        state = None
    error = packet.get('print_error')
    if type(error) is not int or not 0 <= error <= 0xffffffff:
        error = None
    hms = packet.get('hms')
    codes = None
    if type(hms) is list:
        codes = tuple(f"{item['attr']:08X}_{item['code']:08X}" for item in hms
            if type(item) is dict and all(type(item.get(k)) is int
                and 0 <= item[k] <= 0xffffffff for k in ('attr', 'code')))
    return dict(gcode_state=state, print_error=error, active_hms_codes=codes)


class RawAMSAcquisitionCollector:
    def __init__(self, *, topic, configuration_key, owner_org_id, selected_slots,
                 subscribe_mid, clock=time.monotonic, max_age=120.0, incarnation=None, allow_unreported=False, nozzle_dual=None):
        if (not isinstance(topic,str) or not re.fullmatch(r'device/[^/\s+#\x00]+/report',topic)
            or not isinstance(configuration_key,str) or not re.fullmatch(r'[0-9a-f]{64}',configuration_key)
            or (owner_org_id is not None and (type(owner_org_id) is not int or owner_org_id <= 0))
            or type(subscribe_mid) is not int or not 1 <= subscribe_mid <= 65535
            or not isinstance(selected_slots,(list,tuple,set,frozenset)) or not selected_slots
            or any(type(s) is not int or not 1 <= s <= 16 for s in selected_slots)
            or len(set(selected_slots)) != len(selected_slots)
            or (nozzle_dual is not None and type(nozzle_dual) is not bool)
            or type(max_age) not in (int,float) or not math.isfinite(max_age) or not 0 < max_age <= 120):
            raise ValueError('AMS acquisition configuration unavailable')
        if incarnation is not None and (type(incarnation) is not str or not re.fullmatch("[0-9a-f]{32}",incarnation)):
            raise ValueError("AMS acquisition incarnation unavailable")
        self._nozzle_dual = nozzle_dual
        self._allow_unreported = allow_unreported is True
        self.incarnation=incarnation if incarnation is not None else uuid.uuid4().hex
        self._topic=topic;self._config=configuration_key;self._owner=owner_org_id
        self._selected=frozenset(selected_slots);self._mid=subscribe_mid
        self._clock=clock;self._age=max_age;self._lock=RLock()
        self._ready=False;self._retired=False;self._snapshot=None;self._order=0
        self._last_receipt=None

    def acknowledge_subscription(self, incarnation, mid, reason_codes):
        with self._lock:
            if incarnation != self.incarnation or self._retired or type(mid) is not int or mid != self._mid:
                return False
            if (not isinstance(reason_codes,(list,tuple)) or len(reason_codes) != 1
                or any(type(c) is not int or c not in (0,1,2) for c in reason_codes)):
                self.retire();return False
            self._ready=True
            return True

    def retire(self):
        with self._lock:
            self._retired=True;self._ready=False;self._snapshot=None

    def observe(self, *, incarnation, topic, retained, packet, received_monotonic=None):
        with self._lock:
            if incarnation != self.incarnation or self._retired or not self._ready:
                return False
            if topic != self._topic or retained is not False:
                self.retire();return False
            now=self._clock() if received_monotonic is None else received_monotonic
            if (type(now) not in (int,float) or not math.isfinite(now) or now < 0
                or now > self._clock()
                or (self._last_receipt is not None and now < self._last_receipt)):
                self.retire();return False
            from modules.printers.dispatch_readiness import raw_status_is_idle
            idle_without_error = raw_status_is_idle('bambu', packet)
            diagnostics = safe_status_fields(packet)
            if self._snapshot is not None:
                diagnostics = {key: value if value is not None else getattr(self._snapshot, key)
                               for key, value in diagnostics.items()}
            if (self._snapshot is not None and self._snapshot.idle_without_error is True
                    and isinstance(packet, dict) and not idle_without_error
                    and any(key in packet for key in ('gcode_state', 'print_error', 'hms'))):
                self._snapshot = replace(self._snapshot,
                    idle_without_error=self._snapshot.idle_without_error and idle_without_error,
                    **diagnostics)
            try: facts=parse_raw_ams_facts(packet)
            except ValueError:self.retire();return False
            self._last_receipt=now;self._order+=1
            addressed={f.slot_number:f for f in facts if f.slot_number in self._selected}
            if any(f.material_state != 'reported' and not (self._allow_unreported
                    and f.material_state in ('explicit_empty', 'unknown')) for f in addressed.values()):
                self.retire();return False
            # A packet must explicitly report every selected tray. A partial
            # report cannot combine with earlier packet fields to create one.
            if set(addressed) != self._selected:
                # New relevant partial facts supersede earlier complete evidence.
                if addressed:self._snapshot=None
                return False
            nozzles = None
            if self._nozzle_dual is not None:
                from modules.printers.nozzle_capability import installed_nozzles
                device = packet.get('device')
                nozzle = device.get('nozzle') if isinstance(device, dict) else None
                has_rows = isinstance(nozzle, dict) and isinstance(nozzle.get('info'), list)
                if (packet.get('command') != 'push_status'
                        or (not has_rows and (self._nozzle_dual or 'nozzle_type' not in packet))):
                    self._snapshot = None
                    return False
                nozzles = tuple(installed_nozzles(packet, dual=self._nozzle_dual))
            self._snapshot=RawAMSSnapshot(self.incarnation,uuid.uuid4().hex,
                self._order,now,self._config,self._owner,
                tuple(addressed[s] for s in sorted(addressed)), idle_without_error, nozzles=nozzles, **diagnostics)
            return True

    @property
    def retired(self):
        """Distinguish waiting for evidence from sticky acquisition invalidation."""
        with self._lock:return self._retired

    def snapshot(self, *, configuration_key, owner_org_id):
        with self._lock:
            if (configuration_key != self._config or owner_org_id != self._owner
                or type(owner_org_id) is not type(self._owner)):
                self.retire();return None
            if self._retired or not self._ready or self._snapshot is None:return None
            now=self._clock()
            if type(now) not in (int,float) or not math.isfinite(now):
                self.retire();return None
            age=now-self._snapshot.received_monotonic
            if not math.isfinite(age) or age < 0 or age >= self._age:
                self.retire();return None
            return self._snapshot
