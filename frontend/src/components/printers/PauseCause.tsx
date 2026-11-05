export default function PauseCause({ printer }: { printer: { gcode_state?: string | null; print_stage?: string | null } }) {
  if (printer.gcode_state !== 'PAUSE') return null
  return <p role="status" className="px-3 py-2 text-sm text-amber-500">{printer.print_stage?.startsWith('Paused: ') ? printer.print_stage : 'Paused: Cause unavailable'}</p>
}
