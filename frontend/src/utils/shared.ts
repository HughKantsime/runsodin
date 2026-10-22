import type { Printer, FilamentSlot } from '../types'

export const ONLINE_THRESHOLD_MS = 90_000

// ---- Print state vocabulary (one place for every protocol) ----
// Bambu reports RUNNING / PAUSE; Moonraker, PrusaLink and Elegoo monitors
// persist PRINTING / PAUSED. UI code must use these helpers, never compare
// gcode_state strings directly.
const PRINTING_STATES = new Set(['RUNNING', 'PRINTING'])
const PAUSED_STATES = new Set(['PAUSE', 'PAUSED'])

/** Protocol-neutral activity of a gcode_state: 'printing', 'paused' or null. */
export function printActivity(state: string | null | undefined): 'printing' | 'paused' | null {
  const normalized = (state || '').trim().toUpperCase()
  if (PRINTING_STATES.has(normalized)) return 'printing'
  if (PAUSED_STATES.has(normalized)) return 'paused'
  return null
}

/** True while a print is running or paused, for any protocol. */
export function isActivePrintState(state: string | null | undefined): boolean {
  return printActivity(state) !== null
}

/** True while a print is running (not paused), for any protocol. */
export function isPrintingState(state: string | null | undefined): boolean {
  return printActivity(state) === 'printing'
}

/** True while a print is paused, for any protocol. */
export function isPausedState(state: string | null | undefined): boolean {
  return printActivity(state) === 'paused'
}

// ---- Fan speed units ----
// Stored fan_speed is protocol-native: Bambu cooling_fan_speed is a 0-15
// gear, Moonraker is already converted to 0-100 %, Elegoo SDCP ModelFan is
// 0-100 %, PrusaLink fan_print is RPM.
export interface FanDisplay { value: number; unit: '%' | 'RPM'; text: string }

export function fanSpeedDisplay(apiType: string | null | undefined, raw: number | null | undefined): FanDisplay | null {
  if (raw == null || typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0) return null
  const type = (apiType || '').toLowerCase()
  if (type === 'prusalink') {
    const rpm = Math.round(raw)
    return { value: rpm, unit: 'RPM', text: `${rpm} RPM` }
  }
  const percent = type === 'bambu'
    ? Math.round(Math.min(raw, 15) / 15 * 100)
    : Math.round(Math.min(raw, 100))
  return { value: percent, unit: '%', text: `${percent}%` }
}

export function getShortName(slot: string | FilamentSlot | null): string {
  const color = typeof slot === 'string' ? slot : slot?.color
  const ft = typeof slot === 'string' ? '' : (slot?.filament_type || '')
  const fallback = (!ft || ft === 'empty') ? 'Empty' : ft
  if (!color || color.startsWith('#') || /^[0-9a-fA-F]{6}$/.test(color)) return fallback
  const brands = ['Bambu Lab', 'Polymaker', 'Hatchbox', 'eSun', 'Prusament', 'Overture', 'Generic']
  let short = color
  for (const brand of brands) {
    if (color.startsWith(brand + ' ')) {
      short = color.slice(brand.length + 1)
      break
    }
  }
  if (short.length > 12) return short.slice(0, 10) + '...'
  return short
}

/** Format a duration given in minutes to "Xh Ym" display. */
export function formatDuration(minutes: number | null | undefined): string {
  if (!minutes || minutes <= 0) return '0m'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  if (h === 0) return `${m}m`
  if (m === 0) return `${h}h`
  return `${h}h ${m}m`
}

/** Format a duration given in seconds to "Xh Ym" or "Xm Ys" display. */
export function formatDurationSecs(seconds: number | null | undefined): string {
  if (!seconds) return '--'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/** Format an ISO date string to locale-friendly display (e.g. "Jan 5, 2026, 03:45 PM"). */
export function formatDate(d: string | null | undefined): string {
  if (!d) return '--'
  return new Date(d).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** Format an ISO datetime string to time-only display (e.g. "3:45 PM"). */
export function formatTime(isoString: string | null | undefined): string {
  if (!isoString) return '\u2014'
  const d = new Date(isoString)
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}

/** Format decimal hours to "Xh Ym" display. */
export function formatHours(hours: number | null | undefined): string {
  if (!hours) return '\u2014'
  const mins = Math.round(hours * 60)
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  const m = mins % 60
  return m > 0 ? `${hrs}h ${m}m` : `${hrs}h`
}

/** Format a file size given in megabytes to human-readable display. */
export function formatSize(mb: number | null | undefined): string {
  if (!mb) return '-'
  if (mb < 1) return `${Math.round(mb * 1024)} KB`
  return `${mb.toFixed(1)} MB`
}

/** Format seconds to MM:SS display (zero-padded). */
export function formatMMSS(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** Check whether a printer is online based on its last_seen timestamp. */
export function isOnline(printer: Printer): boolean {
  if (printer.gcode_state?.trim().toUpperCase() === 'OFFLINE') return false
  if (!printer.last_seen) return false
  // Server timestamps without a zone are UTC; preserve explicit zones.
  const timestamp = printer.last_seen.trim()
  const qualified = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp)
  const lastSeen = new Date(qualified ? timestamp : `${timestamp}Z`).getTime()
  const age = Date.now() - lastSeen
  return Number.isFinite(lastSeen) && age >= 0 && age < ONLINE_THRESHOLD_MS
}

// ---- AMS slot grouping ----
// Backend slot numbers: 1-16 are four-tray AMS units 0-3 (unit = (n-1)/4),
// 17-24 are single-tray AMS HT units 128-135.
export const AMS_HT_FIRST_SLOT = 17
/** Last slot number the backend assigns: AMS HT units 128-135 -> slots 17-24. */
export const AMS_LAST_SLOT = 24

export interface AmsSlotGroup { key: string; label: string }

/** Unit group of a Bambu AMS slot, or null for a slot number outside AMS numbering. */
export function amsSlotGroup(slotNumber: number): AmsSlotGroup | null {
  if (!Number.isInteger(slotNumber) || slotNumber <= 0 || slotNumber > AMS_LAST_SLOT) return null
  if (slotNumber >= AMS_HT_FIRST_SLOT) {
    const index = slotNumber - AMS_HT_FIRST_SLOT + 1
    return { key: `ht-${index}`, label: `AMS HT ${index}` }
  }
  const unit = Math.floor((slotNumber - 1) / 4)
  return { key: `ams-${unit}`, label: `AMS Unit ${unit}` }
}

// ---- Nozzle capability ----
// Mirrors backend printer_models.BAMBU_NOZZLE_COUNTS (Bambu Studio profiles'
// nozzle_diameter length): H2D, H2D Pro and H2C have two nozzles.
const DUAL_NOZZLE_MODELS = new Set(['H2D', 'H2D PRO', 'H2C', 'O1D', 'O1E', 'O1C2'])

export function isDualNozzle(machineType: string | null | undefined): boolean {
  const normalized = (machineType || '').trim().toUpperCase().replace(/^BAMBU LAB /, '')
  return DUAL_NOZZLE_MODELS.has(normalized)
}

// Friendly names for dual-nozzle model codes (backend printer_models table).
const DUAL_NOZZLE_NAMES: Record<string, string> = {
  'H2D': 'H2D', 'O1D': 'H2D', 'H2D PRO': 'H2D Pro', 'O1E': 'H2D Pro', 'H2C': 'H2C', 'O1C2': 'H2C',
}

/** Display name of a dual-nozzle model ("O1E" -> "H2D Pro"); input unchanged otherwise. */
export function dualNozzleModelName(machineType: string | null | undefined): string {
  const raw = (machineType || '').trim()
  return DUAL_NOZZLE_NAMES[raw.toUpperCase().replace(/^BAMBU LAB /, '')] || raw
}

/** Four zero-indexed wire trays per ACE; ODIN slot numbers remain one based. */
export function filamentSlotGroup(apiType: string | null | undefined, slotNumber: number): AmsSlotGroup | null {
  if (apiType === 'bambu') return amsSlotGroup(slotNumber)
  if (apiType === 'anycubic_lan' && Number.isInteger(slotNumber) && slotNumber > 0) {
    const unit = Math.floor((slotNumber - 1) / 4)
    return { key: `ace-${unit}`, label: `ACE ${unit + 1}` }
  }
  return null
}
