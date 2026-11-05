import { createContext, useContext } from 'react'

// A managed surface can show the cause above its controls without duplicating it.
export const PauseCauseSuppressed = createContext(false)

export default function PauseCause({ printer, className = 'px-3 py-2 text-sm text-amber-500' }: { printer: { gcode_state?: string | null; print_stage?: string | null }; className?: string }) {
  const suppressed = useContext(PauseCauseSuppressed)
  if (suppressed || printer.gcode_state !== 'PAUSE') return null
  return <p role="status" className={className}>{printer.print_stage?.startsWith('Paused: ') ? printer.print_stage : 'Paused: Cause unavailable'}</p>
}
