import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { printers } from '../../api/printers'
import type { PrintObject } from '../../types/printer'
import { SKIP_OBJECTS_WARNING } from '../../utils/supportLevels'
import Modal from '../ui/Modal'
import CheckTarget from '../ui/CheckTarget'
import Button from '../ui/Button'

const objectLabel = (object: PrintObject) => `${object.number}. ${object.name || 'Unnamed object'}`

/** Mounted only while open: polling and selections belong to this dialog session. */
export default function SkipObjectsDialog({ printerId, onClose }: { printerId: number; onClose: () => void }) {
  const [selection, setSelection] = useState<{ jobId: number | null; ids: string[] }>({ jobId: null, ids: [] })
  const [confirming, setConfirming] = useState(false)
  const [sending, setSending] = useState(false)
  const sendingRef = useRef(false)
  const [reloadRequired, setReloadRequired] = useState(false)
  const [message, setMessage] = useState('')
  const [selectionLimitReached, setSelectionLimitReached] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const limitRef = useRef<HTMLParagraphElement>(null)
  const focusReady = useRef(false)
  useEffect(() => {
    focusReady.current = true // Let Modal capture the opener first, including StrictMode remounts.
    return () => { focusReady.current = false }
  }, [])
  const focusAfterRender = useRef<'message' | 'list' | null>(null)
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ['print-objects', printerId],
    queryFn: () => printers.printObjects(printerId),
    retry: false,
    gcTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: query => sending || reloadRequired ? false
      : query.state.data?.reason_code === 'preparing'
        ? Math.max(1, query.state.data.retry_after_seconds ?? 10) * 1000 : 10000,
  })
  const jobId = data?.print?.print_job_id ?? null
  const selected = data?.supported && data.can_skip && selection.jobId === jobId
    ? data.objects.filter(object => selection.ids.includes(object.id) && object.state !== 'skipped' && object.skippable).slice(0, data.max_selectable)
    : []
  const selectedIds = selected.map(object => object.id)
  const validSelection = selected.length > 0 && selected.length === selection.ids.length

  // A removed control must hand focus to the newly rendered dialog content before paint.
  useLayoutEffect(() => {
    const root = contentRef.current
    if (!root || !focusReady.current) return
    const dialog = root.closest('[role="dialog"]')
    if (focusAfterRender.current || !dialog?.contains(document.activeElement)) {
      const messageTarget = root.querySelector<HTMLElement>('[role="status"], [role="alert"]')
      const firstCheckbox = root.querySelector<HTMLInputElement>('input[type="checkbox"]:not(:disabled)')
      const target = focusAfterRender.current === 'list' ? firstCheckbox || messageTarget : messageTarget || firstCheckbox
      ;(target || dialog?.querySelector<HTMLElement>('button:not(:disabled)'))?.focus()
      focusAfterRender.current = null
    }
  })

  // A new print, a changed permission, or a newly skipped object invalidates confirmation.
  useEffect(() => {
    if (sendingRef.current) return
    if (selection.jobId !== jobId || selectedIds.join(',') !== selection.ids.join(',')) {
      setSelection({ jobId, ids: selectedIds })
      setConfirming(false)
    }
  }, [data]) // selection changes are handled by toggle; only a refreshed list reconciles it.

  function toggle(object: PrintObject) {
    if (!data?.can_skip || sending || reloadRequired || object.state === 'skipped' || !object.skippable) return
    setMessage('')
    setSelectionLimitReached(false)
    if (selectedIds.includes(object.id)) {
      setSelection({ jobId, ids: selectedIds.filter(id => id !== object.id) })
    } else if (selected.length >= data.max_selectable) {
      setSelectionLimitReached(true)
      // The refused tick can be far from the message on a phone: bring the reason into view every time.
      requestAnimationFrame(() => limitRef.current?.scrollIntoView?.({ block: 'nearest' }))
    } else {
      setSelection({ jobId, ids: [...selectedIds, object.id] })
    }
  }

  async function send() {
    if (sendingRef.current || reloadRequired || isError || !validSelection || jobId === null) return
    sendingRef.current = true
    setSending(true)
    setMessage('')
    try {
      const receipt = await printers.skipObjects(printerId, jobId, selectedIds)
      focusAfterRender.current = 'message'
      setMessage(receipt.message)
      setSelection({ jobId, ids: [] })
      setConfirming(false)
      await refetch()
    } catch (error) {
      focusAfterRender.current = 'message'
      setMessage(error instanceof Error ? error.message : 'ODIN could not confirm the skip. Reload the object list before trying again.')
      // Network failures can also mean a command arrived. Never offer a resend of stale selections.
      setReloadRequired(true)
      setConfirming(false)
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  async function reload() {
    setSelection({ jobId: null, ids: [] })
    setConfirming(false)
    const result = await refetch()
    if (!result.isError) {
      focusAfterRender.current = 'list'
      setReloadRequired(false)
      setMessage('')
    }
  }

  function showOptIn() {
    onClose()
    // Modal restores the opener synchronously on unmount; focus the linked control afterwards.
    requestAnimationFrame(() => {
      const target = document.getElementById(`experimental-opt-in-${printerId}`)
      if (target instanceof HTMLDetailsElement) target.open = true
      target?.scrollIntoView({ block: 'center' })
      const checkbox = target?.querySelector<HTMLInputElement>('input[type="checkbox"]:not(:disabled)')
      ;(checkbox || target?.querySelector<HTMLElement>('summary'))?.focus()
    })
  }

  return (
    <Modal isOpen onClose={() => { if (!sendingRef.current) onClose() }} title={confirming ? `Skip ${selected.length} ${selected.length === 1 ? 'object' : 'objects'}?` : 'Skip objects'} size="xl">
      <div ref={contentRef} className="space-y-4 text-sm text-[var(--brand-text-primary)]">
        {message && <p role="status" tabIndex={-1}>{message}</p>}
        {reloadRequired ? <Button className="min-h-11" onClick={reload}>Reload list</Button>
          : isPending ? <p role="status" tabIndex={-1}>Reading the objects of this print...</p>
          : isError ? <><p role="alert" tabIndex={-1}>ODIN could not load the object list. Try again.</p><Button className="min-h-11" onClick={() => refetch()}>Try again</Button></>
          : !data?.supported ? <>
            <p role="status" tabIndex={-1}>{data?.reason}</p>
            {data?.reason_code === 'experimental_not_enabled' && <a className="underline inline-flex items-center min-h-11" href={`#experimental-opt-in-${printerId}`} onClick={showOptIn}>Skip objects settings</a>}
          </> : <>
            <p className="break-words">{data.print?.name || 'Unnamed print'} · Plate {data.print?.plate}</p>
            {data.experimental && <p className="text-[var(--brand-text-secondary)]">{SKIP_OBJECTS_WARNING}</p>}
            {confirming ? <>
              <ul className="space-y-2 break-words">{selected.map(object => <li key={object.id}>{objectLabel(object)}</li>)}</ul>
              <p>The printer stops printing {selected.length === 1 ? 'this object' : 'these objects'} for the rest of this print. This cannot be undone.</p>
              <div data-modal-footer className="flex flex-wrap gap-2">
                <Button className="min-h-11 aria-disabled:opacity-50 aria-disabled:cursor-not-allowed" aria-disabled={sending || !validSelection} onClick={send}>Skip {selected.length} {selected.length === 1 ? 'object' : 'objects'}</Button>
                <Button className="min-h-11" autoFocus variant="secondary" disabled={sending} onClick={() => setConfirming(false)}>Back</Button>
              </div>
            </> : <>
              {!data.skipped_reported && <p>The printer has not reported any skipped objects for this print.</p>}
              {data.picture.available && <div aria-hidden="true">
                <svg viewBox={`0 0 ${100 * (data.picture.aspect || 1)} 100`} className="w-full max-w-sm mx-auto border border-[var(--brand-card-border)]" data-testid="object-picture">
                  {data.objects.map(object => {
                    if (!object.bounds) return null
                    const { x, y, w, h } = object.bounds
                    const width = 100 * (data.picture.aspect || 1)
                    const skipped = object.state === 'skipped'
                    const checked = selectedIds.includes(object.id)
                    return <g key={object.id} onClick={() => toggle(object)} className="cursor-pointer">
                      <rect x={x * width} y={y * 100} width={w * width} height={h * 100} fill={skipped ? 'var(--brand-text-muted)' : checked ? 'var(--brand-primary)' : 'var(--brand-card-bg)'} fillOpacity={skipped ? 0.25 : 1} stroke="currentColor" strokeWidth={checked ? 1.5 : 0.5} strokeDasharray={skipped ? '2 1' : undefined} />
                      <text x={(x + w / 2) * width} y={(y + h / 2) * 100} textAnchor="middle" dominantBaseline="middle" fill="currentColor" fontSize="3">{object.number}</text>
                    </g>
                  })}
                </svg>
                {data.picture.front_edge === 'bottom' && <p className="text-center">Front of printer</p>}
              </div>}
              <ul className="space-y-2">{data.objects.map(object => <li key={object.id} className="flex items-center gap-3 min-h-11 border-b border-[var(--brand-card-border)] py-2">
                <CheckTarget touch><input className="h-6 w-6 shrink-0" type="checkbox" aria-label={objectLabel(object)} checked={selectedIds.includes(object.id)} disabled={!data.can_skip || object.state === 'skipped' || !object.skippable || sending} onChange={() => toggle(object)} /></CheckTarget>
                <span className="min-w-0 break-words">{objectLabel(object)}{object.state === 'skipped' ? ' — Skipped' : !object.skippable ? ' — Cannot be sent to this printer' : ''}</span>
              </li>)}</ul>
              {selectionLimitReached && <p ref={limitRef} role="status" tabIndex={-1}>At least one object has to keep printing. To end the print, use Stop.</p>}
              {data.can_skip && <Button className="min-h-11" autoFocus={validSelection} disabled={!validSelection || sending} onClick={() => { setMessage(''); setSelectionLimitReached(false); setConfirming(true) }}>Review selection</Button>}
            </>}
          </>}
      </div>
    </Modal>
  )
}
