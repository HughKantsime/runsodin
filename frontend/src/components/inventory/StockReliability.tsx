import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { spools, filaments } from '../../api/inventory'
import type { ReorderRow } from '../../api/inventory'
import { Button, Modal } from '../ui'
import { canDo } from '../../permissions'
import type { PurchasePrincipal } from './PurchaseEvidenceModal'

export default function StockReliability({ orgId }: { orgId?: number | null }) {
  const client = useQueryClient()
  const { data: principal } = useQuery<PurchasePrincipal | null>({ queryKey: ['auth', 'me'], enabled: false })
  // The selected org is a read filter; receiving and policy edits use the
  // authenticated org. Don't offer writes from a different org's view.
  const editable = canDo('spools.edit') && (orgId == null || orgId === principal?.group_id)
  const [receiving, setReceiving] = useState(false)
  const [editing, setEditing] = useState<ReorderRow | null>(null)
  const [threshold, setThreshold] = useState('1000')
  const [received, setReceived] = useState<number[]>([])
  const [form, setForm] = useState({ filament_id: '', quantity: '1', initial_weight_g: '1000', storage_location: '', sealed: true })
  const rollup = useQuery({ queryKey: ['spools', 'reorder', orgId], queryFn: () => spools.reorder(orgId) })
  const library = useQuery({ queryKey: ['filaments', 'receiving'], queryFn: filaments.list, enabled: receiving })
  const policy = useMutation({ mutationFn: spools.setReorderPolicy, retry: false,
    onSuccess: () => { client.invalidateQueries({ queryKey: ['spools'] }); setEditing(null) } })
  const receive = useMutation({ mutationFn: spools.receive, retry: false,
    onSuccess: result => { client.invalidateQueries({ queryKey: ['spools'] }); setReceived(result.spools.map(s => s.id)); setReceiving(false) } })
  return <section className="mb-6 space-y-3" aria-label="Reorder stock">
    <div className="flex gap-3 items-center"><h2 className="text-lg">Stock by material and color</h2>
      {editable && <Button onClick={() => { receive.reset(); setReceived([]); setReceiving(true) }}>Receive rolls</Button>}
    </div>
    <p className="text-sm text-[var(--brand-text-muted)]">Sealed rolls plus open grams, including loaded stock. Existing rolls count as open. Default reorder threshold: 1000 g.</p>
    {rollup.isPending && <p>Loading reorder stock…</p>}
    {rollup.isError && <p role="alert">Could not load reorder stock. <button onClick={() => rollup.refetch()}>Retry</button></p>}
    {rollup.data?.length === 0 && <p>No active stock or configured thresholds.</p>}
    {rollup.data && <div className="overflow-x-auto"><table className="w-full text-sm text-left">
      <thead><tr>{['Material', 'Color', 'Sealed', 'Open', 'Total', 'Threshold', 'Reorder'].map(label => <th key={label} className="p-2">{label}</th>)}</tr></thead>
      <tbody>{rollup.data.map(row => <tr key={`${row.material}:${row.color}`}>
        <td className="p-2">{row.material}</td><td className="p-2">{row.color}</td>
        <td className="p-2">{row.sealed_rolls} rolls / {row.sealed_g.toFixed(0)} g</td><td className="p-2">{row.open_g.toFixed(0)} g</td><td className="p-2">{row.total_g.toFixed(0)} g</td>
        <td className="p-2">{editable ? <button className="underline" aria-label={`Set threshold for ${row.material} ${row.color}`} onClick={() => { policy.reset(); setEditing(row); setThreshold(String(row.threshold_g)) }}>{row.threshold_g} g</button> : `${row.threshold_g} g`}</td>
        <td className="p-2">{row.reorder ? `Order ${row.shortfall_g.toFixed(0)} g` : 'Stock sufficient'}</td>
      </tr>)}</tbody>
    </table></div>}
    {editing && <Modal isOpen title="Reorder threshold" onClose={() => setEditing(null)}><form className="space-y-3" onSubmit={event => { event.preventDefault(); policy.mutate({ material: editing.material, color: editing.color, threshold_g: Number(threshold) }) }}>
      <label>Threshold (g)<input aria-label="Threshold (g)" type="number" min="0" max="10000000" step="1" required value={threshold} onChange={e => setThreshold(e.target.value)} /></label>
      {policy.isError && <p role="alert">{policy.error.message}</p>}<Button type="submit" disabled={policy.isPending}>Save threshold</Button>
    </form></Modal>}
    {receiving && <Modal isOpen title="Receive rolls into storage" onClose={() => setReceiving(false)}><form className="space-y-3" onSubmit={event => { event.preventDefault(); receive.mutate({ ...form, filament_id: Number(form.filament_id), quantity: Number(form.quantity), initial_weight_g: Number(form.initial_weight_g) }) }}>
      <label className="block">Filament<select aria-label="Receiving filament" required value={form.filament_id} onChange={e => setForm({ ...form, filament_id: e.target.value })}><option value="">Select filament</option>{library.data?.map(f => <option key={f.id} value={String(f.id).replace('lib_', '')}>{f.brand} {f.name} ({f.material})</option>)}</select></label>
      {library.isError && <p role="alert">Could not load filaments.</p>}
      <label className="block">Number of rolls<input aria-label="Number of rolls" type="number" min="1" max="100" step="1" required value={form.quantity} onChange={e => setForm({ ...form, quantity: e.target.value })} /></label>
      <label className="block">Net grams per roll<input aria-label="Net grams per roll" type="number" min="1" max="100000" required value={form.initial_weight_g} onChange={e => setForm({ ...form, initial_weight_g: e.target.value })} /></label>
      <label className="block">Storage location<input aria-label="Storage location" maxLength={100} required value={form.storage_location} onChange={e => setForm({ ...form, storage_location: e.target.value })} /></label>
      <label className="block"><input type="checkbox" checked={form.sealed} onChange={e => setForm({ ...form, sealed: e.target.checked })} /> Factory sealed</label>
      {receive.isError && <p role="alert">{receive.error.message}</p>}
      <Button type="submit" disabled={receive.isPending || !library.data}>Receive into storage</Button>
    </form></Modal>}
    {received.length > 0 && <div role="status"><p>Received {received.length} rolls. Print these QR labels for non-RFID scan assignment:</p>
      {Array.from({ length: Math.ceil(received.length / 40) }, (_, i) => <a key={i} className="underline block" target="_blank" rel="noopener noreferrer" href={`/api/spools/labels/batch?spool_ids=${received.slice(i * 40, (i + 1) * 40).join(',')}`}>Open printable labels {i + 1}</a>)}
    </div>}
  </section>
}
