import { hmsFaults } from './hms'
import { isOnline, isPausedState, isPrintingState } from './shared'
import type { Printer } from '../types'

type ReportedState = { gcode_state?: string | null; state_needs_attention?: boolean }

export function stateNeedsAttention(printer: ReportedState) {
  if (typeof printer.state_needs_attention === 'boolean') return printer.state_needs_attention
  return ['ERROR', 'FAILED', 'RUNOUT'].includes((printer.gcode_state || '').toUpperCase())
}

export function idleAfterFailure(printer: ReportedState) {
  return printer.gcode_state?.toUpperCase() === 'FAILED' && printer.state_needs_attention === false
}

export function needsYou(printer: ReportedState & { hms_errors?: unknown }) {
  // HMS is a JSON string on HTTP and an array on websocket updates. '[]' is not an error.
  let errors: unknown = printer.hms_errors
  if (typeof errors === 'string') { try { errors = JSON.parse(errors) } catch { errors = [] } }
  return isPausedState(printer.gcode_state) || stateNeedsAttention(printer) || hmsFaults(errors).length > 0
}

export function state(printer: Printer) {
  if (!printer.gcode_state || printer.gcode_state.trim().toUpperCase() === 'UNKNOWN') return 'unknown'
  if (!isOnline(printer)) return 'offline'
  if (needsYou(printer)) return 'needs you'
  if (isPrintingState(printer.gcode_state)) return 'printing'
  if (idleAfterFailure(printer)) return 'ready'
  return printer.gcode_state.trim() || 'unknown'
}

const READY_STATES = new Set(['idle', 'ready', 'finish', 'finished', 'complete', 'completed'])

/**
 * One definition of the fleet counts, used by the Foundry header and the sidebar summary.
 * A printer that needs you (paused, errored, runout, HMS fault) is never counted as printing.
 * Deactivated printers are left out here, so every caller counts the same fleet.
 */
export function fleetCounts(all: Printer[]) {
  const printers = all.filter(printer => printer.is_active !== false)
  let printing = 0, ready = 0, attention = 0, offline = 0
  for (const printer of printers) {
    const s = state(printer).toLowerCase()
    if (s === 'printing') printing++
    else if (READY_STATES.has(s)) ready++
    if (needsYou(printer)) attention++
    if (!isOnline(printer)) offline++
  }
  return { printing, ready, attention, offline, total: printers.length }
}
