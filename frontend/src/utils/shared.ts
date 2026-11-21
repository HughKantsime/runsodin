import type { Printer, FilamentSlot, FilamentType } from '../types'

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

const MATERIAL_NAMES = {
  empty: 'Empty', Unknown: 'Unknown', PLA: 'PLA', PETG: 'PETG', ABS: 'ABS', ASA: 'ASA', TPU: 'TPU', PA: 'PA', PC: 'PC', PVA: 'PVA',
  NYLON_CF: 'NYLON-CF', NYLON_GF: 'NYLON-GF', PC_ABS: 'PC-ABS', PC_CF: 'PC-CF', HIPS: 'HIPS', PPS: 'PPS', PPS_CF: 'PPS-CF', PPA_CF: 'PPA-CF', TPU_AMS: 'TPU for AMS',
  PLA_SUPPORT: 'PLA Support', PETG_SUPPORT: 'PETG Support', PA_SUPPORT: 'PA Support', SUPPORT: 'Support',
  PLA_CF: 'PLA-CF', PETG_CF: 'PETG-CF', PA_CF: 'PA-CF', PA_GF: 'PA-GF', PET_CF: 'PET-CF', ABS_GF: 'ABS-GF', ASA_CF: 'ASA-CF',
  PLA_AERO: 'PLA Aero', ASA_AERO: 'ASA Aero', PLA_SILK: 'PLA Silk', PLA_MATTE: 'PLA Matte', TPU_95A: 'TPU 95A', OTHER: 'Other',
} satisfies Record<FilamentType, string> & Record<string, string>
const FILAMENT_BRANDS = ['Bambu Lab', 'Bambu', 'Polymaker', 'Hatchbox', 'eSun', 'eSUN', 'Prusament', 'Overture', 'Elegoo', 'Sunlu', 'SUNLU', 'Anycubic', 'Creality', 'Generic']

/** A material code as people write it: "PLA_SUPPORT" is "PLA Support", "PETG_CF" is "PETG-CF". */
export function materialLabel(type: string | null | undefined, compact = false): string {
  const code = (type || '').trim()
  if (!code || code.toLowerCase() === 'empty') return 'Empty'
  const key = code.toUpperCase().replace(/-/g, '_')
  if (compact && key === 'TPU_AMS') return 'TPU-AMS'
  const known = (MATERIAL_NAMES as Record<string, string>)[key]
  if (known) return known
  return code.replace(/_(CF|GF)$/i, (_, fibre) => `-${fibre.toUpperCase()}`).replace(/_/g, ' ')
}

/** The colour or product name on a slot, without the "preset:" prefix, the brand, or a bare hex code. */
export function slotColorName(slot: string | FilamentSlot | null): string {
  let name = ((typeof slot === 'string' ? slot : slot?.color) || '').trim().replace(/^preset:\s*/i, '')
  if (!name || /^#?[0-9a-fA-F]{6,8}$/.test(name)) return ''
  for (const brand of FILAMENT_BRANDS) {
    if (name.toLowerCase().startsWith(brand.toLowerCase() + ' ')) { name = name.slice(brand.length + 1).trim(); break }
  }
  return name
}

/** What a small slot tile shows: the material ("PLA", "PLA Support"). The ring beside it shows the
 *  colour, so the colour name goes in the tooltip (slotFullName), not squeezed into the tile.
 *  With no material recorded, the colour name stands in. Never cut in code: the tile truncates. */
export function getShortName(slot: string | FilamentSlot | null): string {
  if (typeof slot === 'string') return slotColorName(slot) || 'Empty'
  const material = slot?.filament_type || (slot as { material_type?: string } | null)?.material_type || ''
  if (material && material.toLowerCase() !== 'empty') return materialLabel(material)
  return slotColorName(slot) || 'Empty'
}

/** Material and colour name together, for tooltips and wide rows: "PLA · Galaxy Black". */
export function slotFullName(slot: string | FilamentSlot | null): string {
  if (slot && typeof slot !== 'string' && slot.catalogue_name) return slot.catalogue_name
  const short = getShortName(slot)
  const name = slotColorName(slot)
  if (!name || name.toLowerCase() === short.toLowerCase() || short === 'Empty') return short === 'Empty' && name ? name : short
  // "Support for PLA" beside "PLA Support" says the same thing twice
  const words = (text: string) => text.toLowerCase().replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(w => w && w !== 'for').sort().join(' ')
  return words(name) === words(short) ? short : `${short} \u00B7 ${name}`
}

/** Shared Classic card/detail appearance: absence of a colour name is not an empty tray. */
export function classicSlotAppearance(slot: Partial<FilamentSlot> & { type?: string }) {
  const material = slot.material_type || slot.type || (slot.filament_type !== 'empty' && slot.filament_type)
  const hex = (slot.color_hex || (/^#?[0-9a-f]{6,8}$/i.test(slot.color || '') ? slot.color : '') || '').replace(/^#/, '')
  const named = slot.color && slot.color !== material && !/^preset:|^#?[0-9a-f]{6,8}$/i.test(slot.color) ? slot.color : null
  const empty = !slot.catalogue_name && !material && !slot.color && !slot.color_hex
  return {
    color: /^[0-9a-f]{6,8}$/i.test(hex) ? `#${hex}` : '#888',
    empty,
    name: slot.catalogue_name || named || (material ? materialLabel(material) : null) || (empty ? 'Empty' : 'Material unknown'),
  }
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

/** Parse a timestamp sent by the server.
 *  The API sends UTC without a zone ("2026-11-13T03:18:20"); `new Date()` would read that as
 *  LOCAL time and show it hours off. A value that already carries a zone is used as it is, and a
 *  date with no time ("2026-11-13") is a calendar day, not an instant: it stays that local day. */
export function parseServerDate(value: string | number | Date | null | undefined): Date {
  if (value instanceof Date) return value
  if (typeof value === 'number') return new Date(value)
  if (!value) return new Date(NaN)
  const text = String(value).trim()
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text)
  if (day) return new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3]))
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) {
    return new Date(text.replace(' ', 'T') + 'Z')
  }
  return new Date(text)
}

/** Read a calendar day sent by the server (a job's due date, for example).
 *  A day the user picked is stored as midnight UTC ("2026-11-20T00:00:00", sometimes with a "Z")
 *  or as a bare date. It is a day, not a moment: read as an instant it lands on Nov 19 for
 *  everyone west of UTC. Only the written year-month-day is used, and the result is that same
 *  day at local midnight, so it shows and compares as Nov 20 in every time zone.
 *  Never use this for real instants (created_at, started_at, last_seen): use parseServerDate. */
export function parseServerDay(value: string | null | undefined): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])/.exec(String(value ?? '').trim())
  if (!match) return null
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])]
  const date = new Date(year, month - 1, day)
  // new Date() rolls an impossible day over (Feb 31 becomes Mar 3): refuse it instead.
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null
  return date
}

/** Show a server calendar day as the same day everywhere (e.g. "Nov 20, 2026"). */
export function formatServerDay(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', year: 'numeric' },
  locales: string | string[] = [],
): string {
  const date = parseServerDay(value)
  return date ? date.toLocaleDateString(locales, options) : '--'
}

/** Whole calendar days from the viewer's today to a server calendar day:
 *  0 is today, 1 tomorrow, -1 yesterday (overdue). Null when the value is not a day. */
export function serverDayOffset(value: string | null | undefined, now: Date = new Date()): number | null {
  const date = parseServerDay(value)
  if (!date) return null
  // Compare the two calendar days on a UTC scale so a daylight-saving change cannot make a day 23 or 25 hours.
  const day = Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.round((day - today) / 86_400_000)
}

/** Format an ISO date string to locale-friendly display (e.g. "Jan 5, 2026, 03:45 PM"). */
export function formatDate(d: string | null | undefined): string {
  if (!d) return '--'
  return parseServerDate(d).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** Format an ISO datetime string to time-only display (e.g. "3:45 PM"). */
export function formatTime(isoString: string | null | undefined): string {
  if (!isoString) return '\u2014'
  const d = parseServerDate(isoString)
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

export function spoolSlotNumbers(printer: { slot_count?: number; api_type?: string | null; filament_slots?: { slot_number: number }[] }, fallback = 0) {
  const configured = Array.from({ length: printer.slot_count || fallback }, (_, i) => i + 1)
  const ht = printer.api_type === 'bambu' ? (printer.filament_slots || []).map(s => s.slot_number)
    .filter(n => n >= AMS_HT_FIRST_SLOT && n <= AMS_LAST_SLOT && !configured.includes(n)) : []
  return [...configured, ...ht.sort((a, b) => a - b)]
}

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

export function isH3C(printer: { api_type?: string | null; model?: string | null; machine_type?: string | null }): boolean {
  return printer.api_type === 'bambu' && [printer.model, printer.machine_type].some(
    value => /^(?:Bambu Lab )?H3C$/i.test(value?.trim() || '')
  )
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

/** Shown wherever a Bambu printer is added: the usual reason a connection fails. */
export const BAMBU_LAN_NOTE = "Current Bambu firmware only accepts ODIN with LAN Only mode and Developer Mode turned on (printer screen: Settings, WLAN). Developer Mode disconnects the printer from Bambu's cloud."

/** What to check when a connection test fails, by printer type. */
export function connectionFailureHelp(apiType: string): string {
  if (apiType === 'bambu') return 'Check that ODIN and the printer are on the same network, that the IP address is the printer\'s current one, that the serial number and access code match the printer screen, and that LAN Only mode and Developer Mode are on.'
  if (apiType === 'moonraker') return 'Check the address (add the port if Moonraker is not on 80, for example 192.168.1.50:7125) and that this ODIN server is allowed in moonraker.conf (trusted_clients).'
  if (apiType === 'prusalink') return 'Check the address and the PrusaLink API key or username|password from the printer\'s settings.'
  if (apiType === 'elegoo') return 'Check the address and that the printer is on the same network as ODIN.'
  return 'Check the address and that the printer is on the same network as ODIN.'
}

/** Machine-readable reasons from POST /printers/test-connection (`reason` on a failed test). */
export const CONNECTION_FAILURE_REASONS = ['unreachable', 'auth', 'not_this_type', 'blocked_address', 'control_disabled', 'unknown'] as const
export type ConnectionFailureReason = typeof CONNECTION_FAILURE_REASONS[number]

function connectionAuthFix(apiType: string): string {
  if (apiType === 'bambu') return 'Re-enter the access code exactly as the printer screen shows it.'
  if (apiType === 'moonraker') return 'Allow this ODIN server in moonraker.conf (trusted_clients).'
  if (apiType === 'prusalink') return 'Re-enter the PrusaLink API key or username|password from the printer\'s settings.'
  if (apiType === 'octoprint') return 'Create a new application key in OctoPrint (Settings, Application Keys) and paste it here.'
  return 'Check the credentials and test again.'
}

/**
 * One plain sentence and one thing to do for a failed connection test.
 * Built only from the reason and printer type: never from the address, serial or credential.
 * "unknown" (or a server that sends no reason) keeps the server's sentence and the per-type checklist.
 */
export function connectionFailureMessage(reason: unknown, apiType: string, serverError?: string | null): string {
  switch (reason) {
    case 'unreachable':
      return 'ODIN could not reach a printer at that address. ' + (apiType === 'moonraker'
        ? 'Check the address, and add the port if Moonraker is not on 80 (for example 192.168.1.50:7125).'
        : 'Check that the printer is on, on the same network as ODIN, and that the address is its current one.')
    case 'auth':
      return `The printer answered but did not accept the credentials. ${connectionAuthFix(apiType)}`
    case 'not_this_type':
      return 'Something answered at that address, but not as the printer type you chose. Check the printer type and the address.'
    case 'blocked_address':
      return 'ODIN does not connect to that address. Enter the printer\'s own address on your local network.'
    case 'control_disabled':
      return 'The printer answered, but it is not accepting control from ODIN. Turn on LAN Only mode and Developer Mode on the printer screen (Settings, WLAN).'
    default:
      return `${serverError || 'The connection test failed and ODIN could not tell why.'} ${connectionFailureHelp(apiType)}`
  }
}

/** Grams for display: one decimal under 10 g (a 0.3 g print is not "0g"), whole grams above. */
export function formatGrams(grams: number | null | undefined): string {
  if (grams == null || !Number.isFinite(grams)) return '\u2014'
  return grams < 10 ? `${Math.round(grams * 10) / 10}g` : `${Math.round(grams)}g`
}
