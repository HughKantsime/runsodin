import Table from '../ui/Table'
import { Link } from 'react-router-dom'
import type { Printer, PrintJob, Job } from '../../types'
import StatusBadge from '../ui/StatusBadge'
import PrinterCard, { PrintTelemetry, Temperatures } from './PrinterCard'
import { activePrint, needsYou, nextJob, state } from './data'
import { displayName } from '../../utils/displayName'
import { isPrintingState } from '../../utils/shared'
import { useDensity } from './DensityToggle'

export default function PrinterRows({ printers, prints = [], queue = [], now }: { printers: Printer[]; prints?: PrintJob[]; queue?: Job[]; now?: number }) {
  return <div className="f-table-scroll"><Table className="f-table"><caption className="sr-only">Printer fleet</caption><thead><tr>{['Printer', 'State', 'Job', 'Progress', 'Temps'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>
    {printers.map(printer => { const job = activePrint(printer, prints); const next = nextJob(printer, queue)
      return <tr key={printer.id}><th scope="row"><Link to={`/printers/${printer.id}`}>{printer.nickname || printer.name}</Link></th><td><StatusBadge status={state(printer)} />{needsYou(printer) && <Link className="f-action" to={`/#needs-you-${printer.id}`}>Resolve</Link>}</td><td>{job ? displayName(job) : next ? `Next: ${displayName(next)}` : '—'}</td><td><PrintTelemetry job={job} now={now} printing={isPrintingState(printer.gcode_state)} /></td><td><Temperatures printer={printer} /></td></tr>
    })}
  </tbody></Table></div>
}
export function Fleet({ printers, prints = [], queue = [], now }: { printers: Printer[]; prints?: PrintJob[]; queue?: Job[]; now?: number }) {
  const [density] = useDensity()
  return <div data-density={density} className="f-fleet"><div className="f-comfortable f-card-grid">{printers.map(printer => <PrinterCard key={printer.id} printer={printer} job={activePrint(printer, prints)} next={nextJob(printer, queue)} now={now} />)}</div><div className="f-compact"><PrinterRows printers={printers} prints={prints} queue={queue} now={now} /></div></div>
}
