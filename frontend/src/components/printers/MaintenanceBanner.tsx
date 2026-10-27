import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { printerMaintenance, type MaintenanceState } from '../../api/printerMaintenance'
import { Button } from '../ui'

export default function MaintenanceBanner({ printerId, editable = false, maintenance }: { printerId: number; editable?: boolean; maintenance?: MaintenanceState | null }) {
  const client = useQueryClient()
  const principal = client.getQueryData<{ role: string }>(['auth', 'me'])
  const query = useQuery({ queryKey: ['printer-maintenance', printerId], queryFn: () => printerMaintenance.get(printerId), enabled: maintenance === undefined, refetchInterval: maintenance === undefined ? 30000 : false })
  const current = maintenance === undefined ? query.data : maintenance
  const [editing, setEditing] = useState(false), [state, setState] = useState<MaintenanceState['state']>('ok'), [note, setNote] = useState('')
  const save = useMutation({ mutationFn: () => printerMaintenance.set(printerId, { state, note }), retry: false,
    onSuccess: result => { client.setQueryData(['printer-maintenance', printerId], result); client.invalidateQueries({ queryKey: ['next-suggestion', printerId] }); client.invalidateQueries({ queryKey: ['printers'] }); setEditing(false) } })
  return <div className="space-y-2 text-sm break-words">
    {maintenance === undefined && query.isError && <p role="alert">Maintenance status unavailable. <button onClick={() => query.refetch()}>Retry</button></p>}
    {current && current.state !== 'ok' && <div role="status" className="border border-amber-500 rounded p-3">
      <strong>{current.state === 'hold' ? 'Maintenance hold — dispatch blocked' : 'Maintenance warning'}</strong>
      <p>{current.note}</p><p className="text-xs">Set by {current.set_by_name || current.set_by || 'admin'}{current.set_at ? ` · ${new Date(current.set_at).toLocaleString()}` : ''}</p>
    </div>}
    {editable && principal?.role === 'admin' && current && <Button onClick={() => { setState(current!.state); setNote(current!.note); setEditing(true); save.reset() }}>Set maintenance state</Button>}
    {editing && <form className="space-y-2" onSubmit={e => { e.preventDefault(); save.mutate() }}>
      <label>Maintenance state<select aria-label="Maintenance state" value={state} disabled={save.isPending} onChange={e => setState(e.target.value as MaintenanceState['state'])}><option value="ok">OK — clear maintenance</option><option value="warn">Warn — dispatch allowed</option><option value="hold">Hold — block dispatch</option></select></label>
      <label>Maintenance note<textarea aria-label="Maintenance note" maxLength={1000} required={state !== 'ok'} value={note} disabled={save.isPending} onChange={e => setNote(e.target.value)} /></label>
      {save.isError && <p role="alert">{save.error.message}</p>}
      <Button type="submit" disabled={save.isPending || (state !== 'ok' && !note.trim())}>Save maintenance</Button>
      <Button type="button" disabled={save.isPending} onClick={() => setEditing(false)}>Cancel</Button>
    </form>}
  </div>
}
