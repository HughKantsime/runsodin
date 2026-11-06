import { useEffect, useState } from 'react'
import { educationProgramReports as api, type ProgramFilters, type ProgramReport } from '../../api/educationProgramReports'
import type { ClassRow } from '../../api/educationClasses'
import { Button, Card, Input, Select } from '../ui'

const today = () => new Date().toISOString().slice(0, 10)
export default function ProgramReportForm({ cls, centers = [] }: { cls?: ClassRow; centers?: {id: number; name: string}[] }) {
  const [options, setOptions] = useState({timezone: 'UTC', perkins_enabled: false})
  useEffect(() => { let active = true; api.options().then(value => { if (active) setOptions(value) }).catch(() => {}); return () => { active = false } }, [])
  const [scope, setScope] = useState(cls ? 'class' : 'program')
  const [start, setStart] = useState(today().slice(0, 4) + '-01-01')
  const [end, setEnd] = useState(today())
  const [template, setTemplate] = useState<'generic' | 'perkins'>('generic')
  const [report, setReport] = useState<ProgramReport | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [centerId, setCenterId] = useState(String(centers[0]?.id || ''))
  const [term, setTerm] = useState(cls?.term || '')
  const filters: ProgramFilters = { start_date: start, end_date: end, template,
    ...(scope === 'class' ? { class_id: cls?.id } : scope === 'program' ? { cost_center_id: cls?.cost_center_id ?? (centerId ? Number(centerId) : undefined) } : { term }) }
  const action = async (work: () => Promise<unknown>) => {
    if (pending) return
    setPending(true); setError('')
    try { await work() }
    catch (err) { setError(err instanceof Error ? err.message : 'Unable to complete reporting action') }
    finally { setPending(false) }
  }
  return <Card className="space-y-4">
    <h3 className="text-lg font-semibold">Funding and program reports</h3>
    <p>Programs use cost centers. Program totals include unassigned prints; class and term totals require assignment links. Class participants and concentrators are explicit instructor marks; they are not inferred from printing.</p>
    <div className="grid gap-3 sm:grid-cols-2">
      <Select label="Report scope" value={scope} onChange={e => { setScope(e.target.value); setReport(null) }}>
        {cls && <option value="class">This class</option>}<option value="program">Program (cost center)</option><option value="term">Term across classes I manage</option>
      </Select>
      {!cls && scope === 'program' && <Select label="Program" value={centerId} onChange={e => { setCenterId(e.target.value); setReport(null) }}><option value="">All programs I manage</option>{centers.map(c => <option value={c.id} key={c.id}>{c.name}</option>)}</Select>}
      {scope === 'term' && <Input label="Reporting term" value={term} onChange={e => { setTerm(e.target.value); setReport(null) }} />}
      <Select label="Report template" value={template} onChange={e => { setTemplate(e.target.value as 'generic' | 'perkins'); setReport(null) }}>
        <option value="generic">Generic funding report</option>{options.perkins_enabled && <option value="perkins">Perkins V draft worksheet</option>}
      </Select>
      <Input label={`Reporting start (${options.timezone})`} type="date" value={start} onChange={e => { setStart(e.target.value); setReport(null) }} />
      <Input label={`Reporting end (${options.timezone})`} type="date" value={end} onChange={e => { setEnd(e.target.value); setReport(null) }} />
    </div>
    {template === 'perkins' && <p>Draft only; field definitions not yet confirmed.</p>}
    <div className="flex flex-wrap gap-2">
      <Button disabled={pending || !start || !end || (scope === 'term' && !term.trim()) || (template === 'perkins' && scope === 'program')} onClick={() => action(async () => setReport(await api.get(filters)))}>Generate report</Button>
      <Button disabled={pending || !start || !end || (scope === 'term' && !term.trim()) || (template === 'perkins' && scope === 'program')} onClick={() => action(() => api.download(filters, 'csv'))}>Download CSV</Button>
      <Button disabled={pending || !start || !end || (scope === 'term' && !term.trim()) || (template === 'perkins' && scope === 'program')} onClick={() => action(() => api.download(filters, 'pdf'))}>Download PDF</Button>
    </div>
    {error && <p role="alert">{error}</p>}
    {report && <section aria-label="Program report results" className="space-y-3">
      <p>{report.notice}</p><p>Cost currency: {report.currency || 'Unknown'}</p>
      <dl className="grid gap-2 sm:grid-cols-2">{Object.entries(report.summary).map(([key, value]) => <div key={key}><dt>{report.metric_labels?.[key] || key.replace(/_/g, ' ')}</dt><dd>{value ?? 'Unknown'}</dd></div>)}</dl>
      <details><summary>How these numbers are calculated</summary>{Object.entries(report.basis).map(([key, value]) => <p key={key}>{key.replace(/_/g, ' ')}: {value}</p>)}</details>
      <p>Class detail is included in CSV and PDF. Ask a platform administrator to schedule email under Settings → Scheduled Reports.</p>
    </section>}
  </Card>
}
