import { replaceEqualDeep } from '@tanstack/react-query'
import type { Printer } from '../types/printer'

const liveFields = ['mc_percent', 'mc_remaining_time', 'layer_num', 'total_layer_num', 'speed_level', 'speed_percent', 'telemetry_at'] as const

// Server timestamps are UTC. Keep all six fractional digits when comparing two
// samples produced within the same millisecond.
function instant(value?: string | null) {
  return value?.replace(/(?:\.(\d+))?Z$/, (_, fraction = '') => '.' + fraction.padEnd(6, '0')) ?? ''
}

export function mergePrinter(previous: Printer | undefined, incoming: Printer) {
  if (!previous?.telemetry_at || previous.id !== incoming.id ||
      previous.created_at !== incoming.created_at || instant(previous.telemetry_at) < instant(incoming.telemetry_at)) {
    return incoming
  }
  const merged = { ...incoming }
  for (const field of liveFields) Object.assign(merged, { [field]: previous[field] })
  return merged
}

export function sharePrinterData(previous: unknown, incoming: unknown) {
  if (Array.isArray(incoming) && Array.isArray(previous)) {
    const byId = new Map(previous.map(p => [p.id, p]))
    return replaceEqualDeep(previous, incoming.map(p => mergePrinter(byId.get(p.id), p)))
  }
  if (previous && incoming && 'id' in Object(previous) && 'id' in Object(incoming)) {
    return replaceEqualDeep(previous, mergePrinter(previous as Printer, incoming as Printer))
  }
  return replaceEqualDeep(previous, incoming)
}
