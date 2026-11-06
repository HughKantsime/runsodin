import { useState } from 'react'
import { useLicense } from '../../LicenseContext'
import { canDo } from '../../permissions'
import { traceability, type TraceabilityRecord, type InspectionPolicy } from '../../api/traceability'

export default function TraceabilityPanel() {
  const { hasFeature } = useLicense()
  const [jobId, setJobId] = useState('')
  const [record, setRecord] = useState<TraceabilityRecord | null>(null)
  const [notes, setNotes] = useState('')
  const [result, setResult] = useState<'pass' | 'fail'>('pass')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [orgId, setOrgId] = useState('')
  const [target, setTarget] = useState('')
  const [kind, setKind] = useState<'printer_tag' | 'product'>('printer_tag')
  const [required, setRequired] = useState(true)
  const [policies, setPolicies] = useState<InspectionPolicy[] | null>(null)
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError('')
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : 'Request failed') }
    finally { setBusy(false) }
  }
  if (!hasFeature('traceability') || !canDo('jobs.traceability')) return null
  const input = 'rounded border border-[var(--brand-card-border)] bg-[var(--brand-input-bg)] p-2'
  const button = 'rounded border border-[var(--brand-card-border)] px-3 py-2 disabled:opacity-50'
  return <details className="my-4 rounded border border-[var(--brand-card-border)] p-4">
    <summary className="cursor-pointer font-semibold">Part traceability &amp; certificates</summary>
    <div className="mt-3 space-y-4">
      <p className="text-sm">Look up a completed job, record an inspection, or export a signed certificate. Unknown evidence stays marked as not recorded.</p>
      <form className="flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); setRecord(null); void run(async () => { const r = await traceability.get(Number(jobId)); setRecord(r); setNotes(r.inspection.inspection_notes || ''); setResult(r.inspection.inspection_result || 'pass') }) }}>
        <label>Job ID <input aria-label="Traceability job ID" type="number" min="1" required className={input} value={jobId} onChange={e => { setJobId(e.target.value); setRecord(null) }} /></label>
        <button className={button} disabled={busy}>Load record</button>
      </form>
      {error && <p role="alert" className="text-red-400">{error}</p>}
      {busy && <p role="status">Working…</p>}
      {record && <section aria-label="Part traceability record" className="space-y-3">
        <h3 className="font-semibold">Job {record.job_id} · Attempt {record.attempt}</h3>
        <p>File: {record.file.name || 'Not recorded'}</p>
        <p className="break-all">SHA-256: {record.file.sha256 || 'Not recorded'}</p>
        <p>Inspection: {record.inspection.inspection_result || 'Not inspected'}{record.inspection_required ? ' (required)' : ' (optional)'}</p>
        <p>Delivery: {record.delivery.delivered_at || 'Not delivered'}</p>
        <div className="flex flex-wrap gap-2">
          <button className={button} disabled={busy} onClick={() => void run(() => traceability.certificate(record.job_id))}>Download signed certificate</button>
          {canDo('jobs.deliver') && <button className={button} disabled={busy || !!record.delivery.delivered_at || record.inspection.inspection_result === 'fail' || (record.inspection_required && record.inspection.inspection_result !== 'pass')} onClick={() => void run(async () => setRecord(await traceability.deliver(record.job_id)))}>Mark delivered</button>}
        </div>
        {canDo('jobs.inspect') && !record.delivery.delivered_at && <form className="space-y-2" onSubmit={e => { e.preventDefault(); void run(async () => setRecord(await traceability.inspect(record.job_id, result, notes))) }}>
          <label>Inspection result <select className={input} value={result} onChange={e => setResult(e.target.value as 'pass' | 'fail')}><option value="pass">Pass</option><option value="fail">Fail</option></select></label>
          <label className="block">Inspection notes<textarea className={`${input} block w-full`} maxLength={4000} value={notes} onChange={e => setNotes(e.target.value)} /></label>
          <button className={button} disabled={busy}>Save inspection</button>
        </form>}
        <details><summary>Full evidence and audit trail</summary><pre className="whitespace-pre-wrap break-all text-xs max-h-96 overflow-auto">{JSON.stringify(record, null, 2)}</pre></details>
      </section>}
      <form className="flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); void run(() => traceability.batch(start, end)) }}>
        <label>From (UTC)<input type="date" required className={`${input} block`} value={start} onChange={e => setStart(e.target.value)} /></label>
        <label>Until (exclusive, UTC)<input type="date" required className={`${input} block`} value={end} min={start} onChange={e => setEnd(e.target.value)} /></label>
        <button className={button} disabled={busy || !start || !end || start >= end}>Export date range</button>
      </form>
      {canDo('jobs.inspection_policy') && <details><summary>Inspection policies</summary>
        <p className="text-sm">Require a passing inspection for jobs matching a printer tag or an order item’s product. Either policy can require inspection.</p>
        <form className="flex flex-wrap items-end gap-2 mt-2" onSubmit={e => { e.preventDefault(); void run(async () => { await traceability.policy({ org_id: Number(orgId), target_kind: kind, target_key: target, required }); setPolicies(await traceability.policies(Number(orgId))) }) }}>
          <label>Organization ID (0 for platform)<input className={`${input} block`} type="number" min="0" required value={orgId} onChange={e => { setOrgId(e.target.value); setPolicies(null) }} /></label>
          <label>Policy target<select className={`${input} block`} value={kind} onChange={e => setKind(e.target.value as typeof kind)}><option value="printer_tag">Printer tag</option><option value="product">Product ID</option></select></label>
          <label>Tag or product ID<input className={`${input} block`} required maxLength={100} value={target} onChange={e => setTarget(e.target.value)} /></label>
          <label><input type="checkbox" checked={required} onChange={e => setRequired(e.target.checked)} /> Inspection required</label>
          <button className={button} disabled={busy}>Save policy</button>
          <button type="button" className={button} disabled={busy || !orgId} onClick={() => void run(async () => setPolicies(await traceability.policies(Number(orgId))))}>Load policies</button>
        </form>
        {policies && <ul>{policies.map(p => <li key={`${p.target_kind}:${p.target_key}`}>{p.target_kind === 'product' ? 'Product' : 'Printer tag'} {p.target_key}: {p.required ? 'required' : 'optional'}</li>)}</ul>}
      </details>}
    </div>
  </details>
}
