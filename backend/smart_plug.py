# Re-export stub — canonical location: modules/printers/smart_plug.py
from modules.printers.smart_plug import *  # noqa: F401, F403
from modules.printers.smart_plug import (  # noqa: F401
    tasmota_power, tasmota_energy, ha_switch, ha_get_state, mqtt_power,
    get_plug_config, power_on, power_off, power_toggle, get_energy,
    get_power_state, on_print_start, on_print_complete, record_energy_for_job,
)
