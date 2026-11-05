import { useEffect, useRef, useState } from 'react'
import { timelapses } from '../../api'
import type { TimelapseRecheckPage } from '../../api/cameras'
import Button from '../ui/Button'

type Props = { current: () => boolean; onComplete: () => void }
export default function TimelapseFactsRecheck({ current, onComplete }: Props) {
  const [page, setPage] = useState<TimelapseRecheckPage | null>(null)
  const [pending, setPending] = useState(false), [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => { controller.current?.abort() }, [])
  async function recheck(afterId = 0) {
    if (!current() || controller.current) return
    const abort = new AbortController(); controller.current = abort
    setPending(true); setError('')
    try {
      const result = await timelapses.recheckFacts({ all_ready: true }, afterId, abort.signal)
      if (abort.signal.aborted || !current()) return
      setPage(result); onComplete()
    } catch (err) {
      if (!abort.signal.aborted && current()) setError((err instanceof Error ? err.message : 'Re-check unavailable') + '. Some videos may already have been re-admitted; retry explicitly.')
    } finally {
      if (!abort.signal.aborted && current()) { controller.current = null; setPending(false) }
    }
  }
  return <section aria-label="Restart video re-check" className="border border-[var(--brand-card-border)] rounded-lg p-4 space-y-2">
    <p className="text-sm text-[var(--brand-text-muted)]">Verifies saved video content and restores access after a restart, up to 100 ready recordings per click.</p>
    <Button variant="secondary" loading={pending} onClick={() => recheck()}>Re-check videos after a restart</Button>
    {error && <p role="alert">{error}</p>}
    <div role="status" aria-live="polite">
      {pending && <p>Checking videos…</p>}
      {page && <>
        {!page.results.length && <p>No ready recordings to re-check.</p>}
        <ul>{page.results.map(result => <li key={result.timelapse_id}>Recording {result.timelapse_id}: {result.status === 'refused' ? `Refused — ${result.reason}` : `Re-admitted${result.changed ? '' : ' (already verified)'}`}</li>)}</ul>
      </>}
    </div>
    {page?.next_after_id != null && <Button variant="secondary" disabled={pending} onClick={() => recheck(page.next_after_id!)}>Re-check next 100 videos</Button>}
  </section>
}
