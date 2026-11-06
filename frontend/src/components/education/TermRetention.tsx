import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { termRetention } from '../../api/termRetention'
import type { RetentionKind, TermPolicy, TermPreview } from '../../api/termRetention'
import type { EducationCostCenter } from '../../types'
import { Button, Card } from '../ui'

const kinds: RetentionKind[] = ['files', 'timelapses', 'thumbnails', 'portfolio']
const labels: Record<RetentionKind, string> = { files: 'Print files', timelapses: 'Timelapses', thumbnails: 'Thumbnails', portfolio: 'Portfolio visibility' }
const fieldStyle = 'rounded border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-2'

export default function TermRetention({ centers, audience }: { centers: EducationCostCenter[]; audience: string }) {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['term-retention', audience], queryFn: termRetention.list })
  const [name, setName] = useState('')
  const [center, setCenter] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [days, setDays] = useState<Record<RetentionKind, string>>({ files: '', timelapses: '', thumbnails: '', portfolio: '' })
  const [selection, setSelection] = useState<{ term: TermPolicy; report: TermPreview } | null>(null)
  const [students, setStudents] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  async function action(work: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('')
    try { await work() } catch (e) { setError(e instanceof Error ? e.message : 'Request failed. Run a new preview before retrying.') }
    finally { setBusy(false) }
  }
  async function loadPreview(term: TermPolicy, afterId = 0) {
    setSelection(null); setStudents([])
    const report = await termRetention.preview(term.id, afterId)
    setSelection({ term, report })
  }
  const canPurge = selection?.report.items.some(i => !i.blocked_reason && i.kinds.length > 0)
  return <div className="space-y-4">
    <Card><div className="space-y-3 p-4">
      <h2 className="text-lg font-semibold">End-of-term retention</h2>
      <p>Keep school audit records while removing content after a term. Cleanup runs only when an administrator previews and confirms it. Times use UTC.</p>
      <p className="text-sm">Submissions, approvals, job outcomes, usage, hashes and audit history remain. Portfolio cleanup hides entries from the active view; it preserves school history. Account anonymization is a separate action.</p>
      <form className="space-y-3" onSubmit={e => {
        e.preventDefault()
        void action(async () => {
          await termRetention.create({ name, cost_center_id: Number(center), starts_at: `${start}T00:00:00Z`, ends_at: `${end}T00:00:00Z`,
            files_days: days.files === '' ? null : Number(days.files), timelapses_days: days.timelapses === '' ? null : Number(days.timelapses),
            thumbnails_days: days.thumbnails === '' ? null : Number(days.thumbnails), portfolio_days: days.portfolio === '' ? null : Number(days.portfolio) })
          await queryClient.invalidateQueries({ queryKey: ['term-retention', audience] }); setMessage('Term policy saved. Preview it before cleanup.')
        })
      }}>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1">Term name<input required maxLength={120} className={fieldStyle} value={name} onChange={e => setName(e.target.value)} /></label>
          <label className="flex flex-col gap-1">Class or club<select required className={fieldStyle} value={center} onChange={e => setCenter(e.target.value)}>
            <option value="">Select class or club</option>{centers.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select></label>
          <label className="flex flex-col gap-1">Term start<input required type="date" className={fieldStyle} value={start} onChange={e => setStart(e.target.value)} /></label>
          <label className="flex flex-col gap-1">Term end (exclusive)<input required type="date" min={start} className={fieldStyle} value={end} onChange={e => setEnd(e.target.value)} /></label>
        </div>
        <p className="text-sm">Days after term end. Leave blank to keep. Unused policies can be deleted and replaced. Overlapping terms in the same class are refused.</p>
        <div className="grid gap-3 sm:grid-cols-2">{kinds.map(k => <label key={k} className="flex flex-col gap-1">{labels[k]}<input type="number" min="0" max="36500" step="1" placeholder="Keep" className={fieldStyle} value={days[k]} onChange={e => setDays({ ...days, [k]: e.target.value })} /></label>)}</div>
        <Button type="submit" disabled={busy}>Save term policy</Button>
      </form>
    </div></Card>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {query.isLoading && <p>Loading term policies…</p>}
    {query.isError && <p role="alert">Could not load policies. <button onClick={() => void query.refetch()}>Retry</button></p>}
    {!!query.data?.pending_erasures?.length && <Card><div className="space-y-3 p-4">
      <h3 className="font-semibold">Incomplete account erasures</h3>
      <p>These accounts still have erasure work pending. Resolve the reported failure, then resume. Original recovery identifiers remain private until erasure finishes.</p>
      {query.data.pending_erasures.map(({ user_id, username }) => <div key={user_id} className="flex flex-wrap items-center gap-3">
        <span>{username} (account {user_id})</span>
        <Button disabled={busy} onClick={() => {
          if (!window.confirm(`Resume erasure for account ${user_id}? This cannot be undone.`)) return
          void action(async () => {
            const result = await termRetention.resumeErasure(user_id)
            await queryClient.invalidateQueries({ queryKey: ['term-retention', audience] })
            setSelection(null); setStudents([])
            setMessage(result.state === 'anonymized' ? `Account ${user_id} erasure completed.`
              : result.state === 'erasure_incomplete' ? `Account ${user_id}: erasure_incomplete. Resolve the failure and resume again.`
              : `Account ${user_id}: blocked (${result.reason || 'erasure_failed'}). Resolve the failure and preview graduation again.`)
          })
        }}>Resume erasure for account {user_id}</Button>
      </div>)}
    </div></Card>}
    {query.data?.items.map(term => <Card key={term.id}><div className="flex flex-wrap items-center justify-between gap-3 p-4">
      <div><h3 className="font-semibold">{term.name}</h3><p className="text-sm">{term.starts_at.slice(0, 10)} to {term.ends_at.slice(0, 10)}</p><ul className="text-sm">{kinds.map(k => <li key={k}>{labels[k]}: {term[`${k}_days`] == null ? 'Keep' : `${term[`${k}_days`]} days after term end`}</li>)}</ul></div>
      <Button disabled={busy} onClick={() => {
        if (!window.confirm(`Delete ${term.name}? Policies can be deleted only before cleanup begins.`)) return
        void action(async () => {
          await termRetention.remove(term.id)
          setSelection(null)
          await queryClient.invalidateQueries({ queryKey: ['term-retention', audience] })
        })
      }}>Delete policy</Button>
      <Button disabled={busy} onClick={() => void action(() => loadPreview(term))}>Preview {term.name}</Button>
    </div></Card>)}
    {selection && <Card><div className="space-y-3 p-4">
      <h3 className="font-semibold">Dry run: {selection.term.name}</h3>
      <p>No content was deleted by this preview. {selection.report.items.length} submissions on this page.</p>
      <ul>{kinds.map(k => <li key={k}>{labels[k]}: {selection.report.due[k] ? 'Due for cleanup' : 'Keep or not yet due'}</li>)}</ul>
      <ul>{selection.report.items.map(i => <li key={i.submission_id}>Submission {i.submission_id}: {i.blocked_reason ? `Blocked — ${i.blocked_reason.replace(/_/g, ' ')}` : i.kinds.map(k => i.blocked_kinds?.[k] ? `${k}: blocked (${i.blocked_kinds[k]})` : k).join(', ') || 'No cleanup due'}</li>)}</ul>
      <Button disabled={busy || !canPurge} onClick={() => {
        if (!window.confirm('Permanently purge the eligible content in this preview? School history remains.')) return
        void action(async () => {
          const result = await termRetention.purge(selection.term.id, selection.report.fingerprint, selection.report.after_id || 0)
          if (result.next_after_id != null) await loadPreview(selection.term, result.next_after_id)
          else setSelection(null)
          setMessage(`Cleanup: ${result.results.filter(r => r.state === 'done').length} completed, ${result.results.filter(r => r.state === 'blocked').length} blocked. ${result.results.filter(r => r.reason).map(r => `Submission ${r.submission_id} ${r.kind}: ${r.reason}`).join('; ')} Preview again to retry blocked work.`)
        })
      }}>Purge due content</Button>
      {selection.report.next_after_id != null && <Button disabled={busy} onClick={() => void action(() => loadPreview(selection.term, selection.report.next_after_id!))}>Next page of submissions</Button>}
      <h3 className="font-semibold">Graduation anonymization</h3>
      <p className="text-sm">Only inactive student accounts without active class grants or jobs are eligible. Account access and identity are removed using GDPR erasure; school history remains.</p>
      {selection.report.eligible_student_ids.length === 0 && <p>No eligible departed students.</p>}
      {selection.report.eligible_students.map(({ id, username }) => <label key={id} className="flex gap-2"><input type="checkbox" checked={students.includes(id)} onChange={e => setStudents(e.target.checked ? [...students, id] : students.filter(s => s !== id))} />{username} (account {id})</label>)}
      <Button disabled={busy || students.length === 0} onClick={() => {
        if (!window.confirm(`Anonymize ${students.length} selected student accounts? This cannot be undone.`)) return
        void action(async () => {
          const result = await termRetention.anonymize(selection.term.id, selection.report.fingerprint, students, selection.report.after_id || 0)
          setSelection(null); setStudents([])
          await queryClient.invalidateQueries({ queryKey: ['term-retention', audience] })
          setMessage(`Graduation: ${result.results.filter(r => r.state === 'anonymized').length} anonymized, ${result.results.filter(r => r.state === 'erasure_incomplete').length} erasure_incomplete, ${result.results.filter(r => r.state === 'blocked').length} blocked. ${result.results.filter(r => r.reason).map(r => `Account ${r.user_id}: ${r.reason}`).join('; ')} Resume pending erasures after resolving the failure; preview graduation again for blocked accounts.`)
        })
      }}>Anonymize selected students</Button>
    </div></Card>}
  </div>
}
