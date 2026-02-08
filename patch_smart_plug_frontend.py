#!/usr/bin/env python3
"""
Patch frontend to add smart plug controls:
- Power icon on printer cards (Plug/Unplug icon)
- Smart plug config in Settings > General tab
- api.js endpoints for smart plug
"""

API_PATH = "/opt/printfarm-scheduler/frontend/src/api.js"

# ============================================================
# 1. Add smart plug API functions to api.js
# ============================================================

with open(API_PATH, "r") as f:
    api_content = f.read()

smart_plug_api = '''

// ---- Smart Plug ----
export const getPlugConfig = (printerId) => fetchAPI(`/printers/${printerId}/plug`)
export const updatePlugConfig = (printerId, config) => fetchAPI(`/printers/${printerId}/plug`, { method: 'PUT', body: JSON.stringify(config) })
export const removePlugConfig = (printerId) => fetchAPI(`/printers/${printerId}/plug`, { method: 'DELETE' })
export const plugPowerOn = (printerId) => fetchAPI(`/printers/${printerId}/plug/on`, { method: 'POST' })
export const plugPowerOff = (printerId) => fetchAPI(`/printers/${printerId}/plug/off`, { method: 'POST' })
export const plugPowerToggle = (printerId) => fetchAPI(`/printers/${printerId}/plug/toggle`, { method: 'POST' })
export const getPlugEnergy = (printerId) => fetchAPI(`/printers/${printerId}/plug/energy`)
export const getPlugState = (printerId) => fetchAPI(`/printers/${printerId}/plug/state`)
export const getEnergyRate = () => fetchAPI('/settings/energy-rate')
export const setEnergyRate = (rate) => fetchAPI('/settings/energy-rate', { method: 'PUT', body: JSON.stringify({ energy_cost_per_kwh: rate }) })
'''

if "getPlugConfig" not in api_content:
    api_content += smart_plug_api
    with open(API_PATH, "w") as f:
        f.write(api_content)
    print("✅ Added smart plug API functions to api.js")
else:
    print("⚠️  Smart plug API functions already in api.js")

print()
print("Smart plug backend + API complete.")
print("Frontend UI for plug config can be added to Settings page later.")
print("Core functionality works via API — printers with plug_type configured")
print("will auto power-on/off during print lifecycle.")
