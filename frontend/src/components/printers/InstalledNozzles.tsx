import PrinterNumber from '../ui/PrinterNumber'
import type { InstalledNozzle } from '../../types/printer'
import { parseServerDate } from '../../utils/shared'

export default function InstalledNozzles({ nozzles, rack = false, amsConnected }: {
  nozzles?: InstalledNozzle[] | null
  rack?: boolean
  amsConnected?: boolean | null
}) {
  if (rack) {
    const positions = nozzles?.filter(nozzle => nozzle.side === 'rack')
    return <section aria-label="Nozzle rack" className="rounded-lg border border-[var(--brand-card-border)] p-3">
      <h2 className="font-medium">Nozzle rack · Experimental</h2>
      {positions?.length ? <ul className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
        {positions.map(nozzle => <li key={nozzle.nozzle_id}>
          Position {nozzle.position} · {nozzle.type || 'Type unknown'} · {nozzle.diameter == null ? 'Diameter unknown' : `${nozzle.diameter} mm`}
          {nozzle.present === false && ' · Absent'}
        </li>)}
      </ul> : <p>No rack report received.</p>}
      {amsConnected !== true && <p className="mt-2 text-sm">{amsConnected === false ? 'No AMS connected' : 'AMS status unknown'}</p>}
    </section>
  }
  return <section aria-label="Installed nozzles" className="mb-6 rounded-lg border border-[var(--brand-card-border)] p-4">
    <h2 className="font-medium">Installed nozzles</h2>
    {!nozzles?.length ? <p>No nozzle report received. Nozzle compatibility has not been verified.</p> :
      <ul className="mt-2 space-y-3">{nozzles.map(nozzle => <li key={nozzle.extruder}>
        <p className="capitalize">{nozzle.side} nozzle{nozzle.present === false ? ' · absent' : ''}</p>
        <PrinterNumber as="p">{nozzle.diameter == null ? 'Diameter unknown' : `${nozzle.diameter} mm`} · {nozzle.flow || 'Flow unknown'} · {nozzle.material || 'Material unknown'}</PrinterNumber>
        <PrinterNumber as="p">Wear: {nozzle.wear == null ? 'Unknown' : nozzle.wear}</PrinterNumber>
        <p className="text-xs">Last reported: <time dateTime={nozzle.observed_at}>{parseServerDate(nozzle.observed_at).toLocaleString()}</time></p>
      </li>)}</ul>}
    <p className="mt-2 text-xs">Last reported values; dispatch checks a fresh printer report.</p>
  </section>
}
