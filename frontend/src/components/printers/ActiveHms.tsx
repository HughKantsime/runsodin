import { hmsEvents, hmsFaults, type HmsEvent } from '../../utils/hms'

function HmsGroup({ entries, title }: { entries: HmsEvent[]; title: string }) {
  if (!entries.length) return null
  return <div>
    <h2 className="font-semibold">{title}</h2>
    <ul>{entries.map((e, i) => <li key={e?.code ?? i}>
      {e?.message || e?.code || 'Unrecognized printer code'} {e?.wiki_url && <a className="underline" href={e.wiki_url} target="_blank" rel="noreferrer">Bambu troubleshooting</a>}
    </li>)}</ul>
  </div>
}

export default function ActiveHms({ printer, noticesOnly = false }) {
  if (printer.api_type !== 'bambu') return null
  const events = [...new Map(hmsEvents(printer.hms_errors).map((e, i) => [e?.code ?? i, e])).values()]
  const faults = hmsFaults(events)
  const notices = <HmsGroup entries={events.filter(e => !faults.includes(e))} title="Printer notices" />
  if (noticesOnly) return notices
  return <section className="my-4 space-y-2" aria-label="Printer health">
    <HmsGroup entries={faults} title="Active faults" />
    {notices}
    {!events.length && <p>No active printer notices.</p>}
    {printer.last_error_code && <details><summary>Last seen code (history)</summary>
      <p>{printer.last_error_code}{printer.last_error_at ? ` — ${printer.last_error_at}` : ''}</p>
      <p>This is historical and does not indicate a current fault.</p>
    </details>}
  </section>
}
