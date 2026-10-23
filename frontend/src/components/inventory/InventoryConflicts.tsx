import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { Button, Card } from '../ui'

type Report = {
  duplicates: Array<{ spool_id: number; filament_slot_ids: number[] }>
  contradictions: Array<{ spool_id: number; filament_slot_id: number | null; direction: string }>
  slots: Array<{ id: number; printer_id: number; slot_number: number }>
}

export default function InventoryConflicts({ audience }: { audience: string }) {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['inventory-conflicts', audience], queryFn: () => fetchAPI<Report>('/inventory/conflicts') })
  const [choices, setChoices] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ids = [...new Set([...(query.data?.duplicates || []), ...(query.data?.contradictions || [])].map(row => row.spool_id))]
  return <Card>
    <h2 className="text-lg font-semibold">Resolve inventory conflicts</h2>
    <p className="text-sm">Choose the true loaded slot or explicitly clear the spool. Each choice is audited.</p>
    {(error || query.error) && <p role="alert">{error || query.error?.message}</p>}
    {query.isLoading ? <p>Loading conflicts…</p> : !ids.length && !query.error ? <p>No inventory conflicts.</p> : ids.map(id => <div key={id} className="my-3 flex flex-wrap gap-2 items-center">
      <span>Spool {id}: {query.data?.duplicates.some(r => r.spool_id === id) ? 'multiple slot links' : 'contradictory location'}</span>
      <select aria-label={`True slot for spool ${id}`} value={choices[id] ?? ''} onChange={e => setChoices({ ...choices, [id]: e.target.value })}>
        <option value="">Choose a resolution</option><option value="clear">Clear location and all links</option>
        {query.data?.slots.map(s => <option key={s.id} value={s.id}>Printer {s.printer_id}, slot {s.slot_number} (ID {s.id})</option>)}
      </select>
      <Button disabled={!choices[id] || busy} onClick={async () => {
        setBusy(true)
        try {
          await fetchAPI('/inventory/conflicts/resolve', { method: 'POST', body: JSON.stringify({ spool_id: id, slot_id: choices[id] === 'clear' ? null : Number(choices[id]) }) })
          setError('')
          await queryClient.invalidateQueries({ queryKey: ['inventory-conflicts'] })
          await queryClient.invalidateQueries({ queryKey: ['spools'] })
        } catch (e) { setError(e instanceof Error ? e.message : 'Resolution failed') }
        finally { setBusy(false) }
      }}>Apply resolution</Button>
    </div>)}
  </Card>
}
