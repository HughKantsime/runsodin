import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { printers } from '../../api'
import { canDo } from '../../permissions'
import type { Printer } from '../../types'
import { parseServerDate } from '../../utils/shared'
import Button from '../ui/Button'
import Modal from '../ui/Modal'

export default function PlateCleared({ printer }: { printer: Pick<Printer, 'id' | 'name' | 'nickname' | 'plate_clear' | 'plate_clear_confirmed_at'> }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [confirmation, setConfirmation] = useState<Printer['plate_clear']>(null)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState('')
  const pressing = useRef(false)
  const plate = printer.plate_clear
  const waiting = confirmation?.has_waiting_job || !!confirmation?.releases_job
  const name = printer.nickname || printer.name
  const at = printer.plate_clear_confirmed_at && parseServerDate(printer.plate_clear_confirmed_at)
  async function confirm() {
    if (pressing.current || !canDo('printers.edit') || !confirmation?.needed) return
    pressing.current = true
    setPending(true)
    setMessage('')
    try {
      const expected = waiting && !confirmation.releases_job
        ? { expected_has_waiting_job: true }
        : { expected_release_job_id: confirmation.releases_job?.id ?? null }
      const reply = await printers.plateCleared(printer.id, expected)
      setMessage(reply?.message || 'Plate clear was not confirmed.')
      setOpen(false)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['printers'] }),
        queryClient.invalidateQueries({ queryKey: ['printer'] }),
        queryClient.invalidateQueries({ queryKey: ['jobs'] }),
      ])
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Plate clear was not confirmed.')
      const refusal = error as { status?: number; detail?: { plate_clear?: Printer['plate_clear'] } }
      if (refusal?.status === 409 && refusal.detail?.plate_clear) {
        setConfirmation(refusal.detail.plate_clear)
        setOpen(true)
      } else {
        setOpen(false)
      }
    } finally {
      pressing.current = false
      setPending(false)
    }
  }
  if (!plate?.needed && !at && !message && !open) return null
  return <div className="min-w-0 space-y-2 p-3 break-words" onClick={event => event.stopPropagation()}>
    {plate?.needed ? <>
      <p>Plate not cleared yet</p>
      {canDo('printers.edit') && <Button disabled={pending} onClick={() => { setMessage(''); setConfirmation(plate); setOpen(true) }}>Plate cleared</Button>}
    </> : at && Number.isFinite(at.getTime()) && <p>Plate cleared <time dateTime={printer.plate_clear_confirmed_at!}>{at.toLocaleString()}</time></p>}
    {message && !open && <p role="status">{message}</p>}
    {open && createPortal(<Modal isOpen title="Plate cleared" size="sm" onClose={() => { if (!pressing.current) setOpen(false) }}>
      {message && <p role="status">{message}</p>}
      <p className="break-words">Mark the plate on {name} as cleared? {waiting
        ? `${confirmation?.releases_job?.name || 'A job'} is waiting for it and will be released to start.`
        : 'Nothing will start.'}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="secondary" disabled={pending} onClick={() => setOpen(false)}>Cancel</Button>
        <Button disabled={pending || !confirmation?.needed} onClick={confirm}>{waiting ? 'Plate cleared, release job' : 'Plate cleared'}</Button>
      </div>
    </Modal>, document.body)}
  </div>
}
