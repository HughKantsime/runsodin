import { hmsEvents, hmsFaults } from '../../utils/hms'

export default function ActiveHms({ printer }) {
  if (printer.api_type !== 'bambu') return null
  const events = [...new Map(hmsEvents(printer.hms_errors).map(e => [e.code, e])).values()]
  const faults = hmsFaults(events)
  const notices = events.filter(e => !faults.includes(e))
  return <section className="my-4 space-y-2" aria-label="Printer health">
    {[[faults, 'Active faults'], [notices, 'Maintenance due / notices']] .map(([entries, title]) =>
      (entries as typeof events).length > 0 && <div key={title as string}>
        <h2 className="font-semibold">{title as string}</h2>
        <ul>{(entries as typeof events).map(e => <li key={e.code}>
          {e.message || e.code} {e.wiki_url && <a className="underline" href={e.wiki_url} target="_blank" rel="noreferrer">Bambu troubleshooting</a>}
        </li>)}</ul>
      </div>)}
    {!events.length && <p>No active printer notices.</p>}
    {printer.last_error_code && <details><summary>Last seen code (history)</summary>
      <p>{printer.last_error_code}{printer.last_error_at ? ` — ${printer.last_error_at}` : ''}</p>
      <p>This is historical and does not indicate a current fault.</p>
    </details>}
  </section>
}
