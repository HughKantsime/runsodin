"""
Bambu Lab Printer Integration

Provides MQTT-based communication with Bambu Lab printers for:
- Connection testing
- AMS filament slot synchronization  
- Filament type mapping (including Bambu-specific codes like PLA-S, PLA-CF, etc.)

Install dependency: pip install paho-mqtt --break-system-packages
"""

import json
import ssl
import time
import math
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    mqtt = None


# ============== Bambu Filament Type Mapping ==============

BAMBU_FILAMENT_TYPE_MAP = {
    # Standard PLA variants
    "PLA": "PLA",
    "PLA-S": "PLA_SUPPORT",
    "PLA-CF": "PLA_CF",
    "PLA-SILK": "PLA",
    "PLA-MATTE": "PLA",
    "PLA-AERO": "PLA",
    
    # PETG variants
    "PETG": "PETG",
    "PETG-CF": "PETG_CF",
    
    # ABS variants
    "ABS": "ABS",
    "ABS-GF": "ABS_GF",
    
    # ASA
    "ASA": "ASA",
    
    # TPU/Flexible
    "TPU": "TPU",
    "TPU-95A": "TPU_95A",
    "TPU-83A": "TPU_83A",
    
    # Nylon/Polyamide - Bambu uses PA
    "PA": "PA",
    "PA-CF": "NYLON_CF",
    "PA-GF": "NYLON_GF",
    "PA6": "PA",
    "PA6-CF": "NYLON_CF",
    "PA6-GF": "NYLON_GF",
    "PA12-CF": "NYLON_CF",
    
    # Polycarbonate
    "PC": "PC",
    "PC-ABS": "PC_ABS",
    "PC-CF": "PC_CF",
    
    # Support materials
    "PVA": "PVA",
    "HIPS": "HIPS",
    "SUPPORT": "SUPPORT",
    "SUPPORT-W": "PVA",
    "SUPPORT-G": "HIPS",
    
    # High-performance
    "PPS": "PPS",
    "PPS-CF": "PPS_CF",
    "PPA-CF": "NYLON_CF",
    "PPA-GF": "NYLON_GF",
    
    # Specialty
    "PP": "PP",
    "PP-CF": "PP_CF",
    "PP-GF": "PP_GF",
    
    # Fallback
    "": "OTHER",
    "GENERIC": "OTHER",
    "UNKNOWN": "OTHER",
}


def map_bambu_filament_type(bambu_type: str) -> str:
    """Map a Bambu Lab filament type code to our normalized FilamentType."""
    if not bambu_type:
        return "OTHER"
    
    normalized = bambu_type.upper().strip()
    
    if normalized in BAMBU_FILAMENT_TYPE_MAP:
        return BAMBU_FILAMENT_TYPE_MAP[normalized]
    
    cleaned = normalized.replace(" ", "-").replace("_", "-")
    if cleaned in BAMBU_FILAMENT_TYPE_MAP:
        return BAMBU_FILAMENT_TYPE_MAP[cleaned]
    
    for base in ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PP"]:
        if normalized.startswith(base):
            return BAMBU_FILAMENT_TYPE_MAP.get(base, "OTHER")
    
    return "OTHER"


# ============== Color Utilities ==============

def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.lstrip('#')
    if len(hex_color) >= 6:
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
    return (128, 128, 128)


def color_distance(hex1: str, hex2: str) -> float:
    r1, g1, b1 = hex_to_rgb(hex1)
    r2, g2, b2 = hex_to_rgb(hex2)
    return math.sqrt((r2-r1)**2 + (g2-g1)**2 + (b2-b1)**2)


def find_closest_color_match(target_hex: str, candidates: List[Dict], max_distance: float = 50.0) -> Optional[Dict]:
    if not target_hex or not candidates:
        return None
    best_match, best_distance = None, float('inf')
    for c in candidates:
        if c.get('color_hex'):
            d = color_distance(target_hex, c['color_hex'])
            if d < best_distance:
                best_distance, best_match = d, c
    return best_match if best_match and best_distance <= max_distance else None


def get_color_name_from_hex(hex_color: str) -> str:
    r, g, b = hex_to_rgb(hex_color)
    if r > 200 and g > 200 and b > 200: return "White"
    if r < 50 and g < 50 and b < 50: return "Black"
    if r > 200 and g < 100 and b < 100: return "Red"
    if r < 100 and g > 200 and b < 100: return "Green"
    if r < 100 and g < 100 and b > 200: return "Blue"
    if r > 200 and g > 200 and b < 100: return "Yellow"
    if r > 200 and g > 100 and b < 100: return "Orange"
    if abs(r-g) < 30 and abs(g-b) < 30: return "Gray"
    return "Unknown"


# ============== Data Classes ==============

@dataclass
class BambuPrinterConfig:
    ip_address: str
    serial_number: str
    access_code: str
    port: int = 8883
    timeout: float = 10.0


@dataclass 
class AMSSlotInfo:
    ams_id: int
    tray_id: int
    slot_number: int
    filament_type: str
    mapped_type: str
    color_hex: Optional[str] = None
    remaining_percent: Optional[int] = None
    brand: Optional[str] = None
    is_empty: bool = False
    match_source: Optional[str] = None
    color_name: Optional[str] = None
    matched_filament: Optional[Dict] = None


@dataclass
class AMSSyncResult:
    success: bool
    printer_name: Optional[str] = None
    slots: List[AMSSlotInfo] = field(default_factory=list)
    message: str = ""
    raw_data: Optional[Dict] = None


# ============== MQTT Client ==============

class BambuMQTTClient:
    def __init__(self, config: BambuPrinterConfig):
        if not MQTT_AVAILABLE:
            raise ImportError("paho-mqtt required: pip install paho-mqtt --break-system-packages")
        self.config = config
        self.client = None
        self.connected = False
        self.printer_data = {}
        self._response_received = False
        
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            client.subscribe(f"device/{self.config.serial_number}/report")
            
    def _on_disconnect(self, client, userdata, rc, properties=None):
        self.connected = False
        
    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            if 'print' in payload:
                self.printer_data = payload['print']
                self._response_received = True
        except Exception: pass

    def connect(self) -> bool:
        try:
            self.client = mqtt.Client(client_id=f"printfarm_{int(time.time())}", protocol=mqtt.MQTTv5)
            self.client.username_pw_set("bblp", self.config.access_code)
            self.client.tls_set(cert_reqs=ssl.CERT_NONE)  # nosec B501
            self.client.tls_insecure_set(True)  # nosec B501
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.connect(self.config.ip_address, self.config.port, keepalive=60)
            self.client.loop_start()
            
            start = time.time()
            while not self.connected and (time.time() - start) < self.config.timeout:
                time.sleep(0.1)
            return self.connected
        except Exception as e:
            self.connected = False
            raise ConnectionError(f"Failed to connect: {e}")
            
    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            
    def request_status(self) -> Dict:
        if not self.connected:
            raise ConnectionError("Not connected")
        topic = f"device/{self.config.serial_number}/request"
        self._response_received = False
        self.client.publish(topic, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
        
        start = time.time()
        while not self._response_received and (time.time() - start) < self.config.timeout:
            time.sleep(0.1)
        if not self._response_received:
            raise TimeoutError("No response from printer")
        return self.printer_data
        
    def get_ams_slots(self) -> List[AMSSlotInfo]:
        status = self.request_status()
        slots = []
        for ams in status.get('ams', {}).get('ams', []):
            ams_id = int(ams.get('id', 0))
            for tray in ams.get('tray', []):
                tray_id = int(tray.get('id', 0))
                slot_number = (ams_id * 4) + tray_id + 1
                filament_type = tray.get('tray_type', '')
                color_hex = tray.get('tray_color', '')
                if color_hex and len(color_hex) >= 6:
                    color_hex = f"#{color_hex[:6]}"
                is_empty = not filament_type or tray.get('remain', -1) == 0
                slots.append(AMSSlotInfo(
                    ams_id=ams_id, tray_id=tray_id, slot_number=slot_number,
                    filament_type=filament_type, mapped_type=map_bambu_filament_type(filament_type),
                    color_hex=color_hex or None, remaining_percent=tray.get('remain'),
                    brand=tray.get('tray_sub_brands'), is_empty=is_empty
                ))
        return slots


# ============== High-Level Functions ==============

def test_bambu_connection(ip_address: str, serial_number: str, access_code: str) -> Dict[str, Any]:
    if not MQTT_AVAILABLE:
        return {"success": False, "message": "paho-mqtt not installed. Run: pip install paho-mqtt --break-system-packages"}
    
    client = BambuMQTTClient(BambuPrinterConfig(ip_address, serial_number, access_code))
    try:
        if client.connect():
            status = client.request_status()
            return {
                "success": True,
                "message": f"Connected to {status.get('printer_name', 'Unknown')}",
                "printer_name": status.get('printer_name', 'Unknown'),
                "model": status.get('printer_type', 'Unknown')
            }
        return {"success": False, "message": "Connection failed - check IP, serial, and access code"}
    except Exception as e:
        return {"success": False, "message": f"Connection error: {str(e)}"}
    finally:
        client.disconnect()


def sync_ams_filaments(ip_address: str, serial_number: str, access_code: str,
                       library_filaments: List[Dict] = None, spoolman_spools: List[Dict] = None) -> AMSSyncResult:
    if not MQTT_AVAILABLE:
        return AMSSyncResult(success=False, message="paho-mqtt not installed")
    
    client = BambuMQTTClient(BambuPrinterConfig(ip_address, serial_number, access_code))
    try:
        if not client.connect():
            return AMSSyncResult(success=False, message="Failed to connect")
        
        slots = client.get_ams_slots()
        status = client.request_status()
        printer_name = status.get('printer_name', 'Unknown')
        
        for slot in slots:
            if slot.is_empty:
                continue
            # Try library first
            if library_filaments:
                match = _find_match(slot.color_hex, slot.mapped_type, library_filaments)
                if match:
                    slot.brand = match.get('brand', slot.brand)
                    slot.match_source = 'library'
                    slot.matched_filament = match
                    continue
            # Try Spoolman
            if spoolman_spools:
                match = _find_match(slot.color_hex, slot.mapped_type, spoolman_spools)
                if match:
                    slot.brand = match.get('brand', slot.brand)
                    slot.match_source = 'spoolman'
                    slot.matched_filament = match
                    continue
            # Fallback
            if slot.color_hex:
                slot.color_name = get_color_name_from_hex(slot.color_hex)
                slot.match_source = 'color_analysis'
            else:
                slot.match_source = 'unknown'
        
        return AMSSyncResult(success=True, printer_name=printer_name, slots=slots,
                           message=f"Synced {len([s for s in slots if not s.is_empty])} slots", raw_data=status)
    except Exception as e:
        return AMSSyncResult(success=False, message=f"Sync failed: {str(e)}")
    finally:
        client.disconnect()


def _find_match(color_hex: Optional[str], material_type: str, candidates: List[Dict]) -> Optional[Dict]:
    if not color_hex or not candidates:
        return None
    type_matches = [c for c in candidates if c.get('material', '').upper() == material_type.upper()]
    if type_matches:
        match = find_closest_color_match(color_hex, type_matches, 40.0)
        if match:
            return match
    return find_closest_color_match(color_hex, candidates, 20.0)


def slot_to_dict(slot: AMSSlotInfo) -> Dict[str, Any]:
    result = {
        "ams_id": slot.ams_id, "tray_id": slot.tray_id, "slot_number": slot.slot_number,
        "filament_type_raw": slot.filament_type, "filament_type": slot.mapped_type,
        "color_hex": slot.color_hex, "remaining_percent": slot.remaining_percent,
        "brand": slot.brand, "is_empty": slot.is_empty,
        "match_source": slot.match_source, "color_name": slot.color_name,
    }
    if slot.matched_filament:
        result["matched_filament_id"] = slot.matched_filament.get('id')
        result["matched_filament_name"] = slot.matched_filament.get('name')
    return result
