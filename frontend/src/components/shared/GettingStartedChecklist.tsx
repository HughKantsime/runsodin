/**
 * GettingStartedChecklist — the dashboard's "Getting started" card.
 *
 * Five lines, each read from real state and each linking to where it is done:
 * a printer is added, a printer is online, a file is uploaded, filament is set on a slot,
 * and a first job has been sent to a printer. The card goes away when every line is done
 * or when the user dismisses it (remembered per user in this browser).
 *
 * It replaces the fixed tour. Anyone who finished or skipped that tour never sees the card.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { CheckCircle, Circle } from 'lucide-react'
import { Button, Card } from '../ui'
import { printers as printersApi, jobs as jobsApi, models as modelsApi, printFiles } from '../../api'
import { canAccessPage, canDo, getCurrentUser } from '../../permissions'
import { isOnline } from '../../utils/shared'

// The old tour's flags (browser-wide, and per user once it knew the user): still honoured.
const LEGACY_TOUR_KEY = 'odin_onboarding_complete'
const dismissKeyFor = (userId: number | string) => `odin_getting_started_dismissed:${userId}`

/** Hidden for this user. An unknown user or unreadable storage counts as hidden: never show blind. */
function hidden(userId: number | string | null | undefined): boolean {
  if (userId == null) return true
  try {
    return !!localStorage.getItem(LEGACY_TOUR_KEY) || !!localStorage.getItem(`${LEGACY_TOUR_KEY}:${userId}`)
      || !!localStorage.getItem(dismissKeyFor(userId))
  } catch { return true }
}

// A job in one of these states has been sent to a printer.
const SENT_STATUSES = ['printing', 'paused', 'completed', 'failed']

/** One uploaded file is enough; a model added by hand (no file) counts too. */
async function hasFileOrModel(): Promise<boolean> {
  if (await printFiles.any()) return true
  const library = await modelsApi.list()
  return Array.isArray(library) && library.length > 0
}

interface Line { id: string; label: string; done: boolean; to: string; allowed: boolean }

export default function GettingStartedChecklist() {
  const user = getCurrentUser()
  const userId = user?.id ?? null
  const [dismissed, setDismissed] = useState(() => hidden(userId))
  useEffect(() => { setDismissed(hidden(userId)) }, [userId])
  // Viewers cannot do any of these steps.
  const show = userId != null && !!user && user.role !== 'viewer' && !dismissed

  // Same key and call as the dashboard's own printer list, so this adds no request.
  const fleet = useQuery({ queryKey: ['printers'], queryFn: () => printersApi.list(true), enabled: show, refetchInterval: 30000 })
  const files = useQuery({ queryKey: ['getting-started', 'file', userId], queryFn: hasFileOrModel, enabled: show, refetchInterval: 30000, retry: false })
  const sent = useQuery({
    queryKey: ['getting-started', 'job-sent', userId],
    queryFn: async () => (await jobsApi.list(SENT_STATUSES, null, { limit: 1 })).length > 0,
    enabled: show, refetchInterval: 30000, retry: false,
  })

  const list = Array.isArray(fleet.data) ? fleet.data : null
  const lines: Line[] = []
  if (list) {
    lines.push({ id: 'printer-added', label: 'Add a printer', done: list.length > 0, to: '/printers', allowed: canDo('printers.add') })
    lines.push({ id: 'printer-online', label: 'See a printer online', done: list.some(p => isOnline(p)), to: '/printers', allowed: canAccessPage('printers') })
    // A fact that cannot be read is left out, not guessed.
    if (typeof files.data === 'boolean') {
      const uploadPage = canAccessPage('upload') && canDo('upload.upload')
      lines.push({ id: 'file-uploaded', label: 'Upload a print file', done: files.data, to: uploadPage ? '/upload' : '/models', allowed: uploadPage || canDo('models.create') })
    }
    lines.push({
      id: 'filament-set', label: 'Set the filament in a printer slot',
      done: list.some(p => (p.filament_slots || []).some(s => !!s.filament_type && s.filament_type !== 'empty')),
      to: '/printers', allowed: canDo('printers.slots'),
    })
    if (typeof sent.data === 'boolean') {
      lines.push({ id: 'job-sent', label: 'Send your first job to a printer', done: sent.data, to: '/jobs', allowed: canAccessPage('jobs') && canDo('jobs.create') })
    }
  }
  const settled = !!list && !files.isLoading && !sent.isLoading
  const allDone = settled && lines.every(line => line.done)

  const remember = () => {
    if (userId != null) { try { localStorage.setItem(dismissKeyFor(userId), 'true') } catch { /* storage unavailable */ } }
    setDismissed(true)
  }
  // Finished is remembered too, so the card does not come back when a printer later goes offline.
  useEffect(() => { if (show && allDone) remember() }, [show, allDone]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!show || !settled || allDone) return null
  const doneCount = lines.filter(line => line.done).length

  return (
    <Card className="mb-6 border border-[var(--brand-card-border)]" role="region" aria-label="Getting started" data-testid="getting-started">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h2 className="font-semibold text-sm text-[var(--brand-text-primary)]">Getting started</h2>
          <p className="text-xs text-[var(--brand-text-muted)] mt-0.5">{doneCount} of {lines.length} done</p>
        </div>
        <Button variant="ghost" size="sm" onClick={remember}>Dismiss</Button>
      </div>
      <ul className="space-y-2">
        {lines.map(line => (
          <li key={line.id} data-step={line.id} data-done={line.done ? 'true' : 'false'} className="flex items-center gap-2 text-sm">
            {line.done
              ? <CheckCircle size={16} className="shrink-0 text-[var(--status-completed)]" aria-hidden="true" />
              : <Circle size={16} className="shrink-0 text-[var(--brand-text-muted)]" aria-hidden="true" />}
            <span className="sr-only">{line.done ? 'Done:' : 'To do:'}</span>
            {line.done
              ? <span className="text-[var(--brand-text-muted)] line-through">{line.label}</span>
              : line.allowed
                ? <Link to={line.to} className="text-[var(--brand-primary)] hover:underline">{line.label}</Link>
                : <span className="text-[var(--brand-text-primary)]">{line.label} <span className="text-xs text-[var(--brand-text-muted)]">(ask an administrator)</span></span>}
          </li>
        ))}
      </ul>
    </Card>
  )
}
