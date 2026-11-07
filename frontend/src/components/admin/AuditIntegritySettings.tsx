import { controlledDownload } from '../../api/controlledDownload'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLicense } from '../../LicenseContext'
import { getCurrentUser } from '../../permissions'
import { auditIntegrity, DestinationInput, SiemDestination } from '../../api/auditIntegrity'

const empty: DestinationInput = { name: '', kind: 'webhook', endpoint: '', enabled: false, tls: true }

export default function AuditIntegritySettings() {
  const license = useLicense()
  const user = getCurrentUser()
  const allowed = user?.role === 'admin' && !user?.group_id
  if (!allowed) return null
  if (!license.hasFeature('siem_stream') || !license.hasFeature('audit_integrity')) {
    return <section className="mt-8"><h2>Enterprise audit security</h2><p>Tamper-evident audit verification, signed exports and SIEM streaming require Enterprise.</p></section>
  }
  return <Controls />
}

function Controls() {
  const cache = useQueryClient()
  const [editing, setEditing] = useState('new')
  const [form, setForm] = useState<DestinationInput>(empty)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [message, setMessage] = useState('')
  const [restoreReason, setRestoreReason] = useState('')
  const [keyringReason, setKeyringReason] = useState('')
  const list = useQuery({ queryKey: ['siem'], queryFn: auditIntegrity.list, refetchInterval: 30000 })
  const verify = useMutation({ mutationFn: auditIntegrity.verify })
  const initialize = useMutation({ mutationFn: () => auditIntegrity.initializeKeyring(keyringReason), onSuccess: () => {
    setMessage('Audit keyring initialized and alert recorded. Earlier evidence remains unverified.')
    setKeyringReason(''); verify.mutate()
  } })
  const accept = useMutation({ mutationFn: () => auditIntegrity.acceptRestoreBaseline(restoreReason), onSuccess: () => {
    setMessage('Restore baseline accepted and recorded. Previous signed heads remain in history.')
    setRestoreReason(''); verify.mutate()
  } })
  const save = useMutation({ mutationFn: () => auditIntegrity.save(editing, form), onSuccess: () => {
    setForm(empty); setEditing('new'); setMessage('Destination saved.'); cache.invalidateQueries({ queryKey: ['siem'] })
  } })
  const remove = useMutation({ mutationFn: auditIntegrity.remove, onSuccess: () => cache.invalidateQueries({ queryKey: ['siem'] }) })
  function edit(d: SiemDestination) {
    setEditing(d.id); setForm({ name: d.name, kind: d.kind, endpoint: d.endpoint, enabled: !!d.enabled, tls: !!d.tls, ca_bundle: d.ca_bundle })
  }
  return <section className="mt-8 space-y-4" aria-label="Audit security">
    <h2 className="text-lg font-semibold">Audit integrity & SIEM</h2>
    <p>Daily verification detects changed or missing audit records. Retention leaves authenticated checkpoints; erasures leave authenticated receipts.</p>
    <button className="btn btn-secondary" onClick={() => verify.mutate()} disabled={verify.isPending}>Verify audit chain</button>
    {verify.data?.status === 'audit_key_missing' && <form className="space-y-2" onSubmit={e => {
      e.preventDefault(); initialize.mutate()
    }}>
      <p role="alert">Audit signing keys are missing. Recover the original .audit-hmac-keys.json and .audit-signed-head.json from backup when available.</p>
      <p>If the original keys cannot be restored, initialize replacement keys. This records an alert and leaves earlier evidence unverified.</p>
      <label>Keyring initialization reason<input required minLength={10} maxLength={1000} value={keyringReason} onChange={e => setKeyringReason(e.target.value)} /></label>
      <button type="submit" disabled={initialize.isPending || keyringReason.trim().length < 10}>Initialize audit keyring</button>
    </form>}
    {verify.data && verify.data.status !== 'audit_key_missing' && <p role="status">{verify.data.ok ? `Verified ${verify.data.rows} rows.` : `Integrity failure at row ${verify.data.first_break ?? 'chain head'} (${verify.data.reason}).`}</p>}
    {!!verify.data?.breaks?.length && <ul aria-label="Integrity breaks">{verify.data.breaks.map(b => <li key={b.fingerprint}>Row {b.row_id ?? 'chain evidence'}: {b.reason} ({b.known ? 'known' : 'new'})</li>)}</ul>}
    {verify.data?.breaks?.some(b => b.reason.startsWith('signed_head') || b.reason.startsWith('status_')) && <form onSubmit={e => {
      e.preventDefault(); accept.mutate()
    }} className="space-y-2">
      <p>After investigating a restore, accept this database as the new baseline. Previous breaks and signed heads remain recorded. Row corruption cannot be accepted.</p>
      <label>Restore acceptance reason<input required minLength={10} maxLength={1000} value={restoreReason} onChange={e => setRestoreReason(e.target.value)} /></label>
      <button type="submit" disabled={accept.isPending || restoreReason.trim().length < 10}>Accept restore baseline</button>
    </form>}
    <div className="flex gap-3 flex-wrap items-end">
      <label>Export from<input aria-label="Export from" type="date" value={from} onChange={e => setFrom(e.target.value)} /></label>
      <label>Export through<input aria-label="Export through" type="date" value={to} onChange={e => setTo(e.target.value)} /></label>
      {from && to && from <= to && <a href={`/api/export/audit-logs/jsonl?date_from=${from}&date_to=${to}&bundle=true`} className="btn btn-secondary" onClick={async e => {
        e.preventDefault()
        try {
          const response = await controlledDownload(e.currentTarget.href, 'signed audit export')
          if (!response.ok) throw new Error('Signed audit export failed')
          const url = URL.createObjectURL(await response.blob())
          const link = document.createElement('a'); link.href = url; link.download = 'audit-export.zip'; link.click()
          setTimeout(() => URL.revokeObjectURL(url), 1000)
        } catch (error) { setMessage((error as Error).message) }
      }}>Download signed JSON Lines</a>}
    </div>
    <p className="text-sm">The download includes JSON Lines, the detached signature, public key and signed manifest in one ZIP.</p>
    <h3 className="font-medium">SIEM destinations</h3>
    {list.isPending && <p>Loading destinations…</p>}
    {list.data?.length === 0 && <p>No destinations configured. Streaming is disabled until you add and enable one.</p>}
    {list.data?.map(d => <div key={d.id} className="border rounded p-3 space-y-2">
      <p>{d.name} · {d.enabled ? 'Enabled' : 'Disabled'} · {d.pending} pending</p>
      <p>Last success: {d.last_success || 'Never'} · Last error: {d.last_error || 'None'}</p>
      <button onClick={() => edit(d)}>Edit {d.name}</button>{' '}
      <button disabled={remove.isPending} onClick={() => {
        if (window.confirm('Delete this destination and discard its pending deliveries?')) remove.mutate(d.id)
      }}>Delete {d.name}</button>
    </div>)}
    <form className="space-y-3" onSubmit={e => { e.preventDefault(); setMessage(''); save.mutate() }}>
      <h3>{editing === 'new' ? 'Add destination' : 'Edit destination'}</h3>
      <label className="block">Name<input required maxLength={100} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
      <label className="block">Transport<select value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value as DestinationInput['kind'], endpoint: '', secret: undefined })}>
        <option value="webhook">HTTPS webhook</option><option value="syslog">Syslog TCP (RFC 5424)</option>
      </select></label>
      <label className="block">Endpoint<input required placeholder={form.kind === 'webhook' ? 'https://siem.example/events' : 'syslog://siem.example:6514'} value={form.endpoint} onChange={e => setForm({ ...form, endpoint: e.target.value })} /></label>
      {form.kind === 'webhook' ? <label className="block">HMAC secret (32+ characters; leave blank to keep existing)<input type="password" autoComplete="new-password" minLength={32} value={form.secret || ''} onChange={e => setForm({ ...form, secret: e.target.value || undefined })} /></label>
        : <label className="block"><input type="checkbox" checked={form.tls} onChange={e => setForm({ ...form, tls: e.target.checked })} /> Use TLS with certificate verification</label>}
      {(form.kind === 'webhook' || form.tls) && <label className="block">Private CA bundle (optional PEM)<textarea value={form.ca_bundle || ''} onChange={e => setForm({ ...form, ca_bundle: e.target.value || null })} /></label>}
      <p className="text-sm">A custom CA bundle replaces the default trust store for both HTTPS and syslog TLS.</p>
      <label className="block"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} /> Enable delivery of new audit events</label>
      <label className="block"><input type="checkbox" checked={!!form.acknowledge_controlled} onChange={e => setForm({ ...form, acknowledge_controlled: e.target.checked })} /> I acknowledge this SIEM destination is inside our trusted boundary and may receive controlled audit details outside my audience.</label>
      <button type="submit" className="btn btn-primary" disabled={save.isPending}>Save destination</button>{' '}
      {editing !== 'new' && <button type="button" onClick={() => { setEditing('new'); setForm(empty) }}>Cancel edit</button>}
    </form>
    {message && <p role="status">{message}</p>}
    {[list.error, verify.error, save.error, remove.error, accept.error, initialize.error].filter(Boolean).map((e, i) => <p role="alert" key={i}>{(e as Error).message}</p>)}
  </section>
}
