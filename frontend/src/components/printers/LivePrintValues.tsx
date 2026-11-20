import { useEffect, useReducer } from 'react'
import PrintProgress from './PrintProgress'
import { isPrintingState } from '../../utils/shared'
import type { Printer } from '../../types/printer'

export const speedModes = ['Silent', 'Standard', 'Sport', 'Ludicrous']

export function useTelemetryAge(timestamp?: string | null) {
  const [, tick] = useReducer(n => n + 1, 0)
  const age = Date.now() - Date.parse(timestamp || '')
  const minutes = age > 60000 ? Math.floor(age / 60000) : 0
  useEffect(() => {
    if (!timestamp) return
    const elapsed = Date.now() - Date.parse(timestamp)
    if (!Number.isFinite(elapsed)) return
    const next = minutes ? (minutes + 1) * 60000 : 60001
    const timer = setTimeout(tick, Math.max(1, next - elapsed))
    return () => clearTimeout(timer)
  }, [timestamp, minutes])
  return minutes ? ` as of ${minutes} min ago` : ''
}

type LivePrinter = Pick<Printer, 'telemetry_at' | 'mc_percent' | 'mc_remaining_time' | 'speed_percent' | 'speed_level' | 'api_type'> &
  Partial<Pick<Printer, 'gcode_state' | 'layer_num' | 'total_layer_num'>>

export default function LivePrintValues({ printer, foundry = false, layers = foundry }: { printer: LivePrinter; foundry?: boolean; layers?: boolean }) {
  const age = useTelemetryAge(printer.telemetry_at)
  const speed = printer.speed_percent
  const mode = speedModes[(printer.speed_level ?? 0) - 1]
  const tone = age ? 'text-[var(--brand-text-muted)]' : 'text-[var(--brand-text-secondary)]'
  return <>
    <PrintProgress percent={printer.mc_percent} remainingMinutes={printer.mc_remaining_time} age={age} />
    {layers && printer.layer_num != null && printer.total_layer_num != null &&
      <p className={`f-mono ${tone}`}>Layer {printer.layer_num}/{printer.total_layer_num}{age}</p>}
    {foundry && !age && printer.telemetry_at && printer.mc_remaining_time != null && isPrintingState(printer.gcode_state) &&
      <p className="f-mono">Est. done {new Date(Date.parse(printer.telemetry_at) + printer.mc_remaining_time * 60000)
        .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</p>}
    <p className={`printer-number text-xs ${tone} [overflow-wrap:anywhere]`}>
      {typeof speed === 'number' && Number.isFinite(speed) && speed >= 0 ? `Last reported speed: ${speed}%${age}` : 'Reported speed unknown'}
    </p>
    {printer.api_type === 'bambu' && <p className={`text-xs ${tone} [overflow-wrap:anywhere]`}>
      {mode ? `Last reported mode: ${mode}${age}` : 'Printer speed mode unknown'}
    </p>}
  </>
}
