import type { InstalledNozzle } from '../../types/printer'

export default function InstalledNozzles({ nozzles }: { nozzles?: InstalledNozzle[] | null }) {
  return <section aria-label="Installed nozzles" className="mb-6 rounded-lg border border-[var(--brand-card-border)] p-4">
    <h2 className="font-medium">Installed nozzles</h2>
    {!nozzles?.length ? <p>No nozzle report received. Nozzle compatibility has not been verified.</p> :
      <ul className="mt-2 space-y-3">{nozzles.map(nozzle => <li key={nozzle.extruder}>
        <p className="capitalize">{nozzle.side} nozzle{nozzle.present === false ? ' · absent' : ''}</p>
        <p>{nozzle.diameter == null ? 'Diameter unknown' : `${nozzle.diameter} mm`} · {nozzle.flow || 'Flow unknown'} · {nozzle.material || 'Material unknown'}</p>
        <p>Wear: {nozzle.wear == null ? 'Unknown' : nozzle.wear}</p>
        <p className="text-xs">Last reported: <time dateTime={nozzle.observed_at}>{new Date(nozzle.observed_at).toLocaleString()}</time></p>
      </li>)}</ul>}
    <p className="mt-2 text-xs">Last reported values; dispatch checks a fresh printer report.</p>
  </section>
}
