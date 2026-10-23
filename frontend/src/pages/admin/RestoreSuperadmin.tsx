import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { DialogSurface } from '../../components/ui'
import { users } from '../../api'

export default function RestoreSuperadmin({ user }: { user: { id: number; username: string } }) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const busy = useRef(false)
  const restore = useMutation({
    retry: false,
    mutationFn: () => users.restoreSuperadmin(user.id, confirmation),
    onSuccess: () => { setOpen(false); setConfirmation('') },
    onSettled: () => { busy.current = false; client.invalidateQueries({ queryKey: ['users'] }) },
  })
  const close = () => { if (!busy.current) { setOpen(false); setConfirmation(''); restore.reset() } }
  return <>
    <button onClick={() => setOpen(true)} disabled={restore.isPending} aria-label={`Restore platform Super Admin for ${user.username}`}>Restore Super Admin</button>
    {open && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <DialogSurface role="dialog" aria-modal="true" aria-labelledby={`restore-title-${user.id}`} onEscape={close} className="w-full max-w-lg rounded-md bg-[var(--brand-card-bg)] p-6 space-y-4">
        <h2 id={`restore-title-${user.id}`}>Restore platform Super Admin</h2>
        <p>This revokes Education grants, ends school membership, and signs the account out of all sessions and API tokens. Live Education work must be resolved first.</p>
        <label className="block">Type {user.username} to confirm
          <input aria-label="Confirm target username" value={confirmation} onChange={event => setConfirmation(event.target.value)} disabled={restore.isPending} className="block w-full bg-[var(--brand-input-bg)] p-2" />
        </label>
        {restore.isError && <p role="alert">{restore.error.message}</p>}
        <button onClick={close} disabled={restore.isPending}>Cancel</button>
        <button disabled={confirmation !== user.username || restore.isPending} onClick={() => { if (!busy.current && confirmation === user.username) { busy.current = true; restore.mutate() } }}>{restore.isPending ? 'Restoring…' : 'Restore account'}</button>
      </DialogSurface>
    </div>}
  </>
}
