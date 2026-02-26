# Re-export stub — canonical location: modules/printers/bambu_integration.py
from modules.printers.bambu_integration import *  # noqa: F401, F403
from modules.printers.bambu_integration import (  # noqa: F401
    map_bambu_filament_type, hex_to_rgb, color_distance,
    find_closest_color_match, get_color_name_from_hex,
    BambuPrinterConfig, AMSSlotInfo, AMSSyncResult, BambuMQTTClient,
    test_bambu_connection, sync_ams_filaments, _find_match, slot_to_dict,
)
