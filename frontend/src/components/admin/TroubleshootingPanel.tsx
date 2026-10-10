import { useEffect, useState } from 'react'
import { fetchAPI } from '../../api'
import { Button, Card } from '../ui'

type Check = { stage: string; status: string; message: string; next_step: string; details: Record<string, unknown> }
type Result = { diagnostic_id: string; checked_at: string; checks: Check[] }
type Report = Result & { included: string[]; [key: string]: unknown }

function Results({ result }: { result: Result | null }) {
  if (!result) return null
  return <div className="space-y-3 mt-4">
    <p className="text-xs text-[var(--brand-text-muted)]">Checked {new Date(result.checked_at).toLocaleString()} · ID {result.diagnostic_id}</p>
    {result.checks.map((item, i) => <div key={`${item.stage}-${i}`} className="border border-[var(--brand-card-border)] rounded-md p-3">
      <p className="font-semibold">{item.stage.replaceAll('_', ' ')}: {item.status}</p>
      <p>{item.message}</p>
      {item.next_step && <p className="text-sm mt-1">Next: {item.next_step}</p>}
      {Object.keys(item.details).length > 0 && <dl className="text-xs mt-2 break-words">{Object.entries(item.details).map(([key, value]) => <div key={key}><dt className="inline">{key.replaceAll('_', ' ')}: </dt><dd className="inline">{typeof value === 'number' && key.endsWith('_bytes') ? `${(value / 1024 ** 3).toFixed(2)} GiB` : String(value)}</dd></div>)}</dl>}
    </div>)}
    <p className="text-sm text-[var(--brand-text-muted)]">These checks do not verify a physical print.</p>
  </div>
}

export default function TroubleshootingPanel() {
  const [printers, setPrinters] = useState<Array<{ id: number; name: string; api_type: string }>>([])
  const [printerId, setPrinterId] = useState('')
  const [server, setServer] = useState<Result | null>(null)
  const [printer, setPrinter] = useState<Result | null>(null)
  const [transfer, setTransfer] = useState<Result | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [problem, setProblem] = useState('')
  const [logs, setLogs] = useState(false)
  const [report, setReport] = useState<Report | null>(null)
  useEffect(() => { fetchAPI<Array<{ id: number; name: string; api_type: string }>>('/printers').then(setPrinters).catch(() => setError('Could not load saved printers. Reload this page to try again.')) }, [])
  const selected = printers.find(p => String(p.id) === printerId)
  async function run(kind: string) {
    setBusy(kind); setError(''); setReport(null)
    if (kind === 'server') setServer(null)
    else if (kind === 'transfer') setTransfer(null)
    else setPrinter(null)
    try {
      const result = await fetchAPI<Result>(`/system/troubleshooting/${kind === 'server' ? 'server' : 'printer'}`, {
        method: 'POST', body: JSON.stringify(kind === 'server' ? {} : { printer_id: Number(printerId), transfer: kind === 'transfer', confirm_transfer: confirmed }),
      })
      if (kind === 'server') setServer(result)
      else if (kind === 'transfer') setTransfer(result)
      else setPrinter(result)
    } catch (e) { setError(e instanceof Error ? e.message : 'Diagnostic could not run. Try again or contact support.') }
    finally { setBusy('') }
  }
  async function createReport() {
    setBusy('report'); setError(''); setReport(null)
    try {
      const result = await fetchAPI<Report>('/system/troubleshooting/report', { method: 'POST', body: JSON.stringify({ problem, include_logs: logs }) })
      setReport({ ...result, included: [...(result.included || []), ...(printer ? ['latest selected-printer checks'] : []), ...(transfer ? ['latest selected-printer transfer checks'] : [])], printer_checks: printer, transfer_checks: transfer })
    } catch (e) { setError(e instanceof Error ? e.message : 'Support report could not be created.') }
    finally { setBusy('') }
  }
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
    const a = document.createElement('a'); a.href = url; a.download = `odin-support-${report.diagnostic_id}.json`; a.click(); URL.revokeObjectURL(url)
  }
  return <Card padding="lg" className="mb-4 md:mb-6">
    <h2 className="text-lg font-semibold">Troubleshooting</h2>
    <p className="text-sm mt-2">Run checks, review the results, and prepare a report for support. No check starts a print.</p>
    {error && <p role="alert" className="mt-3 text-red-500">{error}</p>}
    <div aria-live="polite" aria-busy={Boolean(busy)}>
      <section className="mt-5"><h3 className="font-semibold">Server</h3>
        <Button disabled={Boolean(busy)} onClick={() => run('server')}>{busy === 'server' ? 'Checking server…' : 'Check server'}</Button>
        <Results result={server}/>
      </section>
      <section className="mt-5"><h3 className="font-semibold">Saved printer</h3>
        <label htmlFor="diagnostic-printer" className="block text-sm mb-1">Printer</label>
        <select id="diagnostic-printer" className="w-full rounded-md p-2 bg-[var(--brand-input-bg)]" value={printerId} disabled={Boolean(busy)} onChange={e => { setPrinterId(e.target.value); setPrinter(null); setTransfer(null); setConfirmed(false); setReport(null) }}>
          <option value="">Choose a printer</option>{printers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        {!printers.length && <p className="text-sm mt-2">Add a printer in Printers before checking its connection.</p>}
        <div className="mt-3"><Button disabled={Boolean(busy) || !printerId} onClick={() => run('printer')}>{busy === 'printer' ? 'Checking printer…' : 'Check printer'}</Button></div>
        <Results result={printer}/>
        <div className="mt-4">
          <p className="text-sm">Bambu LAN transfer test: uploads a small text file, downloads it to verify the bytes, then removes it. Printer certificates use the existing LAN trust policy.</p>
          <label className="flex gap-2 mt-2 text-sm"><input type="checkbox" checked={confirmed} disabled={Boolean(busy) || selected?.api_type !== 'bambu'} onChange={e => setConfirmed(e.target.checked)}/>I authorize this temporary file transfer to the selected printer.</label>
          <Button disabled={Boolean(busy) || !confirmed || selected?.api_type !== 'bambu'} onClick={() => run('transfer')}>{busy === 'transfer' ? 'Testing transfer…' : 'Test file transfer'}</Button>
          {selected && selected.api_type !== 'bambu' && <p className="text-sm">A safe transfer test is not available for this printer protocol.</p>}
          <Results result={transfer}/>
        </div>
      </section>
      <section className="mt-5"><h3 className="font-semibold">Support report</h3>
        <label htmlFor="diagnostic-problem" className="block text-sm">What happened? Avoid names, passwords and student information.</label>
        <textarea id="diagnostic-problem" maxLength={2000} className="w-full rounded-md p-2 bg-[var(--brand-input-bg)]" value={problem} disabled={Boolean(busy)} onChange={e => { setProblem(e.target.value); setReport(null) }}/>
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={logs} disabled={Boolean(busy)} onChange={e => { setLogs(e.target.checked); setReport(null) }}/>Include recent filtered logs. Review them for personal information before sharing.</label>
        <div className="mt-3"><Button disabled={Boolean(busy)} onClick={createReport}>{busy === 'report' ? 'Preparing report…' : 'Preview support report'}</Button></div>
        {report && <div className="mt-3">
          <p className="text-sm">Review the full report below. Nothing has been sent.</p>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all text-xs p-3 bg-[var(--brand-input-bg)]" tabIndex={0} aria-label="Support report preview">{JSON.stringify(report, null, 2)}</pre>
          <div className="flex gap-3 flex-wrap mt-3"><Button onClick={download}>Download report</Button>
            <a className="underline p-2" href={`mailto:support@runsodin.com?subject=${encodeURIComponent('ODIN support report '+report.diagnostic_id)}&body=${encodeURIComponent('Hi ODIN Support,\n\nI have a diagnostic report: '+report.diagnostic_id+'\nChecked: '+report.checked_at+'\n\nI will attach the downloaded report after reviewing it.\n')}`}>Open email draft</a></div>
          <p className="text-sm mt-2">Download and attach the report yourself. The email link opens your mail app; it does not attach a file or send anything.</p>
        </div>}
      </section>
    </div>
  </Card>
}
