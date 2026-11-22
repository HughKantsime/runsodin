import { isH3C } from './shared'

export type SupportLevel = 'supported' | 'experimental' | 'unsupported'

// Mirror of backend/modules/printers/support_levels.py SUPPORT_TABLE. A backend contract
// test (test_printer_support_levels.py) fails if the two differ. One `key: 'level',` per line.
export const SUPPORT_LEVELS: Record<string, SupportLevel> = {
  bambu: 'supported',
  moonraker: 'supported',
  prusalink: 'supported',
  elegoo: 'supported',
  anycubic_lan: 'supported',
  octoprint: 'experimental',
  ultimaker: 'experimental',
}

const OCTOPRINT_WARNING = "Built from OctoPrint's public API and not yet verified on real hardware. Sending jobs and controls may not work as expected. Send printer diagnostics if something is wrong."
const GENERIC_WARNING = 'Built from public documentation and not yet verified on real hardware. Sending jobs and controls may not work as expected. Send printer diagnostics if something is wrong.'

export const CAPABILITY_COPY: Record<string, { note: string; state: (enabled: boolean) => string; label: string }> = {
  h3c: {
    note: 'H3C monitoring and filament tracking are Supported. Job sending and printer controls are Experimental and have not yet been verified on a printer.',
    state: enabled => enabled ? 'Job sending and controls are on for this printer.' : 'Job sending and controls are off. A platform administrator can turn them on in Experimental features. Pause and stop are always available.',
    label: 'Allow job sending and controls (Experimental)',
  },
  dispatch: {
    note: "Job sending (Experimental): built from Elegoo's public SDCP documentation and not yet verified on a real printer.",
    state: enabled => `Job sending is ${enabled ? 'on' : 'off'} for this printer. Monitoring, pause, resume, speed and stop are not affected.`,
    label: 'Allow job sending (Experimental)',
  },
}

// Mirror of support_levels.GATED_CAPABILITIES; these types remain Supported overall.
export const GATED_CAPABILITIES: Record<string, readonly string[]> = { elegoo: ['dispatch'], h3c: ['dispatch', 'controls'] }

export function capabilityKey(printer: Parameters<typeof isH3C>[0] & { unidentified_rack?: boolean }): string {
  return printer.api_type === 'bambu' && isH3C(printer) && !printer.unidentified_rack ? 'h3c' : (printer.api_type ?? '').trim().toLowerCase()
}

export function optInApplies(apiType?: string | null): boolean {
  const key = (apiType ?? '').trim().toLowerCase()
  return !isMonitorOnlyType(key) && (supportLevelOfType(key) === 'experimental' || !!GATED_CAPABILITIES[key]?.length)
}

const ULTIMAKER_WARNING = "Built from Ultimaker's public local API and not yet verified on real hardware. Monitoring and the camera only for now: sending jobs and controls are not available for this printer type yet. Send printer diagnostics if something is wrong."

// Mirror of backend support_levels.MONITOR_ONLY: Experimental types with no job sending or controls yet.
export const MONITOR_ONLY_TYPES: ReadonlySet<string> = new Set(['ultimaker'])

export function isMonitorOnlyType(apiType?: string | null): boolean {
  return MONITOR_ONLY_TYPES.has((apiType ?? '').trim().toLowerCase())
}

export function experimentalWarning(apiType?: string | null): string {
  const key = (apiType ?? '').trim().toLowerCase()
  if (key === 'ultimaker') return ULTIMAKER_WARNING
  return key === 'octoprint' ? OCTOPRINT_WARNING : GENERIC_WARNING
}

/** Level for a type; null for a manual printer, 'unsupported' for an unknown type. */
export function supportLevelOfType(apiType?: string | null): SupportLevel | null {
  const key = (apiType ?? '').trim().toLowerCase()
  if (!key) return null
  return SUPPORT_LEVELS[key] ?? 'unsupported'
}

/** The server's level when it sent one, otherwise the table. */
export function supportLevelOfPrinter(printer?: { support_level?: unknown; api_type?: string | null } | null): SupportLevel | null {
  const reported = printer?.support_level
  if (reported === 'supported' || reported === 'experimental' || reported === 'unsupported') return reported
  return supportLevelOfType(printer?.api_type)
}

export interface PrinterTypeOption { value: string; label: string; disabled?: boolean }

export const PRINTER_TYPE_OPTIONS: PrinterTypeOption[] = [
  { value: 'bambu', label: 'Bambu Lab (X1C, P1S, A1, etc.)' },
  { value: 'moonraker', label: 'Klipper / Moonraker' },
  { value: 'prusalink', label: 'Prusa (PrusaLink)' },
  { value: 'elegoo', label: 'Elegoo (SDCP)' },
  { value: 'anycubic_lan', label: 'Anycubic LAN (monitor only)' },
  { value: 'octoprint', label: 'OctoPrint' },
  { value: 'ultimaker', label: 'Ultimaker' },
]

export function printerTypeLabel(option: PrinterTypeOption): string {
  return supportLevelOfType(option.value) === 'experimental' ? `${option.label} — Experimental` : option.label
}
